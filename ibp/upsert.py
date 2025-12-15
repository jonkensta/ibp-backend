"""IBP inmate provider upsert methods."""

from sqlalchemy.future import select

from . import models, providers
from .base import config


async def _build_inmate_from_response(session, response: providers.QueryResult):
    """Create an Inmate instance from a provider response."""
    kwargs = response.model_dump()

    # Check if the unit exists in the database
    unit_exists = (
        await session.execute(
            select(models.Unit.name)
            .where(models.Unit.name == kwargs["unit"])
            .where(models.Unit.jurisdiction == kwargs["jurisdiction"])
        )
    ).scalar_one_or_none()

    inmate_data = {
        "id": kwargs["id"],
        "jurisdiction": kwargs["jurisdiction"],
        "first_name": kwargs["first_name"],
        "last_name": kwargs["last_name"],
        # Only set unit_name if the unit actually exists to avoid FK violation
        "unit_name": kwargs["unit"] if unit_exists else None,
        "race": kwargs.get("race"),
        "sex": kwargs.get("sex"),
        "release": kwargs.get("release"),
        "url": kwargs.get("url"),
        "datetime_fetched": kwargs.get("datetime_fetched"),
    }

    return models.Inmate(**inmate_data)


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
