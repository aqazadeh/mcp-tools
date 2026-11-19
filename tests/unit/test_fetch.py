"""The URL fetcher must never connect to a private, local or reserved address."""

from collections.abc import Callable

import anyio
import httpx2
import pytest

from mcp_tools.core.errors import ApiError
from mcp_tools.core.media.fetch import MAX_REDIRECTS, SafeFetcher, is_public_address

pytestmark = pytest.mark.anyio

PUBLIC = "93.184.216.34"

DENIED = [
    "0.0.0.0", "10.0.0.1", "100.64.0.1", "127.0.0.1", "127.255.255.254", "169.254.169.254", "172.16.0.1",
    "172.31.255.255", "192.0.0.1", "192.0.2.1", "192.88.99.1", "192.168.1.1", "198.18.0.1", "198.51.100.7",
    "203.0.113.9", "224.0.0.1", "239.255.255.250", "240.0.0.1", "255.255.255.255",
    "::", "::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1", "::ffff:169.254.169.254", "::7f00:1", "64:ff9b::7f00:1",
    "64:ff9b:1::1", "100::1", "2001:db8::1", "2001::1", "2002:7f00:1::1", "fc00::1", "fd12:3456::1", "fe80::1",
    "fec0::1", "ff02::1", "not-an-address", "", "1.2.3", "0x7f.0.0.1",
]  # fmt: skip
ALLOWED = [PUBLIC, "1.1.1.1", "8.8.8.8", "::ffff:8.8.8.8", "2606:4700:4700::1111", "2a00:1450:4001:81b::200e"]


@pytest.mark.parametrize("address", DENIED)
def test_non_public_addresses_are_denied(address: str) -> None:
    assert is_public_address(address) is False


@pytest.mark.parametrize("address", ALLOWED)
def test_public_addresses_are_allowed(address: str) -> None:
    assert is_public_address(address) is True


def fetcher(
    handler: Callable[[httpx2.Request], httpx2.Response],
    hosts: dict[str, list[str]] | None = None,
    **kwargs: object,
) -> SafeFetcher:
    table = {"cdn.example.com": [PUBLIC], **(hosts or {})}

    async def resolve(host: str, port: int) -> list[str]:
        return table.get(host, [])

    return SafeFetcher(resolver=resolve, transport_factory=lambda: httpx2.MockTransport(handler), **kwargs)  # type: ignore[arg-type]


async def fetch(f: SafeFetcher, url: str, *, max_bytes: int = 1000, **kwargs: object) -> bytes:
    async with f.stream(url, max_bytes=max_bytes, **kwargs) as chunks:  # type: ignore[arg-type]
        return b"".join([c async for c in chunks])


def reply(body: bytes, *, declare: bool = False) -> httpx2.Response:
    """A streamed 200 response, like a real transport returns (a bytes body would arrive pre-read)."""

    async def chunks():
        yield body

    return httpx2.Response(200, content=chunks(), headers={"content-length": str(len(body))} if declare else None)


def never(request: httpx2.Request) -> httpx2.Response:
    raise AssertionError(f"no request must be sent, got {request.url}")


async def test_connects_to_the_vetted_ip_with_the_original_host_and_sni() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return reply(b"hello")

    assert await fetch(fetcher(handler), "https://cdn.example.com:8443/a.png?x=1") == b"hello"
    (request,) = seen
    assert request.url.host == PUBLIC and request.url.port == 8443 and request.url.path == "/a.png"
    assert request.headers["host"] == "cdn.example.com:8443"
    assert request.extensions["sni_hostname"] == "cdn.example.com"
    assert request.headers["accept-encoding"] == "identity"


@pytest.mark.parametrize(
    "url",
    [
        "http://cdn.example.com/a.png",
        "ftp://cdn.example.com/a.png",
        "file:///etc/passwd",
        "https://user:pw@cdn.example.com/a.png",
        "https:///a.png",
        "https://127.0.0.1/a.png",
        "https://[::1]/a.png",
        "https://169.254.169.254/latest/meta-data",
        "https://[::ffff:10.0.0.1]/a.png",
        "https://2130706433/a.png",
        "https://internal.example.com/a.png",
        "https://mixed.example.com/a.png",
        "https://unresolvable.example.com/a.png",
        "not a url",
    ],
)
async def test_unsafe_urls_are_refused_before_any_request(url: str) -> None:
    f = fetcher(never, {"internal.example.com": ["10.1.2.3"], "mixed.example.com": [PUBLIC, "192.168.0.5"]})
    with pytest.raises(ApiError) as caught:
        await fetch(f, url)
    assert caught.value.code == "invalid_params"


async def test_redirects_are_followed_by_hand_and_revalidated() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/start":
            return httpx2.Response(302, headers={"location": "/next"})
        if request.url.path == "/next":
            return httpx2.Response(307, headers={"location": "https://other.example.com/final"})
        assert request.headers["host"] == "other.example.com"
        return reply(b"done")

    f = fetcher(handler, {"other.example.com": ["1.1.1.1"]})
    assert await fetch(f, "https://cdn.example.com/start") == b"done"


@pytest.mark.parametrize(
    "location",
    ["https://internal.example.com/x", "http://cdn.example.com/x", "https://127.0.0.1/x", "https://[fd00::1]/x"],
)
async def test_a_redirect_to_an_unsafe_target_is_refused(location: str) -> None:
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(302, headers={"location": location})

    with pytest.raises(ApiError):
        await fetch(fetcher(handler, {"internal.example.com": ["10.0.0.9"]}), "https://cdn.example.com/a")
    assert calls == 1


async def test_redirect_loops_and_bad_statuses_end_in_an_error() -> None:
    calls = 0

    def loop(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(302, headers={"location": "/again"})

    with pytest.raises(ApiError, match="too many redirects"):
        await fetch(fetcher(loop), "https://cdn.example.com/a")
    assert calls == MAX_REDIRECTS + 1
    with pytest.raises(ApiError, match="HTTP 404"):
        await fetch(fetcher(lambda r: httpx2.Response(404)), "https://cdn.example.com/a")
    with pytest.raises(ApiError, match="without a location"):
        await fetch(fetcher(lambda r: httpx2.Response(302)), "https://cdn.example.com/a")


async def test_size_limit_applies_to_declared_and_to_actual_bytes() -> None:
    with pytest.raises(ApiError) as declared:
        await fetch(fetcher(lambda r: reply(b"x" * 50, declare=True)), "https://cdn.example.com/a", max_bytes=10)
    assert declared.value.code == "quota_exceeded"

    async def endless():  # no content-length
        while True:
            yield b"y" * 8

    with pytest.raises(ApiError) as streamed:
        await fetch(
            fetcher(lambda r: httpx2.Response(200, content=endless())), "https://cdn.example.com/a", max_bytes=20
        )
    assert streamed.value.code == "quota_exceeded"


async def test_allowlist_egress_off_and_transport_errors() -> None:
    ok = fetcher(lambda r: reply(b"z"), {"files.provider.test": ["8.8.8.8"]})
    assert await fetch(ok, "https://files.provider.test/o", allowed_hosts=["provider.test"]) == b"z"
    with pytest.raises(ApiError, match="allowlist"):
        await fetch(ok, "https://cdn.example.com/o", allowed_hosts=["provider.test"])
    with pytest.raises(ApiError, match="allowlist"):
        await fetch(ok, "https://evilprovider.test/o", allowed_hosts=["provider.test"])

    with pytest.raises(ApiError) as off:
        await fetch(fetcher(never, egress="off"), "https://cdn.example.com/a")
    assert off.value.code == "unsupported"

    def broken(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("secret-internal-detail")

    with pytest.raises(ApiError) as failed:
        await fetch(fetcher(broken), "https://cdn.example.com/a")
    assert "secret" not in failed.value.message


async def test_proxy_mode_checks_the_url_text_and_does_not_rewrite_it() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return reply(b"p")

    f = fetcher(handler, egress="proxy")
    assert await fetch(f, "https://cdn.example.com/a") == b"p"
    assert seen[0].url.host == "cdn.example.com" and "sni_hostname" not in seen[0].extensions
    for url in (
        "https://localhost/a", "https://intranet/a", "https://db.internal/a", "https://10.0.0.1/a",
        "https://127.1/a", "https://0x7f.0.0.1/a", "https://10.1:8443/a", "https://2130706433/a", "https://host.7/a",
    ):  # fmt: skip
        with pytest.raises(ApiError):
            await fetch(f, url)
    assert len(seen) == 1


async def test_the_deadline_covers_slow_readers() -> None:
    f = fetcher(lambda r: reply(b"slow"), total_timeout=0.05)
    with pytest.raises(ApiError, match="too long"):
        async with f.stream("https://cdn.example.com/a", max_bytes=100):
            await anyio.sleep(1)
