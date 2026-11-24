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

A month is provisional while it is younger than the settle time (see
``settle_days``): unanswered letters are not in the database until they are
processed, so a recent month holds only its fastest requests so far, and
no coverage figure can show that.
"""

import calendar
import datetime
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Optional
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from . import clock, models, schemas
from .shipment_links import local_date

DELIVERED = "delivered"
RETURN_TO_SENDER = "return_to_sender"
# A latency over this many days, like a negative one, means a bad postmark.
MAX_LATENCY_DAYS = 365

# A postmark month is provisional until it is ``settle_days`` old: letters
# from it are still being answered, the slow ones last. The settle time is
# the ``SETTLE_PERCENTILE`` latency (latest bound) of the months that ended
# more than ``SETTLE_AGE`` days ago, or ``DEFAULT_SETTLE_DAYS`` when they
# have fewer than ``SETTLE_MIN_REQUESTS`` delivered requests.
SETTLE_PERCENTILE = 0.9
SETTLE_AGE = 120
SETTLE_MIN_REQUESTS = 50
DEFAULT_SETTLE_DAYS = 90

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


def month_key(day: datetime.date) -> str:
    """The ``YYYY-MM`` month of ``day``."""
    return day.strftime("%Y-%m")


def month_end(month: str) -> datetime.date:
    """The last day of a ``YYYY-MM`` month."""
    year, number = (int(part) for part in month.split("-"))
    return datetime.date(year, number, calendar.monthrange(year, number)[1])


def settle_days(
    latest_by_month: Mapping[str, Sequence[int]], today: datetime.date
) -> int:
    """How long a postmark month takes to settle, in days.

    The ``SETTLE_PERCENTILE`` of the latest-bound latencies of the months
    that ended more than ``SETTLE_AGE`` days before ``today``, rounded up;
    ``DEFAULT_SETTLE_DAYS`` when those months have fewer than
    ``SETTLE_MIN_REQUESTS`` delivered requests.
    """
    latencies = [
        days
        for month, values in latest_by_month.items()
        if (today - month_end(month)).days > SETTLE_AGE
        for days in values
    ]
    if len(latencies) < SETTLE_MIN_REQUESTS:
        return DEFAULT_SETTLE_DAYS
    return math.ceil(percentile(latencies, SETTLE_PERCENTILE))


def is_provisional(month: str, today: datetime.date, settle: int) -> bool:
    """Whether a postmark month ended fewer than ``settle`` days before today,
    so letters from it are still being answered and its figures will rise."""
    return (today - month_end(month)).days < settle


@dataclass
class _Month:
    counts: Counter[str] = field(default_factory=Counter)
    latencies: list[Latency] = field(default_factory=list)

    def point(self, month: str, provisional: bool) -> schemas.DeliveryLatencyMonth:
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
            provisional=provisional,
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


@dataclass(frozen=True)
class Summary:
    """The metric's months, oldest first, and the settle time used to mark
    the provisional ones."""

    months: list[schemas.DeliveryLatencyMonth]
    settle_days: int


def _links_by_request(
    links: Iterable[Link], bases: frozenset[str]
) -> dict[RequestKey, list[Link]]:
    """Each request's links whose basis is in ``bases``."""
    by_request: dict[RequestKey, list[Link]] = defaultdict(list)
    for link in links:
        if link.basis in bases:
            by_request[link.key].append(link)
    return by_request


def _cohorts(
    requests: Sequence[FilledRequest],
    by_request: Mapping[RequestKey, list[Link]],
    outcomes: Mapping[int, LabelOutcome],
    filled: Mapping[str, int],
    timezone: ZoneInfo,
) -> dict[str, _Month]:
    """Count and measure each postmark month's requests over their links in
    ``by_request``; ``filled`` gives each month's Filled requests."""
    months: dict[str, _Month] = defaultdict(_Month)
    for key, count in filled.items():
        months[key].counts["filled"] = count
    for request in requests:
        request_links = by_request.get(request.key)
        if not request_links:
            continue
        month = months[month_key(request.postmarked)]
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
    return months


def summarize(  # pylint: disable=too-many-arguments, too-many-positional-arguments
    requests: Iterable[FilledRequest],
    links: Iterable[Link],
    events: Iterable[Event],
    method: schemas.DeliveryMethodEnum = schemas.DeliveryMethodEnum.ALL,
    timezone: ZoneInfo = clock.TIMEZONE,
    today: Optional[datetime.date] = None,
    filled: Optional[Mapping[str, int]] = None,
) -> Summary:
    """The latency metric per postmark month, oldest first.

    Every month with a Filled request is listed, linked or not. Only links
    whose basis ``method`` counts are used, but the settle time is worked
    out over every link, so the methods agree on which months are
    provisional. ``filled`` is the number of Filled requests per month;
    without it, ``requests`` must be every Filled request and are counted.
    ``today`` is ``clock.today()`` by default.
    """
    requests = list(requests)
    links = list(links)
    if today is None:
        today = clock.today()
    if filled is None:
        filled = Counter(month_key(request.postmarked) for request in requests)
    outcomes = label_outcomes(events, timezone)

    def cohorts(bases: frozenset[str]) -> dict[str, _Month]:
        by_request = _links_by_request(links, bases)
        return _cohorts(requests, by_request, outcomes, filled, timezone)

    everything = cohorts(METHOD_BASES[schemas.DeliveryMethodEnum.ALL])
    months = (
        everything
        if method == schemas.DeliveryMethodEnum.ALL
        else cohorts(METHOD_BASES[method])
    )
    settle = settle_days(
        {key: [x.latest for x in month.latencies] for key, month in everything.items()},
        today,
    )
    return Summary(
        [
            months[key].point(key, is_provisional(key, today, settle))
            for key in sorted(months)
        ],
        settle,
    )


@dataclass(frozen=True)
class Rows:
    """What ``load_rows`` reads: the Filled requests per postmark month, the
    linked Filled requests, their links and their labels' delivered and
    returned events."""

    filled: dict[str, int]
    requests: list[FilledRequest]
    links: list[Link]
    events: list[Event]


def _in_range(
    request: type[models.Request],
    start_date: Optional[datetime.date],
    end_date: Optional[datetime.date],
) -> list[sa.ColumnElement[bool]]:
    """Filters for the Filled requests postmarked in the range."""
    filters = [request.action == "Filled"]
    if start_date:
        filters.append(request.date_postmarked >= start_date)
    if end_date:
        filters.append(request.date_postmarked <= end_date)
    return filters


def _first_linked_month_start(
    end_date: Optional[datetime.date], dialect: str
) -> Optional[sa.ColumnElement]:
    """The first day of the month of the earliest postmark of a linked Filled
    request (up to ``end_date``), as SQL; None for a dialect without a
    month function (the months are then only trimmed in Python)."""
    request = aliased(models.Request)
    link = aliased(models.PackageShipment)
    first = (
        sa.select(sa.func.min(request.date_postmarked))
        .select_from(link)
        .join(
            request,
            sa.and_(
                link.inmate_jurisdiction == request.inmate_jurisdiction,
                link.inmate_id == request.inmate_id,
                link.request_index == request.index,
            ),
        )
        .where(*_in_range(request, None, end_date))
        .scalar_subquery()
    )
    if dialect == "sqlite":
        return sa.func.date(first, "start of month")
    if dialect == "postgresql":
        return sa.cast(sa.func.date_trunc("month", first), sa.Date)
    return None


def _rows_statement(
    start_date: Optional[datetime.date],
    end_date: Optional[datetime.date],
    from_first_linked: bool = False,
    dialect: str = "sqlite",
) -> sa.CompoundSelect:
    """The ``load_rows`` statement: the linked Filled requests in the range,
    a row per link per delivered or returned event, then the number of
    Filled requests per postmark date (from the first linked month on, with
    ``from_first_linked``, where the dialect allows)."""
    request, link = models.Request, models.PackageShipment
    shipment, event = models.Shipment, models.TrackingEvent
    in_range = _in_range(request, start_date, end_date)
    counted_range = list(in_range)
    if from_first_linked:
        month_start = _first_linked_month_start(end_date, dialect)
        if month_start is not None:
            counted_range.append(request.date_postmarked >= month_start)

    linked = (
        sa.select(
            sa.literal("link").label("row_kind"),
            request.inmate_jurisdiction,
            request.inmate_id,
            request.index.label("request_index"),
            request.date_postmarked,
            request.date_processed,
            link.shipment_id,
            link.basis,
            shipment.purchased_at,
            event.id.label("event_id"),
            event.occurred_at,
            event.status,
            sa.literal(0).label("filled"),
        )
        .select_from(link)
        .join(
            request,
            sa.and_(
                link.inmate_jurisdiction == request.inmate_jurisdiction,
                link.inmate_id == request.inmate_id,
                link.request_index == request.index,
            ),
        )
        .join(shipment, shipment.id == link.shipment_id)
        .outerjoin(
            event,
            sa.and_(
                event.shipment_id == shipment.id,
                event.status.in_((DELIVERED, RETURN_TO_SENDER)),
            ),
        )
        .where(*in_range)
    )
    counted = (
        sa.select(
            sa.literal("filled"),
            *([sa.null()] * 3),
            request.date_postmarked,
            *([sa.null()] * 7),
            sa.func.count(),  # pylint: disable=not-callable
        )
        .where(*counted_range)
        .group_by(request.date_postmarked)
    )

    return sa.union_all(linked, counted)


async def load_rows(
    session: AsyncSession,
    start_date: Optional[datetime.date] = None,
    end_date: Optional[datetime.date] = None,
    from_first_linked: bool = False,
) -> Rows:
    """Read the Filled requests postmarked in the range: their number per
    postmark date, and the linked ones with every link and their labels'
    delivered and returned events.

    One statement (a ``UNION ALL``), so one consistent read: a sync or link
    rebuild committing meanwhile shows up entirely or not at all. Unlinked
    requests are only counted in SQL, which keeps an all-time read small.
    The linked rows are a request per link per event, deduplicated here.
    With ``from_first_linked``, Filled requests before the month of the first
    linked one may be left uncounted (see ``first_linked_month``).
    """
    requests: dict[RequestKey, FilledRequest] = {}
    links: dict[tuple[RequestKey, int], Link] = {}
    events: dict[int, Event] = {}
    filled: Counter[str] = Counter()
    dialect = session.get_bind().dialect.name
    statement = _rows_statement(start_date, end_date, from_first_linked, dialect)
    for row in (await session.execute(statement)).all():
        if row.row_kind == "filled":
            filled[month_key(row.date_postmarked)] += row.filled
            continue
        key = (row.inmate_jurisdiction, row.inmate_id, row.request_index)
        if key not in requests:
            requests[key] = FilledRequest(key, row.date_postmarked, row.date_processed)
        if (key, row.shipment_id) not in links:
            links[(key, row.shipment_id)] = Link(
                key, row.shipment_id, row.basis, row.purchased_at
            )
        if row.event_id is not None and row.event_id not in events:
            events[row.event_id] = Event(row.shipment_id, row.occurred_at, row.status)
    return Rows(
        dict(filled),
        list(requests.values()),
        list(links.values()),
        list(events.values()),
    )


def first_linked_month(requests: Iterable[FilledRequest]) -> Optional[str]:
    """The postmark month of the earliest of ``requests`` (linked ones, of
    any method), or None for none."""
    return min((month_key(request.postmarked) for request in requests), default=None)


def from_month(summary: Summary, first: Optional[str]) -> Summary:
    """``summary`` without the months before ``first`` (every month for
    None); the settle time is kept."""
    months = [] if first is None else [m for m in summary.months if m.month >= first]
    return Summary(months, summary.settle_days)


async def get_delivery_latency(  # pylint: disable=too-many-arguments
    session: AsyncSession,
    method: schemas.DeliveryMethodEnum = schemas.DeliveryMethodEnum.ALL,
    start_date: Optional[datetime.date] = None,
    end_date: Optional[datetime.date] = None,
    *,
    today: Optional[datetime.date] = None,
    from_first_linked: bool = False,
) -> Summary:
    """The latency metric for Filled requests postmarked in the range.

    With ``from_first_linked`` (for a range with no start), the months
    before the first with a linked request of any method are dropped, so
    the series starts where shipment data does; none at all if nothing is
    linked. The cutoff is the same for every method.
    """
    rows = await load_rows(session, start_date, end_date, from_first_linked)
    summary = summarize(
        rows.requests,
        rows.links,
        rows.events,
        method,
        today=today,
        filled=rows.filled,
    )
    if from_first_linked:
        return from_month(summary, first_linked_month(rows.requests))
    return summary
