"""Request label endpoints: the PNG for the print server, the PDF for browsers."""

import io

from fastapi import Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from . import models, schemas
from .api import get_request_with_unit, get_session
from .base import app
from .labels import label_package_id, render_request_label, render_request_label_pdf


async def load_label_request(
    session: AsyncSession,
    jurisdiction: schemas.JurisdictionEnum,
    inmate_id: int,
    request_index: int,
) -> models.Request:
    """Load a request with its inmate and unit for label rendering, or 404."""
    request = await get_request_with_unit(
        session, jurisdiction, inmate_id, request_index
    )
    if request is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Request not found.")
    return request


@app.get(
    "/inmates/{jurisdiction}/{inmate_id}/requests/{request_index}/label",
    response_class=Response,
)
async def get_request_label(
    jurisdiction: schemas.JurisdictionEnum,
    inmate_id: int,
    request_index: int,
    session: AsyncSession = Depends(get_session),
):
    """Return a PNG label image for a request."""
    request = await load_label_request(session, jurisdiction, inmate_id, request_index)
    image = render_request_label(request)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return Response(content=buffer.getvalue(), media_type="image/png")


@app.get(
    "/inmates/{jurisdiction}/{inmate_id}/requests/{request_index}/label.pdf",
    response_class=Response,
)
async def get_request_label_pdf(
    jurisdiction: schemas.JurisdictionEnum,
    inmate_id: int,
    request_index: int,
    session: AsyncSession = Depends(get_session),
):
    """Return a request label as a one-page PDF sized exactly to the label.

    Browsers print a PDF at its own page size, so this is the reliable
    fallback when the print server is unavailable.
    """
    request = await load_label_request(session, jurisdiction, inmate_id, request_index)
    filename = f"{label_package_id(request)}.pdf"
    return Response(
        content=render_request_label_pdf(request),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )
