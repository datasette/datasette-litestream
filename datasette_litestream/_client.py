"""
Client for the litestream control socket (litestream >= 0.5).

litestream's ``replicate`` daemon can expose a Unix-domain control socket when
started with ``socket.enabled: true`` in its config. The socket speaks plain
HTTP/1.1 with JSON request/response bodies. This module is a thin synchronous
wrapper over that API so the Datasette plugin can register/unregister databases
at runtime without restarting the daemon.

See litestream ``server.go`` for the endpoint definitions:

    POST /register     {"path", "replica_url"}        -> {"status", "path"}
    POST /unregister   {"path", "timeout"}            -> {"status", "path", "txid"}
    POST /start        {"path", "timeout"}            -> {"status", "path", "txid"}
    POST /stop         {"path", "timeout"}            -> {"status", "path", "txid"}
    POST /sync         {"path", "wait", "timeout"}    -> {"status", "path", ...}
    GET  /list                                        -> {"databases": [...]}
    GET  /info                                        -> {"version", "pid", ...}
    GET  /txid?path=...                               -> {"txid"}

Errors are returned as ``{"error": "...", "details": "..."}`` with a non-2xx
HTTP status; we surface those as :class:`LitestreamControlError`.
"""

import httpx


class LitestreamControlError(Exception):
    """Raised when the litestream control socket returns an error response."""

    def __init__(self, message, status_code=None, details=None):
        super().__init__(message)
        self.status_code = status_code
        self.details = details


class LitestreamClient:
    """Synchronous client for a litestream control socket at ``socket_path``."""

    def __init__(self, socket_path, timeout=30.0):
        self.socket_path = str(socket_path)
        self.timeout = timeout

    def _client(self):
        # httpx routes every request over the Unix socket via the uds transport.
        # base_url host is ignored but required for a valid URL.
        return httpx.Client(
            transport=httpx.HTTPTransport(uds=self.socket_path),
            base_url="http://litestream",
            timeout=self.timeout,
        )

    def _request(self, method, path, *, json_body=None, params=None):
        with self._client() as client:
            response = client.request(
                method, path, json=json_body, params=params
            )
        if response.status_code >= 400:
            error = None
            details = None
            try:
                payload = response.json()
                error = payload.get("error")
                details = payload.get("details")
            except Exception:
                error = response.text
            raise LitestreamControlError(
                error or f"litestream control socket returned {response.status_code}",
                status_code=response.status_code,
                details=details,
            )
        if not response.content:
            return {}
        return response.json()

    # --- Read endpoints ---------------------------------------------------

    def info(self):
        """Daemon metadata: version, pid, uptime_seconds, started_at, database_count."""
        return self._request("GET", "/info")

    def list_databases(self):
        """Return the list of databases the daemon currently manages."""
        return self._request("GET", "/list").get("databases", [])

    def txid(self, path):
        return self._request("GET", "/txid", params={"path": str(path)})

    # --- Write endpoints --------------------------------------------------

    def register(self, path, replica_url):
        """Register ``path`` for replication to ``replica_url``.

        Returns the daemon response, e.g. ``{"status": "registered", "path": ...}``.
        ``status`` is ``"already_registered"`` if the database was already known.
        """
        return self._request(
            "POST",
            "/register",
            json_body={"path": str(path), "replica_url": replica_url},
        )

    def unregister(self, path, timeout=None):
        """Unregister ``path``; the daemon performs a final sync before removing it.

        Returns ``{"status", "path", "txid"}``. ``status`` is
        ``"already_unregistered"`` if the database was not managed.
        """
        body = {"path": str(path)}
        if timeout is not None:
            body["timeout"] = int(timeout)
        return self._request("POST", "/unregister", json_body=body)

    def start(self, path, timeout=None):
        body = {"path": str(path)}
        if timeout is not None:
            body["timeout"] = int(timeout)
        return self._request("POST", "/start", json_body=body)

    def stop(self, path, timeout=None):
        body = {"path": str(path)}
        if timeout is not None:
            body["timeout"] = int(timeout)
        return self._request("POST", "/stop", json_body=body)

    def sync(self, path, wait=False, timeout=None):
        body = {"path": str(path), "wait": bool(wait)}
        if timeout is not None:
            body["timeout"] = int(timeout)
        return self._request("POST", "/sync", json_body=body)
