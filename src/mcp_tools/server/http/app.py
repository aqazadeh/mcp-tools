"""Assemble the HTTP application: MCP endpoint + file gateway behind the gate."""

import logging
import secrets
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.routing import Mount, Route
from starlette.types import ASGIApp

from mcp_tools.config import Settings
from mcp_tools.core.clock import SystemClock
from mcp_tools.core.datadir import DataDir, prepare_data_dir, read_or_create_secret, write_private
from mcp_tools.core.platform import Platform
from mcp_tools.server.build import Built, build_server
from mcp_tools.server.http.gate import Gate, HostPolicy, hash_api_key, is_loopback
from mcp_tools.server.http.gateway import Gateway
from mcp_tools.server.http.signing import GatewayIssuer, Signer
from mcp_tools.server.registry import Capabilities

logger = logging.getLogger(__name__)


class HttpConfigError(ValueError):
    """The HTTP settings are unsafe or inconsistent; the server refuses to start."""


@dataclass(frozen=True)
class HttpServer:
    app: ASGIApp
    built: Built
    policy: HostPolicy
    host: str
    port: int
    public_base_url: str
    new_api_key: str | None  # set when a key was generated on this start; shown once


def load_api_key_hash(data: DataDir, settings: Settings) -> tuple[str, str | None]:
    """Return (sha256 of the key, the key itself if it was created just now)."""
    if settings.api_key:
        if len(settings.api_key) < 24:
            raise HttpConfigError("MCP_TOOLS_API_KEY is too short; use at least 24 random characters")
        return hash_api_key(settings.api_key), None
    path = data.api_key_file
    if path.exists():
        digest = path.read_text(encoding="ascii").strip()
        if len(digest) != 64:
            raise HttpConfigError(f"{path} is damaged; delete it to generate a new API key")
        return digest, None
    key = secrets.token_urlsafe(32)
    write_private(path, hash_api_key(key).encode("ascii"))
    return hash_api_key(key), key


def build_http_server(
    settings: Settings,
    *,
    no_auth: bool = False,
    allow_insecure: bool = False,
    tls: bool = False,
    platform: Platform | None = None,
) -> HttpServer:
    host, port = settings.http_host, settings.http_port
    loopback = is_loopback(host)
    extra_hosts = [h for h in settings.http_allowed_hosts.split(",") if h.strip()]
    scheme = "https" if tls else "http"
    base_url = (settings.public_base_url or f"{scheme}://{f'[{host}]' if ':' in host else host}:{port}").rstrip("/")
    parts = urlsplit(base_url)
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.path not in ("", "/") or parts.query:
        raise HttpConfigError("public_base_url must look like https://host[:port] with no path")
    if no_auth and (not loopback or extra_hosts or settings.public_base_url):
        raise HttpConfigError("--no-auth is only allowed when bound to loopback with no extra hosts or public URL")
    if not loopback and not tls and not allow_insecure:
        raise HttpConfigError(
            "refusing to serve plain HTTP on a non-loopback address; use --tls-cert/--tls-key, put a TLS proxy "
            "in front and pass --insecure-http, or bind to 127.0.0.1"
        )
    if settings.public_base_url:
        extra_hosts.append(parts.netloc)
        if parts.port is None:
            extra_hosts.append(f"{parts.hostname}:{443 if parts.scheme == 'https' else 80}")
    policy = HostPolicy.build(
        bind_host=host, port=port, extra_hosts=extra_hosts, origins=settings.http_allowed_origins.split(",")
    )

    data = prepare_data_dir(settings.data_dir)
    signer = Signer(read_or_create_secret(data.signing_key_file))
    key_hash, new_key = (None, None) if no_auth else load_api_key_hash(data, settings)

    local = loopback and not settings.public_base_url
    if platform is None:
        clock = SystemClock()
        platform = Platform(
            settings, clock=clock, urls=GatewayIssuer(signer, base_url, clock), expose_local_paths=local
        )
    built = build_server(
        settings,
        platform=platform,
        capabilities=Capabilities(
            url_issuer=True, local_paths=local and settings.local_imports, ui=settings.ui_enabled
        ),
    )
    mcp_app = built.mcp.streamable_http_app(transport_security=policy.sdk_settings(), session_idle_timeout=None)
    gateway = Gateway(platform, signer)

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        # A mounted app's own lifespan is not run by the parent, so the session manager is started here.
        async with platform.running(), built.mcp.session_manager.run():
            yield

    inner = Starlette(
        routes=[
            Route("/healthz", gateway.health, methods=["GET"]),
            Route("/files/{media_id}", gateway.download, methods=["GET", "HEAD"]),
            Route("/upload/{media_id}", gateway.upload, methods=["PUT"]),
            Mount("/", app=mcp_app),
        ],
        lifespan=lifespan,
    )
    app = Gate(inner, policy=policy, api_key_sha256=key_hash)
    return HttpServer(
        app=app, built=built, policy=policy, host=host, port=port, public_base_url=base_url, new_api_key=new_key
    )


def serve_http(
    settings: Settings, *, no_auth: bool, allow_insecure: bool, tls_cert: str | None, tls_key: str | None
) -> int:
    import uvicorn

    if bool(tls_cert) != bool(tls_key):
        raise HttpConfigError("--tls-cert and --tls-key must be given together")
    server = build_http_server(settings, no_auth=no_auth, allow_insecure=allow_insecure, tls=bool(tls_cert))
    out = sys.stderr
    print(f"mcp-tools: MCP endpoint {server.public_base_url}/mcp", file=out)
    if no_auth:
        print("mcp-tools: authentication is OFF (loopback only)", file=out)
    elif server.new_api_key:
        print(f"mcp-tools: new API key (shown once, store it now): {server.new_api_key}", file=out)
        print("mcp-tools: clients send it as  Authorization: Bearer <key>", file=out)
    else:
        print("mcp-tools: using the existing API key (run `mcp-tools key rotate` for a new one)", file=out)
    config = uvicorn.Config(
        server.app,
        host=server.host,
        port=server.port,
        lifespan="on",
        timeout_graceful_shutdown=10,
        access_log=False,  # signed URLs must not end up in a log
        proxy_headers=False,
        server_header=False,
        log_config=None,
        ssl_certfile=tls_cert,
        ssl_keyfile=tls_key,
    )
    uvicorn.Server(config).run()
    return 0
