"""The HTTP application: gate, MCP endpoint and file gateway, driven in-process over ASGI."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from mcp_tools.config import Settings
from mcp_tools.server.http.app import HttpConfigError, HttpServer, build_http_server
from mcp_tools.server.http.signing import MAX_TTL_SECONDS, Signer
from tests.conftest import error_of, payload
from tests.helpers.media_samples import HTML, png_bytes

pytestmark = pytest.mark.anyio

BASE = "http://127.0.0.1:8000"
KEY = "k" * 40


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", log_level="WARNING", mock_delay_ms=20, api_key=KEY)


@pytest.fixture
async def server(settings: Settings) -> AsyncIterator[HttpServer]:
    built = build_http_server(settings)
    async with built.built.platform.running(), built.built.mcp.session_manager.run():
        yield built


def http(server: HttpServer, **kwargs: Any) -> httpx2.AsyncClient:
    kwargs.setdefault("base_url", BASE)
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=server.app), timeout=httpx2.Timeout(30.0), **kwargs)


RPC = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
AUTH = {"Authorization": f"Bearer {KEY}"}


async def test_gate_matrix(server: HttpServer) -> None:
    async with http(server) as c:
        assert (await c.get("/healthz")).json() == {"status": "ok"}
        cases: list[tuple[str, str, dict[str, str], int]] = [
            ("POST", "/mcp", {}, 401),
            ("POST", "/mcp", {"Authorization": "Bearer wrong"}, 401),
            ("POST", "/mcp", {"Authorization": f"Basic {KEY}"}, 401),
            ("POST", "/mcp", {"Authorization": "Bearer"}, 401),
            ("POST", f"/mcp?key={KEY}", {}, 401),
            ("POST", "/mcp", {"Cookie": f"key={KEY}"}, 401),
            ("PUT", "/mcp", AUTH, 405),
            ("POST", "/mcp/", AUTH, 404),
            ("POST", "/MCP", AUTH, 404),
            ("GET", "/", AUTH, 404),
            ("GET", "/.well-known/oauth-protected-resource", AUTH, 404),
            ("GET", "/files/med_" + "0" * 26 + "/x", AUTH, 404),
            ("GET", "/files/med_short", AUTH, 404),
            ("POST", "/healthz", {}, 405),
            ("POST", "/mcp", {**AUTH, "Origin": "https://evil.test"}, 403),
            ("POST", "/mcp", {**AUTH, "Sec-Fetch-Site": "cross-site"}, 403),
            ("GET", "/healthz", {"Origin": "https://evil.test"}, 403),
        ]
        for method, path, headers, expected in cases:
            response = await c.request(method, path, headers=headers, json=RPC if method == "POST" else None)
            assert response.status_code == expected, (method, path, headers, response.text)
            assert response.json()["error"]["message"], path
            assert "access-control-allow-origin" not in response.headers
        unauthorized = await c.post("/mcp", json=RPC)
        assert unauthorized.headers["www-authenticate"].startswith("Bearer")
        assert KEY not in unauthorized.text

    for host in ("evil.test", "127.0.0.1:9999", "localhost.evil.test:8000"):
        async with http(server, base_url=f"http://{host}") as bad:
            assert (await bad.post("/mcp", json=RPC, headers=AUTH)).status_code == 421
            assert (await bad.get("/healthz")).status_code == 421


async def test_duplicate_authorization_headers_are_refused(server: HttpServer) -> None:
    async with http(server) as c:
        request = c.build_request("POST", "/mcp", json=RPC)
        request.headers = httpx2.Headers(
            [*request.headers.raw, (b"authorization", f"Bearer {KEY}".encode()), (b"authorization", b"Bearer x")]
        )
        assert (await c.send(request)).status_code == 401


async def test_both_host_checks_agree(server: HttpServer) -> None:
    sdk = server.policy.sdk_settings()
    assert sdk.enable_dns_rebinding_protection is True
    assert set(sdk.allowed_hosts) == set(server.policy.hosts) == {"127.0.0.1:8000", "localhost:8000", "[::1]:8000"}
    assert sdk.allowed_origins == []


async def test_mcp_over_http_upload_generate_and_download(server: HttpServer) -> None:
    async with http(server, headers=AUTH) as authed, http(server) as anonymous:
        async with Client(streamable_http_client(f"{BASE}/mcp", http_client=authed)) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            assert {"media_upload", "media_confirm", "media_import_path"} <= names  # loopback: local paths stay

            reserved = payload(await c.call_tool("media_upload", {"files": [{"filename": "ref.png"}, {}]}))
            first, second = reserved["uploads"]
            assert first["upload_url"].startswith(f"{BASE}/upload/{first['media_id']}?")
            early = payload(await c.call_tool("media_confirm", {"media_ids": [first["media_id"]]}))
            assert early["all_ready"] is False and early["items"][0]["status"] == "not_uploaded"
            unusable = error_of(
                await c.call_tool(
                    "generate_image",
                    {
                        "params": {
                            "model": "mock_image",
                            "prompt": "p",
                            "medias": [{"role": "image", "value": first["media_id"]}],
                        }
                    },
                )
            )
            assert unusable["code"] == "invalid_params" and "not ready" in unusable["message"]

            path = first["upload_url"].removeprefix(BASE)
            assert (await anonymous.put(path + "0", content=png_bytes())).status_code == 403  # tampered signature
            assert (await anonymous.put(path.replace("&m=", "&m=9"), content=png_bytes())).status_code == 403
            other = path.replace(first["media_id"], second["media_id"])
            assert (await anonymous.put(other, content=png_bytes())).status_code == 403  # token is bound to its id
            assert (await anonymous.put(path, content=HTML)).status_code == 415
            assert (await anonymous.post(path, content=png_bytes())).status_code == 405
            uploaded = await anonymous.put(path, content=png_bytes(40, 30))
            assert uploaded.status_code == 201, uploaded.text
            assert (await anonymous.put(path, content=png_bytes(1, 1))).status_code == 409  # single use

            confirmed = payload(
                await c.call_tool("media_confirm", {"media_ids": [first["media_id"], second["media_id"], "med_x"]})
            )
            assert [i["status"] for i in confirmed["items"]] == ["ready", "not_uploaded", "not_found"]
            assert confirmed["items"][0]["width"] == 40 and confirmed["items"][0]["filename"] == "ref.png"

            done = payload(
                await c.call_tool(
                    "generate_image",
                    {
                        "params": {
                            "model": "mock_image",
                            "prompt": "p",
                            "medias": [{"role": "image", "value": first["media_id"]}],
                        },
                        "wait_seconds": 10,
                    },
                )
            )
            assert "jobs" in done, done
            output = done["jobs"][0]["results"][0]
            assert done["jobs"][0]["status"] == "completed" and output["url"].startswith(f"{BASE}/files/med_")

        download = output["url"].removeprefix(BASE)
        full = await anonymous.get(download)
        assert full.status_code == 200 and full.headers["content-type"] == "image/png"
        assert (
            full.headers["x-content-type-options"] == "nosniff" and "sandbox" in full.headers["content-security-policy"]
        )
        assert full.headers["content-disposition"] == "inline" and len(full.content) == output["size_bytes"]
        part = await anonymous.get(download, headers={"Range": "bytes=0-7"})
        assert part.status_code == 206 and part.content == full.content[:8]
        assert part.headers["content-range"] == f"bytes 0-7/{len(full.content)}"
        tail = await anonymous.get(download, headers={"Range": "bytes=-4"})
        assert tail.content == full.content[-4:]
        assert (await anonymous.get(download, headers={"Range": "bytes=99999999-"})).status_code == 416
        huge = await anonymous.get(download, headers={"Range": "bytes=" + "9" * 5000 + "-"})
        assert huge.status_code == 200 and huge.content == full.content  # an absurd range is ignored, not a 500
        assert (await anonymous.head(download)).headers["content-length"] == str(len(full.content))
        # a widget in an opaque origin may fetch a signed file
        assert (await anonymous.get(download, headers={"Origin": "null"})).status_code == 200
        assert (await anonymous.get(download[:-1] + ("0" if download[-1] != "0" else "1"))).status_code == 403
        assert (await anonymous.get(download + "&x=1")).status_code == 403
        assert (await anonymous.get(download.split("?")[0])).status_code == 403


async def test_signed_urls_expire_and_cannot_change_purpose() -> None:
    signer = Signer(b"s" * 32)
    media_id = "med_" + "0" * 26
    now = 1_000_000
    ok = signer.download_query(media_id, now + 60).encode()
    assert signer.verify("download", media_id, ok, now_s=now) is not None
    assert signer.verify("download", media_id, ok, now_s=now + 61) is None
    assert signer.verify("download", "med_" + "1" * 26, ok, now_s=now) is None
    assert (
        signer.verify(
            "download", media_id, signer.download_query(media_id, now + MAX_TTL_SECONDS + 999).encode(), now_s=now
        )
        is None
    )
    assert Signer(b"t" * 32).verify("download", media_id, ok, now_s=now) is None
    upload = signer.upload_query(media_id, now + 60, 100, "n" * 24).encode()
    grant = signer.verify("upload", media_id, upload, now_s=now)
    assert grant is not None and grant.max_bytes == 100 and grant.nonce == "n" * 24
    assert signer.verify("download", media_id, upload, now_s=now) is None
    assert signer.verify("upload", media_id, ok, now_s=now) is None
    for bad in (b"", b"e=1", ok + b"&e=9", ok.replace(b"e=", b"e=0"), b"\xff", ok + b"&"):
        assert signer.verify("download", media_id, bad, now_s=now) is None


async def test_upload_size_limit(server: HttpServer) -> None:
    platform = server.built.platform
    media_id, nonce, _ = await platform.media.reserve_upload(
        principal=platform.principal, filename=None, ttl_seconds=60
    )
    url = platform.urls.upload_url(media_id, ttl_seconds=60, max_bytes=50, nonce=nonce)
    assert url is not None
    async with http(server) as c:
        assert (await c.put(url.removeprefix(BASE), content=png_bytes(64, 64) * 4)).status_code == 413

        async def trickle():  # no content-length: the stream counter has to catch it
            for _ in range(20):
                yield png_bytes(8, 8)

        assert (await c.put(url.removeprefix(BASE), content=trickle())).status_code == 413
    assert sum(1 for p in platform.data.blobs.rglob("*") if p.is_file()) == 0


def test_unsafe_http_configurations_are_refused(tmp_path: Path) -> None:
    def make(**kwargs: Any) -> Settings:
        return Settings(data_dir=tmp_path / "d", log_level="WARNING", api_key=KEY, **kwargs)

    with pytest.raises(HttpConfigError, match="plain HTTP"):
        build_http_server(make(http_host="0.0.0.0"))
    with pytest.raises(HttpConfigError, match="no-auth"):
        build_http_server(make(http_host="0.0.0.0"), no_auth=True, allow_insecure=True)
    with pytest.raises(HttpConfigError, match="no-auth"):
        build_http_server(make(public_base_url="https://media.example.com"), no_auth=True)
    with pytest.raises(HttpConfigError, match="too short"):
        build_http_server(Settings(data_dir=tmp_path / "d", api_key="short"))
    with pytest.raises(HttpConfigError, match="public_base_url"):
        build_http_server(make(public_base_url="https://media.example.com/path"))

    public = build_http_server(
        make(http_host="0.0.0.0", public_base_url="https://media.example.com"), allow_insecure=True
    )
    assert {"media.example.com", "media.example.com:443"} <= public.policy.hosts
    names = {d.name for d in public.built.tools}
    assert "media_import_path" not in names and "media_upload" in names
    assert public.built.platform.expose_local_paths is False


def test_extra_host_names_make_the_server_non_local(tmp_path: Path) -> None:
    tunnelled = build_http_server(
        Settings(data_dir=tmp_path / "d", log_level="WARNING", api_key=KEY, http_allowed_hosts="studio.example.com")
    )
    assert "studio.example.com" in tunnelled.policy.hosts
    assert "media_import_path" not in {d.name for d in tunnelled.built.tools}
    assert tunnelled.built.platform.expose_local_paths is False


def test_a_generated_key_is_stored_hashed_and_shown_once(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "d", log_level="WARNING")
    first = build_http_server(settings)
    assert first.new_api_key and len(first.new_api_key) >= 40
    stored = (tmp_path / "d" / "api-key.sha256").read_text()
    assert first.new_api_key not in stored and len(stored) == 64
    assert (tmp_path / "d" / "api-key.sha256").stat().st_mode & 0o777 == 0o600
    assert build_http_server(settings).new_api_key is None
