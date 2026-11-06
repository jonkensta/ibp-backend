"""The HTTP clients must not log request URLs (they can carry API keys)."""

import logging

from ibp.base import configure_external_loggers


def test_http_client_request_logs_are_quiet():
    configure_external_loggers([])
    for name in ("httpx", "httpx2", "httpcore"):
        assert not logging.getLogger(name).isEnabledFor(logging.INFO)
