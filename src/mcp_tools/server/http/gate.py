"""The outermost ASGI layer of the HTTP server: default deny.

Why a gate of our own, in front of everything: the SDK's custom routes sit outside its
authentication, its DNS-rebinding check and its body limit, and its built-in auth assumes
an OAuth provider. The gate is pure ASGI and never touches `receive` or `send` of a request
it lets through, so streaming responses and disconnect detection keep working.
"""

import hashlib
import hmac
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass

from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Receive, Scope, Send

MCP_PATH = "/mcp"
HEALTH_PATH = "/healthz"
_FILE = re.compile(r"^/files/med_[0-9a-z]{26}$")
_UPLOAD = re.compile(r"^/upload/med_[0-9a-z]{26}$")
_METHODS = {
    "mcp": {"GET", "POST", "DELETE"},
    "health": {"GET"},
    "file": {"GET", "HEAD"},
    "upload": {"PUT"},
}
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
WILDCARD_HOSTS = ("0.0.0.0", "::")  # noqa: S104 - compared against, never bound here


def is_loopback(host: str) -> bool:
    return host in LOOPBACK_HOSTS


def _hostport(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


@dataclass(frozen=True)
class HostPolicy:
    """The one source of truth for which Host and Origin values are accepted."""

    hosts: frozenset[str]
    origins: frozenset[str]

    @classmethod
    def build(
        cls, *, bind_host: str, port: int, extra_hosts: Iterable[str] = (), origins: Iterable[str] = ()
    ) -> "HostPolicy":
        hosts = {h.strip().lower() for h in extra_hosts if h.strip()}
        if is_loopback(bind_host) or bind_host in WILDCARD_HOSTS:
            hosts.update(_hostport(h, port) for h in LOOPBACK_HOSTS)
        if bind_host not in WILDCARD_HOSTS:
            hosts.add(_hostport(bind_host, port).lower())
        return cls(hosts=frozenset(hosts), origins=frozenset(o.strip().rstrip("/") for o in origins if o.strip()))

    def sdk_settings(self) -> TransportSecuritySettings:
        """The same sets for the SDK's own check, so the two layers cannot disagree."""
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=sorted(self.hosts),
            allowed_origins=sorted(self.origins),
        )


def hash_api_key(key: str | bytes) -> str:
    return hashlib.sha256(key.encode() if isinstance(key, str) else key).hexdigest()


def classify(path: str) -> str | None:
    if path == MCP_PATH:
        return "mcp"
    if path == HEALTH_PATH:
        return "health"
    if _FILE.match(path):
        return "file"
    if _UPLOAD.match(path):
        return "upload"
    return None


class Gate:
    def __init__(self, app: ASGIApp, *, policy: HostPolicy, api_key_sha256: str | None) -> None:
        self._app = app
        self._policy = policy
        self._key_hash = api_key_sha256

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._app(scope, receive, send)
            return
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        refusal = self._check(scope)
        if refusal is not None:
            await _reject(send, *refusal)
            return
        await self._app(scope, receive, send)

    def _check(self, scope: Scope) -> tuple[int, str, list[tuple[bytes, bytes]]] | None:
        headers: dict[bytes, list[bytes]] = {}
        for name, value in scope.get("headers") or []:
            headers.setdefault(name.lower(), []).append(value)

        def single(name: bytes) -> str | None:
            values = headers.get(name)
            if not values or len(values) != 1:
                return None
            try:
                return values[0].decode("ascii")
            except UnicodeDecodeError:
                return None

        host = single(b"host")
        if host is None or host.lower() not in self._policy.hosts:
            return 421, "this host name is not served here", []
        route = classify(scope.get("path") or "")
        if route is None:
            return 404, "not found", []
        if scope.get("method") not in _METHODS[route]:
            return 405, "method not allowed", [(b"allow", ", ".join(sorted(_METHODS[route])).encode())]

        # Signed file downloads are fetched by widgets from an opaque origin; the token is the check.
        if route != "file":
            if b"origin" in headers:
                origin = single(b"origin")
                if origin is None or origin.rstrip("/") not in self._policy.origins:
                    return 403, "this origin is not allowed", []
            if single(b"sec-fetch-site") == "cross-site" and b"origin" not in headers:
                return 403, "cross-site requests are not allowed", []

        if route == "mcp" and self._key_hash is not None:
            challenge = [(b"www-authenticate", b'Bearer realm="mcp-tools"')]
            values = headers.get(b"authorization") or []
            if len(values) != 1:
                return 401, "an API key is required: send it as 'Authorization: Bearer <key>'", challenge
            scheme, _, presented = values[0].partition(b" ")
            presented = presented.strip()
            if scheme.lower() != b"bearer" or not presented:
                return 401, "an API key is required: send it as 'Authorization: Bearer <key>'", challenge
            if not hmac.compare_digest(hash_api_key(presented), self._key_hash):
                return 401, "the API key is not valid", challenge
        return None


async def _reject(send: Send, status: int, message: str, extra: list[tuple[bytes, bytes]]) -> None:
    body = json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message": message}}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"cache-control", b"no-store"),
                (b"x-content-type-options", b"nosniff"),
                *extra,
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
