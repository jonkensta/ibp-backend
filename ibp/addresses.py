"""Request shipping addresses, and the shipping settings they use.

Used by the request-address endpoints in ``ibp/request_api.py`` and their
deprecated aliases in ``ibp/api.py``.
"""

import logging
from typing import Awaitable, Callable, Optional, cast

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from . import models, providers, schemas
from .labels import label_package_id
from .upsert import refresh_inmate as upsert_refresh_inmate

logger = logging.getLogger(__name__)


def format_addressee(inmate: models.Inmate) -> str:
    """Format an inmate's addressee line, e.g. ``Jane Doe #00012345``."""
    parts = [part.title() for part in (inmate.first_name, inmate.last_name) if part]
    return " ".join([*parts, f"#{inmate.id:08d}"])


async def refresh_stale_inmate(
    session: AsyncSession, inmate: models.Inmate
) -> tuple[schemas.InmateStatusEnum, Optional[str]]:
    """Re-query a stale inmate from its own provider before it is addressed.

    Must run inside a transaction. A fresh inmate (``entry_is_fresh``, the
    ``[warnings] inmates_cache_ttl`` rule) is left alone. Otherwise the
    inmate row is locked and reloaded (as in ``api._record_lookup_once``;
    SQLite ignores the lock, and a concurrent refresh just merges the same
    answer twice) and rechecked, then re-queried; a listed inmate is reloaded
    again with its ``unit``, so a transfer is picked up.

    Returns the inmate's status and, after a refresh, the unit name the
    provider reported (which may be one the ``units`` table lacks, leaving
    ``inmate.unit`` None).
    """
    if inmate.entry_is_fresh():
        return schemas.InmateStatusEnum.CURRENT, None

    # Reloads ``inmate`` itself (populate_existing), with its unit.
    reload = (
        select(models.Inmate)
        .where(
            models.Inmate.jurisdiction == inmate.jurisdiction,
            models.Inmate.id == inmate.id,
        )
        .options(selectinload(models.Inmate.unit))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    await session.execute(reload)
    if inmate.entry_is_fresh():
        return schemas.InmateStatusEnum.CURRENT, None

    logger.debug("addressed inmate is stale; querying its provider")
    jurisdiction = cast(providers.Jurisdiction, inmate.jurisdiction)
    match, errors = await upsert_refresh_inmate(session, jurisdiction, inmate.id)

    if match is None:
        if errors:
            logger.warning("provider query failed; addressing from stored data")
            return schemas.InmateStatusEnum.UNVERIFIED, None
        logger.info("provider does not list the addressed inmate")
        return schemas.InmateStatusEnum.NOT_LISTED, None

    await session.execute(reload)
    return schemas.InmateStatusEnum.CURRENT, match.unit


async def address_request(
    session: AsyncSession,
    load: Callable[[AsyncSession], Awaitable[models.Request]],
) -> schemas.RequestAddress:
    """Return a request's shipping address, refreshing a stale inmate first.

    ``load`` returns the request with its inmate and the inmate's unit loaded
    (or raises a 404). It runs in one transaction with the refresh (see
    ``refresh_stale_inmate``). The address is built before the commit (which
    expires the loaded rows), but a 409 from it is raised only after, so the
    refresh is kept either way.
    """
    async with session.begin():
        request = await load(session)
        inmate_status, reported_unit = await refresh_stale_inmate(
            session, request.inmate
        )
        settings = await load_shipping_settings(session)
        try:
            return build_request_address(
                request, settings, inmate_status, reported_unit
            )
        except HTTPException as error:
            conflict = error

    raise conflict


def build_request_address(
    request: models.Request,
    settings: schemas.ShippingSettings,
    inmate_status: schemas.InmateStatusEnum,
    reported_unit: Optional[str] = None,
) -> schemas.RequestAddress:
    """Build a request's shipping address from its inmate and unit.

    The request's inmate and the inmate's unit must already be loaded.  An
    inmate without a unit cannot be shipped to, so that is a 409 error; if
    the provider just reported a unit (``reported_unit``) that IBP's units
    table lacks, the detail names it. ``settings`` supplies the unit mailroom
    recipient line.
    """
    inmate = request.inmate
    unit = inmate.unit

    if unit is None:
        if reported_unit:
            detail = (
                f"{inmate.jurisdiction} inmate #{inmate.id:08d} is listed at unit "
                f"'{reported_unit}', which is not in IBP's units list; add the "
                "unit to address a package."
            )
        else:
            detail = (
                f"{inmate.jurisdiction} inmate #{inmate.id:08d} is not assigned "
                "to a unit; cannot address a package."
            )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)

    return schemas.RequestAddress(
        name=format_addressee(inmate),
        first_name=inmate.first_name,
        last_name=inmate.last_name,
        jurisdiction=schemas.JurisdictionEnum(inmate.jurisdiction),
        id=inmate.id,
        datetime_fetched=inmate.datetime_fetched,
        inmate_status=inmate_status,
        request_index=request.index,
        request_id=request.request_id,
        code=label_package_id(request),
        action=schemas.ActionEnum(request.action),
        unit_name=unit.name,
        unit_recipient_name=settings.unit_recipient_name,
        street1=unit.street1,
        street2=unit.street2,
        city=unit.city,
        state=unit.state,
        zipcode=unit.zipcode,
        shipping_method=(
            schemas.ShippingMethodEnum(unit.shipping_method)
            if unit.shipping_method
            else None
        ),
    )


def shipping_settings_to_schema(
    row: Optional[models.ShippingSettings],
) -> schemas.ShippingSettings:
    """Convert the settings row (or the defaults, if missing) to its schema."""
    values = (
        {column: getattr(row, column) for column in models.DEFAULT_SHIPPING_SETTINGS}
        if row is not None
        else models.DEFAULT_SHIPPING_SETTINGS
    )
    return schemas.ShippingSettings.model_validate(
        {
            "return_address": {
                "name": values["return_name"],
                "street1": values["return_street1"],
                "street2": values["return_street2"],
                "city": values["return_city"],
                "state": values["return_state"],
                "zipcode": values["return_zipcode"],
            },
            "unit_recipient_name": values["unit_recipient_name"],
        }
    )


async def load_shipping_settings(session: AsyncSession) -> schemas.ShippingSettings:
    """Load the shipping settings, falling back to the defaults if unset.

    The row is seeded by the migration and by ``ibp.db_bootstrap``; the
    fallback only matters for a schema built by ``create_all`` alone.
    """
    row = await session.get(models.ShippingSettings, models.SHIPPING_SETTINGS_ID)
    return shipping_settings_to_schema(row)
