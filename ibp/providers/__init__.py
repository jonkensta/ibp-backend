"""IBP inmate search utility."""

from .base import query_by_inmate_id, query_by_name
from .types import QueryResult

__all__ = ["query_by_inmate_id", "query_by_name", "QueryResult"]
