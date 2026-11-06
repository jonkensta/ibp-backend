"""Tests for the request label endpoints (PNG and PDF)."""

# pylint: disable=redefined-outer-name

import datetime
import io
import re

import pytest
from PIL import Image

from ibp import models

FETCHED = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
MM_PER_POINT = 25.4 / 72


@pytest.fixture
def seeded(api_db):
    """Seed a unit, an inmate in it, and one request."""
    with api_db.begin() as session:
        session.add(
            models.Unit(
                jurisdiction="Texas",
                name="Alpha",
                street1="1 Main St",
                city="Austin",
                state="TX",
                zipcode="78701",
                shipping_method="Box",
            )
        )
        session.add(
            models.Inmate(
                jurisdiction="Texas",
                id=12345,
                first_name="JANE",
                last_name="DOE",
                unit_name="Alpha",
                datetime_fetched=FETCHED,
            )
        )
        session.add(
            models.Request(
                inmate_jurisdiction="Texas",
                inmate_id=12345,
                index=0,
                action="Filled",
                date_processed=datetime.date(2026, 10, 2),
                date_postmarked=datetime.date(2026, 9, 28),
            )
        )
    return api_db


def media_box_mm(pdf: bytes) -> tuple[float, float]:
    """Return the (width, height) in mm of a single-page PDF's MediaBox."""
    boxes = re.findall(rb"/MediaBox\s*\[\s*([\d.\s]+)\]", pdf)
    assert len(boxes) == 1, boxes
    x0, y0, x1, y1 = (float(v) for v in boxes[0].split())
    return (x1 - x0) * MM_PER_POINT, (y1 - y0) * MM_PER_POINT


@pytest.mark.usefixtures("seeded")
def test_label_png(client):
    """The PNG label is unchanged: 1004x378 px, 85x32 mm at 300 DPI."""
    response = client.get("/requests/TEX-12345-0/label")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(response.content)).size == (1004, 378)


@pytest.mark.usefixtures("seeded")
def test_label_pdf_is_one_page_at_label_size(client):
    """The PDF label is a single page sized exactly 85 x 32 mm."""
    response = client.get("/requests/TEX-12345-0/label.pdf")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"] == (
        'inline; filename="TEX-00012345-0.pdf"'
    )

    pdf = response.content
    assert pdf.startswith(b"%PDF-")
    assert re.search(rb"/Count\s+1\b", pdf)
    width, height = media_box_mm(pdf)
    assert width == pytest.approx(85, abs=0.05)
    assert height == pytest.approx(32, abs=0.05)


@pytest.mark.usefixtures("seeded")
def test_label_pdf_image_fills_page(client):
    """The page holds the full-resolution label image with no margins."""
    pdf = client.get("/requests/TEX-12345-0/label.pdf").content
    assert re.search(rb"/Width\s+1004\b", pdf)
    assert re.search(rb"/Height\s+378\b", pdf)
    # The content stream scales the image to the full MediaBox at the origin.
    width_pt, height_pt = (v / MM_PER_POINT for v in media_box_mm(pdf))
    match = re.search(rb"q\s+([\d.]+)\s+0\s+0\s+([\d.]+)\s+0\s+0\s+cm", pdf)
    assert match, "image placement not found"
    assert float(match.group(1)) == pytest.approx(width_pt, abs=0.01)
    assert float(match.group(2)) == pytest.approx(height_pt, abs=0.01)


@pytest.mark.usefixtures("seeded")
@pytest.mark.parametrize("suffix", ["label", "label.pdf"])
def test_label_by_lowercase_padded_code(client, suffix):
    """Any spelling of the code finds the request; the PDF keeps the canonical name."""
    response = client.get(f"/requests/tex-00012345-0/{suffix}")
    assert response.status_code == 200
    if suffix == "label.pdf":
        assert (
            'filename="TEX-00012345-0.pdf"' in response.headers["content-disposition"]
        )


@pytest.mark.usefixtures("seeded")
@pytest.mark.parametrize("suffix", ["label", "label.pdf"])
@pytest.mark.parametrize(
    "code",
    ["TEX-00012345-7", "FED-00012345-0", "TEX-00099999-0", "TEX-12345", "nonsense"],
)
def test_label_unknown_request(client, suffix, code):
    """Unknown and malformed codes are a 404 for both label formats."""
    response = client.get(f"/requests/{code}/{suffix}")
    assert response.status_code == 404
    assert response.json()["detail"] == "Request not found."


@pytest.mark.usefixtures("seeded")
@pytest.mark.parametrize("suffix", ["label", "label.pdf"])
def test_old_label_paths_are_gone(client, suffix):
    """The label moved to /requests/{code}; the old inmate path is a 404."""
    response = client.get(f"/inmates/Texas/12345/requests/0/{suffix}")
    assert response.status_code == 404
