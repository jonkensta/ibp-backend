"""Postmark-to-delivery latency of Filled requests, by postmark month.

Read-only: works from ``requests``, ``package_shipments`` (see
``ibp/shipment_links.py``), ``shipments`` and ``tracking_events``. The
definitions:

- A label is delivered on the local date (``clock.TIMEZONE``) of its
  earliest ``delivered`` event, unless a ``return_to_sender`` event came at
  or before that one: then the "delivery" is the box arriving back with us,
  and the label is returned. A label with a ``return_to_sender`` event and
  no ``delivered`` one is returned too. Any other label is not delivered yet.
- A request's delivery is over the linked labels the method counts (see
  ``METHOD_BASES``): ``earliest`` and ``latest`` are the first and last
  delivery dates among them (equal for one label; a bulk request links to
  every box of one session and cannot be told apart). With no delivered
  label, the request is returned if every label was, else not delivered yet.
- Latency is the delivery date minus the postmark, in days, for each bound.
  A request with either bound under 0 or over ``MAX_LATENCY_DAYS`` (a bad
  postmark) is excluded from the statistics and counted as ``excluded``.
- Stages, for a delivered request: handling (postmark to processed),
  waiting (processed to the local purchase date of its earliest linked
  label) and transit (that purchase date to delivery, for each bound). They
  add up to the latency.

Unlinked and undelivered requests appear only in the counts, never as a
latency.
"""

import datetime
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Optional
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from . import clock, models, schemas
from .shipment_links import local_date

DELIVERED = "delivered"
RETURN_TO_SENDER = "return_to_sender"
# A latency over this many days, like a negative one, means a bad postmark.
MAX_LATENCY_DAYS = 365

# The link bases each method counts.
METHOD_BASES: dict[schemas.DeliveryMethodEnum, frozenset[str]] = {
    schemas.DeliveryMethodEnum.ALL: frozenset(models.LINK_BASES),
    schemas.DeliveryMethodEnum.INDIVIDUAL: frozenset({"reference", "inmate_number"}),
    schemas.DeliveryMethodEnum.BULK: frozenset({"unit_session"}),
}

RequestKey = tuple[str, int, int]


@dataclass(frozen=True)
class FilledRequest:
    """A Filled request: its key, postmark and processed date."""

    key: RequestKey
    postmarked: datetime.date
    processed: datetime.date


@dataclass(frozen=True)
class Link:
    """A request's link to a label, with the label's purchase time (UTC)."""

    key: RequestKey
    shipment_id: int
    basis: str
    purchased_at: datetime.datetime


@dataclass(frozen=True)
class Event:
    """One tracking event of a label (time in UTC)."""

    shipment_id: int
    occurred_at: datetime.datetime
    status: Optional[str]


@dataclass(frozen=True)
class LabelOutcome:
    """A label's delivery date (local), or that it was returned."""

    delivered: Optional[datetime.date] = None
    returned: bool = False


@dataclass(frozen=True)
class RequestDelivery:
    """What became of a linked request's labels.

    ``purchased`` is the local purchase date of its earliest linked label;
    ``earliest`` and ``latest`` are set only when ``status`` is delivered.
    """

    status: Literal["delivered", "returned", "pending"]
    purchased: datetime.date
    earliest: Optional[datetime.date] = None
    latest: Optional[datetime.date] = None


@dataclass(frozen=True)
class Latency:
    """A delivered request's latency and stages, in days."""

    earliest: int
    latest: int
    handling: int
    waiting: int
    transit_earliest: int
    transit_latest: int


def label_outcomes(
    events: Iterable[Event], timezone: ZoneInfo = clock.TIMEZONE
) -> dict[int, LabelOutcome]:
    """The outcome of each label with a delivered or returned event, by id.

    A label missing from the result is not delivered yet.
    """
    first: dict[str, dict[int, datetime.datetime]] = {
        DELIVERED: {},
        RETURN_TO_SENDER: {},
    }
    for event in events:
        if event.status not in first:
            continue
        earliest = first[event.status]
        current = earliest.get(event.shipment_id)
        if current is None or event.occurred_at < current:
            earliest[event.shipment_id] = event.occurred_at
    delivered, returned = first[DELIVERED], first[RETURN_TO_SENDER]
    outcomes: dict[int, LabelOutcome] = {}
    for shipment_id in delivered.keys() | returned.keys():
        delivered_at = delivered.get(shipment_id)
        returned_at = returned.get(shipment_id)
        if delivered_at is None or (
            returned_at is not None and returned_at <= delivered_at
        ):
            outcomes[shipment_id] = LabelOutcome(returned=True)
        else:
            outcomes[shipment_id] = LabelOutcome(
                delivered=local_date(delivered_at, timezone)
            )
    return outcomes


def request_delivery(
    links: Sequence[Link],
    outcomes: Mapping[int, LabelOutcome],
    timezone: ZoneInfo = clock.TIMEZONE,
) -> RequestDelivery:
    """The delivery of a request over its (non-empty) ``links``."""
    if not links:
        raise ValueError("a request's delivery needs at least one link")
    purchased = min(local_date(link.purchased_at, timezone) for link in links)
    labels = [outcomes.get(link.shipment_id, LabelOutcome()) for link in links]
    dates = [label.delivered for label in labels if label.delivered is not None]
    if dates:
        return RequestDelivery("delivered", purchased, min(dates), max(dates))
    if all(label.returned for label in labels):
        return RequestDelivery("returned", purchased)
    return RequestDelivery("pending", purchased)


def request_latency(
    request: FilledRequest, delivery: RequestDelivery
) -> Optional[Latency]:
    """A delivered request's latency and stages; None when it is excluded
    (either bound under 0 or over ``MAX_LATENCY_DAYS``)."""
    if delivery.earliest is None or delivery.latest is None:
        raise ValueError("only a delivered request has a latency")
    earliest = (delivery.earliest - request.postmarked).days
    latest = (delivery.latest - request.postmarked).days
    if earliest < 0 or latest > MAX_LATENCY_DAYS:
        return None
    return Latency(
        earliest=earliest,
        latest=latest,
        handling=(request.processed - request.postmarked).days,
        waiting=(delivery.purchased - request.processed).days,
        transit_earliest=(delivery.earliest - delivery.purchased).days,
        transit_latest=(delivery.latest - delivery.purchased).days,
    )


def percentile(values: Sequence[float], fraction: float) -> float:
    """The ``fraction`` (0 to 1) percentile of ``values``, interpolating
    linearly between the closest ranks (numpy's default method)."""
    if not values:
        raise ValueError("a percentile needs at least one value")
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


@dataclass
class _Month:
    counts: Counter[str] = field(default_factory=Counter)
    latencies: list[Latency] = field(default_factory=list)

    def point(self, month: str) -> schemas.DeliveryLatencyMonth:
        """This month's counts and, if any request was delivered, statistics."""
        delivered = bool(self.latencies)
        return schemas.DeliveryLatencyMonth(
            month=month,
            filled=self.counts["filled"],
            linked=self.counts["linked"],
            delivered=len(self.latencies),
            returned=self.counts["returned"],
            excluded=self.counts["excluded"],
            earliest=self._quartiles("earliest") if delivered else None,
            latest=self._quartiles("latest") if delivered else None,
            median_handling=self._median("handling") if delivered else None,
            median_waiting=self._median("waiting") if delivered else None,
        )

    def _median(self, name: str) -> float:
        return percentile([getattr(x, name) for x in self.latencies], 0.5)

    def _quartiles(self, bound: str) -> schemas.LatencyQuartiles:
        days = [getattr(x, bound) for x in self.latencies]
        return schemas.LatencyQuartiles(
            p25=percentile(days, 0.25),
            median=percentile(days, 0.5),
            p75=percentile(days, 0.75),
            median_transit=self._median(f"transit_{bound}"),
        )


def summarize(
    requests: Iterable[FilledRequest],
    links: Iterable[Link],
    events: Iterable[Event],
    method: schemas.DeliveryMethodEnum = schemas.DeliveryMethodEnum.ALL,
    timezone: ZoneInfo = clock.TIMEZONE,
) -> list[schemas.DeliveryLatencyMonth]:
    """The latency metric per postmark month, oldest first.

    Every month with a Filled request is listed, linked or not. Only links
    whose basis ``method`` counts are used.
    """
    bases = METHOD_BASES[method]
    by_request: dict[RequestKey, list[Link]] = defaultdict(list)
    for link in links:
        if link.basis in bases:
            by_request[link.key].append(link)
    outcomes = label_outcomes(events, timezone)

    months: dict[str, _Month] = defaultdict(_Month)
    for request in requests:
        month = months[request.postmarked.strftime("%Y-%m")]
        month.counts["filled"] += 1
        request_links = by_request.get(request.key)
        if not request_links:
            continue
        month.counts["linked"] += 1
        delivery = request_delivery(request_links, outcomes, timezone)
        if delivery.status != "delivered":
            month.counts[delivery.status] += 1
            continue
        latency = request_latency(request, delivery)
        if latency is None:
            month.counts["excluded"] += 1
        else:
            month.latencies.append(latency)
    return [months[key].point(key) for key in sorted(months)]


async def load_rows(
    session: AsyncSession,
    start_date: Optional[datetime.date] = None,
    end_date: Optional[datetime.date] = None,
) -> tuple[list[FilledRequest], list[Link], list[Event]]:
    """Fetch the Filled requests postmarked in the range, their links and the
    linked labels' delivered and returned events, in two queries."""
    request, link, shipment = models.Request, models.PackageShipment, models.Shipment
    query = (
        sa.select(
            request.inmate_jurisdiction,
            request.inmate_id,
            request.index.label("request_index"),
            request.date_postmarked,
            request.date_processed,
            link.shipment_id,
            link.basis,
            shipment.purchased_at,
        )
        .select_from(request)
        .outerjoin(
            link,
            sa.and_(
                link.inmate_jurisdiction == request.inmate_jurisdiction,
                link.inmate_id == request.inmate_id,
                link.request_index == request.index,
            ),
        )
        .outerjoin(shipment, shipment.id == link.shipment_id)
        .where(request.action == "Filled")
    )
    if start_date:
        query = query.where(request.date_postmarked >= start_date)
    if end_date:
        query = query.where(request.date_postmarked <= end_date)

    requests: dict[RequestKey, FilledRequest] = {}
    links: list[Link] = []
    for row in (await session.execute(query)).all():
        key = (row.inmate_jurisdiction, row.inmate_id, row.request_index)
        if key not in requests:
            requests[key] = FilledRequest(key, row.date_postmarked, row.date_processed)
        if row.shipment_id is not None and row.purchased_at is not None:
            links.append(Link(key, row.shipment_id, row.basis, row.purchased_at))

    event = models.TrackingEvent
    event_rows = await session.execute(
        sa.select(event.shipment_id, event.occurred_at, event.status).where(
            event.status.in_((DELIVERED, RETURN_TO_SENDER)),
            event.shipment_id.in_(sa.select(link.shipment_id).distinct()),
        )
    )
    events = [Event(*row) for row in event_rows.all()]
    return list(requests.values()), links, events


async def get_delivery_latency(
    session: AsyncSession,
    method: schemas.DeliveryMethodEnum = schemas.DeliveryMethodEnum.ALL,
    start_date: Optional[datetime.date] = None,
    end_date: Optional[datetime.date] = None,
) -> list[schemas.DeliveryLatencyMonth]:
    """The latency metric for Filled requests postmarked in the range."""
    requests, links, events = await load_rows(session, start_date, end_date)
    return summarize(requests, links, events, method)
