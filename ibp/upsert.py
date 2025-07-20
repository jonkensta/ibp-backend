"""IBP inmate provider upsert methods."""

from sqlalchemy.future import select

from . import models, providers
from .base import config


async def _build_inmate_from_response(session, response):
    """Create an Inmate instance from a provider response."""
    kwargs = response.model_dump()

    unit = (
        await session.execute(
            select(models.Unit).where(models.Unit.name == kwargs["unit"])
        )
    ).scalar_one_or_none()

    kwargs["unit"] = unit

    return models.Inmate(**kwargs)


async def inmates_by_inmate_id(session, id_: int) -> list[str]:
    """Upsert from inmate providers by inmate id."""
    timeout = config.getfloat("providers", "timeout")

    # pylint: disable=no-value-for-parameter
    responses, errors = await providers.query_by_inmate_id(id_, timeout=timeout)

    async with session.begin_nested():
        for response in responses:
            inmate = await _build_inmate_from_response(session, response)
            await session.merge(inmate)

    return errors


async def inmates_by_name(session, first_name: str, last_name: str):
    """Upsert from inmate providers by name."""
    timeout = config.getfloat("providers", "timeout")

    # pylint: disable=no-value-for-parameter
    responses, errors = await providers.query_by_name(
        first_name, last_name, timeout=timeout
    )

    async with session.begin_nested():
        for response in responses:
            inmate = await _build_inmate_from_response(session, response)
            await session.merge(inmate)

    return errors
