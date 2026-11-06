"""Tests for scripts/label-refund/ibp-label-refund (label selection only)."""

import importlib.machinery
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "label-refund"
    / "ibp-label-refund"
)
_loader = importlib.machinery.SourceFileLoader("ibp_label_refund", str(SCRIPT))
_spec = importlib.util.spec_from_loader("ibp_label_refund", _loader)
assert _spec is not None
refund = importlib.util.module_from_spec(_spec)
sys.modules["ibp_label_refund"] = refund  # dataclasses look the module up
_loader.exec_module(refund)

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)


def shipment(days_old: float, **overrides):
    s = {
        "id": f"shp_{days_old}",
        "mode": "production",
        "created_at": (NOW - timedelta(days=days_old)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tracking_code": "9400",
        "selected_rate": {"carrier": "USPS", "rate": "5.57"},
        "refund_status": None,
        "tracker": {
            "id": "trk_1",
            "status": "pre_transit",
            "tracking_details": [{"status": "pre_transit"}],
        },
    }
    s.update(overrides)
    return s


def ids(shipments):
    picked, skipped = refund.first_pass(shipments, NOW)
    return [p.shipment_id for p in picked], skipped


def test_picks_unscanned_label_in_window():
    picked, _ = ids([shipment(14.5), shipment(28.9)])
    assert picked == ["shp_14.5", "shp_28.9"]


def test_skips_labels_outside_14_to_29_days():
    picked, skipped = ids([shipment(13.9), shipment(29.1)])
    assert picked == []
    assert skipped["out_of_window"] == 2


def test_skips_scanned_label():
    scanned = shipment(
        20, tracker={"id": "t", "status": "in_transit", "tracking_details": []}
    )
    scanned_detail = shipment(
        21,
        tracker={
            "id": "t",
            "status": "pre_transit",
            "tracking_details": [{"status": "in_transit"}],
        },
    )
    picked, skipped = ids([scanned, scanned_detail])
    assert picked == []
    assert skipped["scanned"] == 2


def test_skips_refund_already_requested_or_rejected():
    picked, skipped = ids(
        [shipment(20, refund_status=r) for r in ("submitted", "refunded", "rejected")]
    )
    assert picked == []
    assert skipped["refund_requested"] == 3


def test_skips_unbought_test_non_usps_and_trackerless():
    picked, skipped = ids(
        [
            shipment(20, tracking_code=None),
            shipment(20, mode="test"),
            shipment(20, selected_rate={"carrier": "UPS", "rate": "9"}),
            shipment(20, tracker=None),
        ]
    )
    assert picked == []
    assert skipped["not_usps"] == 1 and skipped["no_tracker"] == 1


def test_refuses_over_the_limit(monkeypatch):
    labels = [shipment(20, id=f"shp_{i}") for i in range(refund.MAX_REFUNDS + 1)]
    posts = []

    def fake_request(method, path, key, params=None):
        if method == "POST":
            posts.append(path)
            return {"refund_status": "submitted"}
        if path == "shipments":
            return {"shipments": labels, "has_more": False}
        return {"status": "pre_transit", "tracking_details": []}

    monkeypatch.setattr(refund, "request", fake_request)
    monkeypatch.setattr(refund.time, "sleep", lambda s: None)
    assert refund.run("EZAK", apply=True, log=[]) == 2
    assert posts == []


def test_dry_run_never_posts(monkeypatch):
    posts = []

    def fake_request(method, path, key, params=None):
        if method == "POST":
            posts.append(path)
        if path == "shipments":
            return {"shipments": [shipment(20)], "has_more": False}
        return {"status": "pre_transit", "tracking_details": []}

    monkeypatch.setattr(refund, "request", fake_request)
    monkeypatch.setattr(refund.time, "sleep", lambda s: None)
    log: list[str] = []
    assert refund.run("EZAK", apply=False, log=log) == 0
    assert posts == []
    assert "1 never scanned" in log[0]


def test_apply_posts_and_records(monkeypatch, tmp_path):
    posts = []

    def fake_request(method, path, key, params=None):
        if method == "POST":
            posts.append(path)
            return {"refund_status": "submitted"}
        if path == "shipments":
            return {"shipments": [shipment(20)], "has_more": False}
        return {"status": "pre_transit", "tracking_details": []}

    monkeypatch.setattr(refund, "request", fake_request)
    monkeypatch.setattr(refund, "STATE", tmp_path)
    monkeypatch.setattr(refund.time, "sleep", lambda s: None)
    assert refund.run("EZAK", apply=True, log=[]) == 0
    assert posts == ["shipments/shp_20/refund"]
    assert "shp_20" in (tmp_path / "refunds.csv").read_text()


def test_fresh_tracker_scan_prevents_refund(monkeypatch):
    posts = []

    def fake_request(method, path, key, params=None):
        if method == "POST":
            posts.append(path)
        if path == "shipments":
            return {"shipments": [shipment(20)], "has_more": False}
        return {
            "status": "in_transit",
            "tracking_details": [],
        }  # scanned since the list was cached

    monkeypatch.setattr(refund, "request", fake_request)
    monkeypatch.setattr(refund.time, "sleep", lambda s: None)
    assert refund.run("EZAK", apply=True, log=[]) == 0
    assert posts == []
