"""File gateway: signed downloads and uploads next to the MCP endpoint.

These routes carry no API key; the signed URL is the credential. Responses are hardened so a
stored file can never run as a page in the server's origin: the content type comes from
sniffing at ingest, only image / video / audio are served inline, and every response is
sandboxed and `nosniff`.
"""

import re
from collections.abc import AsyncIterator

import anyio
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from mcp_tools.core.errors import ApiError
from mcp_tools.core.platform import Platform
from mcp_tools.server.http.signing import Signer

UPLOAD_TIMEOUT_SECONDS = 600.0
_RANGE = re.compile(r"^bytes=(\d{0,18})-(\d{0,18})$")
_SAFE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "sandbox; default-src 'none'",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "cross-origin",
    "Cache-Control": "private, no-store",
}


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status, headers=_SAFE_HEADERS)


def _parse_range(header: str | None, size: int) -> tuple[int, int] | bool | None:
    """(start, end-exclusive) for a satisfiable single range, None for no range, False if unsatisfiable."""
    if not header:
        return None
    match = _RANGE.match(header.strip())
    if not match or (not match.group(1) and not match.group(2)):
        return None  # multi-range or malformed: serve the whole file
    first, last = match.group(1), match.group(2)
    if not first:  # suffix: the last N bytes
        length = int(last)
        if length == 0:
            return False
        return max(0, size - length), size
    start = int(first)
    if start >= size:
        return False
    end = min(int(last), size - 1) if last else size - 1
    if end < start:
        return None
    return start, end + 1


class Gateway:
    def __init__(self, platform: Platform, signer: Signer) -> None:
        self._platform = platform
        self._signer = signer

    def _now_s(self) -> int:
        return self._platform.clock.now_ms() // 1000

    async def health(self, request: Request) -> Response:
        running = self._platform.started
        return JSONResponse({"status": "ok" if running else "starting"}, status_code=200 if running else 503)

    async def download(self, request: Request) -> Response:
        media_id = request.path_params["media_id"]
        grant = self._signer.verify("download", media_id, request.scope.get("query_string", b""), now_s=self._now_s())
        if grant is None or not self._platform.started:
            return _error(403, "forbidden", "this link is not valid or has expired")
        try:
            row = await self._platform.media.get(media_id, principal=self._platform.principal)
        except ApiError:
            return _error(404, "not_found", "no such file")
        if (
            row.status != "ready"
            or not row.blob_key
            or row.size_bytes is None
            or row.kind not in ("image", "video", "audio")
        ):
            return _error(404, "not_found", "no such file")
        size = row.size_bytes
        span = _parse_range(request.headers.get("range"), size)
        headers = {
            **_SAFE_HEADERS,
            "Accept-Ranges": "bytes",
            "Content-Disposition": "inline",
            "Content-Type": row.mime,
        }
        if span is False:
            return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"})
        start, end = span if isinstance(span, tuple) else (0, size)
        status = 206 if isinstance(span, tuple) else 200
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end - 1}/{size}"
        headers["Content-Length"] = str(end - start)
        if request.method == "HEAD":
            return Response(status_code=status, headers=headers)
        body = self._platform.blobs.open(row.blob_key, start=start, end=end)
        return StreamingResponse(body, status_code=status, headers=headers)

    async def upload(self, request: Request) -> Response:
        media_id = request.path_params["media_id"]
        grant = self._signer.verify("upload", media_id, request.scope.get("query_string", b""), now_s=self._now_s())
        if grant is None or not self._platform.started:
            return _error(403, "forbidden", "this upload link is not valid or has expired")
        declared = request.headers.get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > grant.max_bytes):
            return _error(413, "too_large", f"the file is larger than {grant.max_bytes} bytes")
        if not await self._platform.media.upload_open(media_id, grant.nonce):
            return _error(409, "conflict", "this upload link was already used or has expired")

        async def chunks() -> AsyncIterator[bytes]:
            received = 0
            async for chunk in request.stream():
                received += len(chunk)
                if received > grant.max_bytes:
                    raise _TooLarge
                yield chunk

        try:
            with anyio.fail_after(UPLOAD_TIMEOUT_SECONDS):
                attached = await self._platform.media.receive_upload(
                    media_id, grant.nonce, chunks(), max_bytes=grant.max_bytes
                )
        except _TooLarge:
            return _error(413, "too_large", f"the file is larger than {grant.max_bytes} bytes")
        except ApiError as err:
            status = 413 if err.code == "quota_exceeded" else 415
            return _error(status, err.code, err.message)
        except TimeoutError:
            return _error(408, "timeout", "the upload took too long")
        except ClientDisconnect:
            return _error(400, "incomplete", "the upload was interrupted")
        if not attached:
            return _error(409, "conflict", "this upload link was already used or has expired")
        return JSONResponse({"media_id": media_id, "status": "uploaded"}, status_code=201, headers=_SAFE_HEADERS)


class _TooLarge(Exception):
    pass
