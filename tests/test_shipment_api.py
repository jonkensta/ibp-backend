"""GET /requests/{code}/shipments: a request's linked labels and events."""

from ibp import models

from .shipment_helpers import add_inmate, add_request, add_shipment, add_unit

NUMBER = 12345678


def seed(api_db) -> tuple[str, str]:
    """A request linked to an individual label and to a bulk box; returns
    their tracking codes. The links are written directly (the link rules
    give a request with its own label no box)."""
    with api_db() as session:
        add_unit(session, "ALPHA")
        add_inmate(session, NUMBER, "ALPHA")
        add_request(session, NUMBER, 0, "2026-03-01")
        add_request(session, NUMBER, 1, "2026-03-20")
        label = add_shipment(
            session,
            "2026-03-03T15:00",
            inmate_number=f"{NUMBER:08d}",
            tracker_status="delivered",
            events=(
                ("2026-03-03T15:01", "pre_transit"),
                ("2026-03-04T10:00", "in_transit"),
                ("2026-03-06T17:30", "delivered"),
            ),
        )
        box = add_shipment(
            session,
            "2026-03-02T16:00",
            kind="bulk",
            unit_jurisdiction="Texas",
            unit_name="ALPHA",
            tracker_status="in_transit",
            events=(("2026-03-03T09:00", "in_transit"),),
        )
        session.flush()
        for shipment, basis in ((label, "inmate_number"), (box, "unit_session")):
            session.add(
                models.PackageShipment(
                    inmate_jurisdiction="Texas",
                    inmate_id=NUMBER,
                    request_index=0,
                    shipment_id=shipment.id,
                    basis=basis,
                )
            )
        session.commit()
        return label.tracking_code, box.tracking_code


def test_request_shipments_shape(client, api_db):
    """Linked labels come oldest first with basis, status and events."""
    label_code, box_code = seed(api_db)

    response = client.get(f"/requests/TEX-{NUMBER:08d}-0/shipments")

    assert response.status_code == 200
    box, label = response.json()
    assert box == {
        "tracking_code": box_code,
        "purchased_at": "2026-03-02T16:00:00+00:00",
        "kind": "bulk",
        "basis": "unit_session",
        "tracker_status": "in_transit",
        "events": [
            {
                "occurred_at": "2026-03-03T09:00:00+00:00",
                "local_time": None,
                "status": "in_transit",
                "status_detail": None,
                "message": None,
                "city": None,
                "state": None,
                "zip": None,
            }
        ],
    }
    assert label["tracking_code"] == label_code
    assert (label["kind"], label["basis"], label["tracker_status"]) == (
        "individual",
        "inmate_number",
        "delivered",
    )
    assert [event["status"] for event in label["events"]] == [
        "pre_transit",
        "in_transit",
        "delivered",
    ]


def test_request_without_links_has_no_shipments(client, api_db):
    """A known request with no linked label gives an empty list."""
    seed(api_db)
    response = client.get(f"/requests/TEX-{NUMBER:08d}-1/shipments")
    assert response.status_code == 200
    assert response.json() == []


def test_unknown_request_is_404(client, api_db):
    """An unknown or malformed request code is a 404."""
    seed(api_db)
    for code in (f"TEX-{NUMBER:08d}-7", "TEX-00000001-0", "not-a-code"):
        response = client.get(f"/requests/{code}/shipments")
        assert response.status_code == 404
        assert response.json() == {"detail": "Request not found."}
