"""Request endpoints, addressed by request code.

A request's code is the text on its label, e.g. ``TEX-1234567-2``
(jurisdiction, inmate number, request index; see ``labels.parse_request_code``).
A malformed code is a 404, like an unknown one. Requests are created at
``POST /inmates/{jurisdiction}/{inmate_id}/requests`` (``ibp/api.py``), whose
response includes the code.
"""

import io
import logging

from fastapi import Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from . import models, schemas
from .api import (
    address_by_legacy_request_id,
    build_request_address,
    get_session,
    inmate_by_legacy_request_id,
    load_shipping_settings,
)
from .base import app
from .labels import (
    label_package_id,
    parse_request_code,
    render_request_label,
    render_request_label_pdf,
)

logger = logging.getLogger(__name__)


async def load_request(session: AsyncSession, code: str, *options) -> models.Request:
    """Return the request with a request code, or 404 if malformed or unknown."""
    key = parse_request_code(code)
    if key is not None:
        request = (
            await session.execute(
                select(models.Request)
                .where(
                    models.Request.inmate_jurisdiction == key.jurisdiction,
                    models.Request.inmate_id == key.inmate_id,
                    models.Request.index == key.index,
                )
                .options(*options)
            )
        ).scalar_one_or_none()
        if request is not None:
            return request

    raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Request not found.")


async def load_request_with_unit(session: AsyncSession, code: str) -> models.Request:
    """Return a request with its inmate and the inmate's unit loaded, or 404."""
    return await load_request(
        session,
        code,
        selectinload(models.Request.inmate).selectinload(models.Inmate.unit),
    )


# /requests/legacy/... is declared before /requests/{code}/... so that a
# future /requests/{code}/{suffix} route can never shadow it.
@app.get("/requests/legacy/{legacy_id}/inmate", response_model=schemas.Inmate)
async def get_inmate_by_legacy_id(
    legacy_id: int,
    session: AsyncSession = Depends(get_session),
):
    """Get the inmate of a request by its legacy request ID (``request_id``).

    For labels printed by the old system, which carry that number. Read-only.
    """
    return await inmate_by_legacy_request_id(session, legacy_id)


@app.get("/requests/legacy/{legacy_id}/address", response_model=schemas.RequestAddress)
async def get_address_by_legacy_id(
    legacy_id: int,
    session: AsyncSession = Depends(get_session),
):
    """Get the shipping address of a request by its legacy request ID.

    Read-only: does not record a lookup or query the inmate providers.
    """
    return await address_by_legacy_request_id(session, legacy_id)


@app.delete("/requests/{code}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_request(code: str, session: AsyncSession = Depends(get_session)):
    """Delete a request. Its index is never reused."""
    async with session.begin():
        request = await load_request(session, code)
        logger.debug("deleting request")
        await session.delete(request)


@app.get("/requests/{code}/address", response_model=schemas.RequestAddress)
async def get_request_address(code: str, session: AsyncSession = Depends(get_session)):
    """Get the shipping address for a request.

    Read-only: does not record a lookup or query the inmate providers, so it
    is safe to call once per shipped package. An inmate without a unit cannot
    be addressed (409).
    """
    request = await load_request_with_unit(session, code)
    return build_request_address(request, await load_shipping_settings(session))


@app.get("/requests/{code}/label", response_class=Response)
async def get_request_label(code: str, session: AsyncSession = Depends(get_session)):
    """Return a PNG label image for a request (for the print server)."""
    request = await load_request_with_unit(session, code)
    image = render_request_label(request)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return Response(content=buffer.getvalue(), media_type="image/png")


@app.get("/requests/{code}/label.pdf", response_class=Response)
async def get_request_label_pdf(
    code: str, session: AsyncSession = Depends(get_session)
):
    """Return a request label as a one-page PDF sized exactly to the label.

    Browsers print a PDF at its own page size, so this is the reliable
    fallback when the print server is unavailable.
    """
    request = await load_request_with_unit(session, code)
    filename = f"{label_package_id(request)}.pdf"
    return Response(
        content=render_request_label_pdf(request),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )
