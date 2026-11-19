"""Fetch a URL chosen by someone else without becoming a proxy into the local network.

Used for `media_import_url` and for downloading provider output URLs (a hosted model controls
its output strings, so those are untrusted too).

Direct mode pins the connection, one hop at a time:
  resolve the name -> every address must be public -> connect to the literal IP while sending
  the original Host header and SNI (the certificate is verified for the hostname) -> confirm
  the peer address is the one that was vetted -> read with a byte cap.
A fresh client without keep-alive is used per hop: the connection pool matches by origin
only, so a reused connection would skip the check. Redirects are followed by hand and
re-validated. `ipaddress.is_global` alone is not trusted: it differs between Python patch
releases and lets multicast, NAT64 and other special ranges through.

Behind an environment proxy the peer is the proxy, so pinning is impossible; proxy mode only
checks the URL text and is documented as weaker (`mcp-tools doctor` reports the mode).
"""

import ipaddress
import socket
import urllib.request
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any, Literal

import anyio
import httpx2

from mcp_tools.core.errors import INVALID_PARAMS, QUOTA_EXCEEDED, UNSUPPORTED, ApiError

Egress = Literal["direct", "proxy", "off"]
Resolver = Callable[[str, int], Awaitable[list[str]]]

MAX_REDIRECTS = 5
TOTAL_TIMEOUT_S = 60.0
USER_AGENT = "mcp-tools/0.1 (+media import)"

_DENIED_NETWORKS = [
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
        "192.0.0.0/24", "192.0.2.0/24", "192.88.99.0/24", "192.168.0.0/16", "198.18.0.0/15",
        "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4",
        "::/96", "64:ff9b::/96", "64:ff9b:1::/48", "100::/64", "2001::/23", "2001:db8::/32",
        "2002::/16", "fc00::/7", "fe80::/10", "fec0::/10", "ff00::/8",
    )
]  # fmt: skip
_INTERNAL_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home", ".corp", ".intranet", ".test")


def is_public_address(value: str) -> bool:
    """True only for addresses that are safe to connect to on behalf of a caller."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if any(address in network for network in _DENIED_NETWORKS if network.version == address.version):
        return False
    return (
        address.is_global
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_link_local
        and not address.is_loopback
        and not address.is_unspecified
        and not address.is_private
    )


def detect_egress() -> Egress:
    """`proxy` when the environment routes HTTPS through a proxy, else `direct`."""
    proxies = urllib.request.getproxies()
    return "proxy" if any(proxies.get(k) for k in ("https", "all", "http")) else "direct"


async def _system_resolver(host: str, port: int) -> list[str]:
    try:
        infos = await anyio.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return []
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def _refuse(message: str, *, hint: str = "Use a public https URL of an image, video or audio file.") -> ApiError:
    return ApiError(INVALID_PARAMS, message, hint=hint)


def _host_allowed(host: str, allowed_hosts: Sequence[str] | None) -> bool:
    if allowed_hosts is None:
        return True
    host = host.lower().rstrip(".")
    return any(host == suffix or host.endswith("." + suffix) for suffix in (h.lower() for h in allowed_hosts))


class SafeFetcher:
    def __init__(
        self,
        *,
        egress: Egress = "direct",
        resolver: Resolver | None = None,
        transport_factory: Callable[[], httpx2.AsyncBaseTransport] | None = None,
        verify_peer: bool = True,
        total_timeout: float = TOTAL_TIMEOUT_S,
    ) -> None:
        self.egress: Egress = egress
        self._resolver = resolver or _system_resolver
        self._transport_factory = transport_factory
        self._verify_peer = verify_peer and transport_factory is None
        self._total_timeout = total_timeout

    def _check_url(self, url: str, allowed_hosts: Sequence[str] | None) -> httpx2.URL:
        try:
            parsed = httpx2.URL(url)
        except Exception as exc:
            raise _refuse("the URL is not valid") from exc
        if parsed.scheme != "https":
            raise _refuse("only https URLs can be imported")
        if parsed.userinfo:
            raise _refuse("URLs with a user name or password are not accepted")
        host = parsed.host
        if not host:
            raise _refuse("the URL has no host")
        if not _host_allowed(host, allowed_hosts):
            raise _refuse("this host is not on the allowlist for provider outputs", hint="Report this to the operator.")
        return parsed

    async def _vetted_address(self, parsed: httpx2.URL) -> str:
        host, port = parsed.host, parsed.port or 443
        try:
            ipaddress.ip_address(host)
            addresses = [host]
        except ValueError:
            addresses = await self._resolver(host, port)
        if not addresses:
            raise _refuse("the host could not be resolved")
        if not all(is_public_address(a) for a in addresses):
            raise _refuse("the URL points to a private, local or reserved address")
        return addresses[0]

    @staticmethod
    def _check_text_only(parsed: httpx2.URL) -> None:
        """Proxy mode: no pinning is possible, so only the URL text can be checked."""
        host = parsed.host.lower().rstrip(".")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if "." not in host or host == "localhost" or host.endswith(_INTERNAL_SUFFIXES):
                raise _refuse("the URL points to a local or internal host name") from None
        else:
            if not is_public_address(host):
                raise _refuse("the URL points to a private, local or reserved address")

    def _client(self, *, pinned: bool) -> httpx2.AsyncClient:
        timeout = httpx2.Timeout(20.0, connect=10.0)
        if self._transport_factory is not None:
            return httpx2.AsyncClient(
                transport=self._transport_factory(), follow_redirects=False, timeout=timeout, trust_env=False
            )
        if pinned:
            transport = httpx2.AsyncHTTPTransport(
                http2=False, retries=0, trust_env=False, limits=httpx2.Limits(max_keepalive_connections=0)
            )
            return httpx2.AsyncClient(transport=transport, follow_redirects=False, timeout=timeout, trust_env=False)
        return httpx2.AsyncClient(follow_redirects=False, timeout=timeout, trust_env=True)

    @asynccontextmanager
    async def stream(
        self,
        url: str,
        *,
        max_bytes: int,
        allowed_hosts: Sequence[str] | None = None,
    ) -> AsyncIterator[AsyncIterator[bytes]]:
        """Yield the body of `url` as chunks, capped at `max_bytes`, after following safe redirects.

        One deadline covers every hop and the time the caller spends reading the body.
        """
        try:
            with anyio.fail_after(self._total_timeout):
                async with self._hops(url, max_bytes=max_bytes, allowed_hosts=allowed_hosts) as chunks:
                    yield chunks
        except TimeoutError:
            raise _refuse("the download took too long", hint="Try again, or use a smaller file.") from None

    @asynccontextmanager
    async def _hops(
        self, url: str, *, max_bytes: int, allowed_hosts: Sequence[str] | None
    ) -> AsyncIterator[AsyncIterator[bytes]]:
        if self.egress == "off":
            raise ApiError(
                UNSUPPORTED,
                "fetching URLs is disabled on this server",
                hint="Ask the operator to enable egress, or import a local file instead.",
            )
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            parsed = self._check_url(current, allowed_hosts)
            pinned = self.egress == "direct"
            headers = {"Accept-Encoding": "identity", "User-Agent": USER_AGENT}
            extensions: dict[str, Any] = {}
            if pinned:
                address = await self._vetted_address(parsed)
                literal = f"[{address}]" if ":" in address else address
                target = str(parsed.copy_with(host=literal))
                headers["Host"] = parsed.netloc.decode("ascii")
                extensions["sni_hostname"] = parsed.host
            else:
                self._check_text_only(parsed)
                address, target = "", current
            async with self._client(pinned=pinned) as client:
                try:
                    request = client.build_request("GET", target, headers=headers, extensions=extensions)
                    response = await client.send(request, stream=True)
                except httpx2.HTTPError as exc:
                    raise _refuse("the URL could not be fetched", hint="Check that the file is public.") from exc
                try:
                    if pinned and self._verify_peer:
                        stream = response.extensions.get("network_stream")
                        peer = stream.get_extra_info("server_addr") if stream is not None else None
                        if not peer or str(peer[0]) != address:
                            raise _refuse("the connection did not reach the vetted address")
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location:
                            raise _refuse("the server sent a redirect without a location")
                        current = str(parsed.join(location))
                        continue
                    if response.status_code != 200:
                        raise _refuse(
                            f"the server answered with HTTP {response.status_code}",
                            hint="Check that the URL is correct and publicly readable.",
                        )
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > max_bytes:
                        raise ApiError(
                            QUOTA_EXCEEDED,
                            f"the file is larger than the {max_bytes // (1024 * 1024)} MB limit",
                            hint="Use a smaller file.",
                        )

                    async def body(resp: httpx2.Response = response) -> AsyncIterator[bytes]:
                        received = 0
                        try:
                            async for chunk in resp.aiter_raw():
                                received += len(chunk)
                                if received > max_bytes:
                                    raise ApiError(
                                        QUOTA_EXCEEDED,
                                        f"the file is larger than the {max_bytes // (1024 * 1024)} MB limit",
                                        hint="Use a smaller file.",
                                    )
                                yield chunk
                        except httpx2.HTTPError as exc:
                            raise _refuse("the download was interrupted", hint="Try again.") from exc

                    yield body()
                    return
                finally:
                    await response.aclose()
        raise _refuse("too many redirects")
