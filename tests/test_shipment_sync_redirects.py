"""The EasyPost client refuses redirects, against a local stand-in server."""

# pylint: disable=redefined-outer-name

import http.server
import threading
from typing import Optional

import pytest

from ibp import shipment_sync
from ibp.shipment_sync import EasyPost

KEY = "EZAK" + "0" * 28


@pytest.fixture
def local_easypost(monkeypatch):
    """A local HTTP server standing in for EasyPost: ``/v2/shipments``
    redirects with the status in ``answer``, anything else answers ``{}``.
    Yields the (path, Authorization header) pairs it was asked for, and
    ``answer``."""
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.setenv(name, "*")  # never send these through a proxy
    asked: list[tuple[str, Optional[str]]] = []
    answer = {"status": 302}

    class Handler(http.server.BaseHTTPRequestHandler):
        """Redirects the listing; answers anything else."""

        def do_GET(self):  # pylint: disable=invalid-name
            """Answer a GET."""
            asked.append((self.path, self.headers.get("Authorization")))
            if self.path.startswith("/v2/shipments"):
                self.send_response(answer["status"])
                self.send_header("Location", "/elsewhere")
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

        def log_message(self, *args):  # pylint: disable=arguments-differ
            """Keep quiet."""

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(
        shipment_sync, "API", f"http://127.0.0.1:{server.server_address[1]}/v2"
    )
    try:
        yield asked, answer
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirect_is_refused(local_easypost, status):
    """A redirect fails as an ApiError and is not followed, so the key's
    Authorization header goes nowhere else."""
    asked, answer = local_easypost
    answer["status"] = status
    client = EasyPost(KEY, sleep=lambda _: None)
    assert client.get("trackers/trk_1") == {}  # the default opener works
    with pytest.raises(shipment_sync.ApiError, match=f"HTTP {status}"):
        client.get("shipments")
    assert [path for path, _ in asked] == ["/v2/trackers/trk_1", "/v2/shipments"]
    assert all(auth and auth.startswith("Basic ") for _, auth in asked)
