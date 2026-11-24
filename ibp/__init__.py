"""IBP FastAPI application."""

from . import (
    api,
    book_api,
    metrics_api,
    models,
    package_api,
    request_api,
    shipment_api,
)
from .base import app, config
