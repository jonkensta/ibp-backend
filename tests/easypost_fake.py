"""A fake EasyPost API and hand-built shipment JSON for the sync tests.

Every name, address and number here is invented.
"""

import datetime
import email.message
import io
import json
import urllib.error
import urllib.parse
from typing import Any, Optional

NAME = "Testy Example"
STREET = "100 Example Rd."
ZIPCODE = "77000-1234"


def stamp(moment: datetime.datetime) -> str:
    """An EasyPost timestamp."""
    return moment.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def event(
    when: datetime.datetime,
    status: str,
    detail: str = "status_update",
    message: str = "Update",
    city: Optional[str] = "EXAMPLETON",
) -> dict[str, Any]:
    """One tracking detail, as EasyPost lists it."""
    return {
        "object": "TrackingDetail",
        "message": message,
        "description": "",
        "status": status,
        "status_detail": detail,
        "datetime": stamp(when),
        "datetime_local": when.strftime("%Y-%m-%dT%H:%M:%S-06:00"),
        "source": "USPS",
        "tracking_location": {
            "object": "TrackingLocation",
            "city": city,
            "state": "TX" if city else None,
            "country": None,
            "zip": "77000" if city else None,
        },
        "signed_by": "SOMEONE",
    }


def shipment(
    number: int,
    created: datetime.datetime,
    events: Optional[list[dict]] = None,
    **overrides: Any,
) -> dict[str, Any]:
    """A purchased shipment with its embedded tracker.

    By default it is addressed to an invented person ``NAME #<8 digits>`` at
    the invented unit address; ``overrides`` replace top-level fields, and
    ``name``, ``company``, ``street1``, ``street2`` and ``zip`` the address's.
    """
    address = {
        "object": "Address",
        "name": f"{NAME} #{40000000 + number:08d}",
        "company": None,
        "street1": STREET,
        "street2": None,
        "city": "EXAMPLETON",
        "state": "TX",
        "zip": ZIPCODE,
        "country": "US",
    }
    for field in ("name", "company", "street1", "street2", "zip"):
        if field in overrides:
            address[field] = overrides.pop(field)
    events = events if events is not None else [event(created, "pre_transit")]
    tracker_status = overrides.pop(
        "tracker_status", events[-1]["status"] if events else "unknown"
    )
    data = {
        "id": f"shp_{number:08d}",
        "object": "Shipment",
        "mode": "production",
        "created_at": stamp(created),
        "reference": None,
        "status": tracker_status,
        "tracking_code": f"9400{number:018d}",
        "refund_status": None,
        "to_address": address,
        "parcel": {"object": "Parcel", "weight": 48.0},
        "postage_label": {
            "object": "PostageLabel",
            "created_at": stamp(created + datetime.timedelta(seconds=4)),
        },
        "selected_rate": {
            "object": "Rate",
            "carrier": "USPS",
            "service": "LibraryMail",
            "rate": "5.57",
        },
        "tracker": {
            "id": f"trk_{number:08d}",
            "object": "Tracker",
            "status": tracker_status,
            "signed_by": "SOMEONE",
            "tracking_details": events,
        },
    }
    data.update(overrides)
    return data


class FakeEasyPost:
    """Answers ``GET /shipments``, ``GET /shipments/{id}`` and
    ``GET /trackers/{id}`` like EasyPost.

    Shipments are listed newest first, filtered by ``start_datetime`` and
    ``end_datetime`` (exclusive), paged by ``page_size`` and ``before_id``.
    ``failures`` are HTTP statuses (or (status, Retry-After) pairs) raised,
    in order, before any answer. ``calls`` records (path, params).
    """

    def __init__(self, shipments=(), trackers=None, failures=()):
        self.shipments: list[dict] = list(shipments)
        self.trackers: dict[str, dict] = dict(trackers or {})
        self.failures = list(failures)
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, request, timeout):  # pylint: disable=unused-argument
        assert request.get_method() == "GET"
        assert request.get_header("Authorization", "").startswith("Basic ")
        url = urllib.parse.urlsplit(request.full_url)
        path = url.path.removeprefix("/v2/")
        params = dict(urllib.parse.parse_qsl(url.query))
        self.calls.append((path, params))
        if self.failures:
            failure = self.failures.pop(0)
            code, retry = failure if isinstance(failure, tuple) else (failure, None)
            headers = email.message.Message()
            if retry is not None:
                headers["Retry-After"] = str(retry)
            raise urllib.error.HTTPError(
                request.full_url, code, "error", headers, io.BytesIO(b"{}")
            )
        by_id = {s["id"]: s for s in self.shipments}
        if path == "shipments":
            body = self.list_shipments(params)
        elif path.startswith("shipments/") and path[len("shipments/") :] in by_id:
            body = by_id[path[len("shipments/") :]]
        elif path.startswith("trackers/") and path[len("trackers/") :] in self.trackers:
            body = self.trackers[path[len("trackers/") :]]
        else:
            raise urllib.error.HTTPError(
                request.full_url,
                404,
                "not found",
                email.message.Message(),
                io.BytesIO(b'{"error": {"code": "NOT_FOUND"}}'),
            )
        return io.BytesIO(json.dumps(body).encode())

    def list_shipments(self, params: dict[str, str]) -> dict:
        """One page of the shipment list."""
        start, end = params["start_datetime"], params["end_datetime"]
        listed = sorted(
            (s for s in self.shipments if start < s["created_at"] < end),
            key=lambda s: (s["created_at"], s["id"]),
            reverse=True,
        )
        if "before_id" in params:
            ids = [s["id"] for s in listed]
            listed = listed[ids.index(params["before_id"]) + 1 :]
        size = int(params.get("page_size", 20))
        return {"shipments": listed[:size], "has_more": len(listed) > size}

    def listing_windows(self) -> list[tuple[str, str]]:
        """The (start, end) of each first-page listing request."""
        return [
            (params["start_datetime"], params["end_datetime"])
            for path, params in self.calls
            if path == "shipments" and "before_id" not in params
        ]
