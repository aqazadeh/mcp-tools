"""A provider whose behaviour is scripted by the test."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio

from mcp_tools.core.catalog.loader import Availability
from mcp_tools.providers.base import OutputRef, PollResult, ProviderError, ProviderRequest, Submitted
from tests.helpers.media_samples import png_bytes

CATALOG_DIR = Path(__file__).parent / "catalog"


class ScriptedProvider:
    name = "fake"

    def __init__(self, *, submit_log: Path | None = None, submit_delay: float = 0.0) -> None:
        self.submits: list[ProviderRequest] = []
        self.polls: list[str] = []
        self.cancels: list[str] = []
        self.poll_script: list[PollResult | Exception] = []
        self.default_poll = PollResult("succeeded", progress=1.0, outputs=(OutputRef(state={"n": 1}),))
        self.submit_error: Exception | None = None
        self.sync_result: PollResult | None = None
        self.output: bytes | Exception = png_bytes(16, 16)
        self.before_output: Any = None  # async callable run right before the output is yielded
        self.available = True
        self._submit_log = submit_log
        self._submit_delay = submit_delay

    def availability(self) -> Availability:
        return Availability(True) if self.available else Availability(False, "disabled by the test")

    async def submit(self, request: ProviderRequest) -> Submitted:
        self.submits.append(request)
        if self._submit_log is not None:  # one line per submit, atomic across processes
            fd = os.open(self._submit_log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, f"{request.job_id} {os.getpid()}\n".encode())
            finally:
                os.close(fd)
        if self._submit_delay:
            await anyio.sleep(self._submit_delay)
        if self.submit_error is not None:
            raise self.submit_error
        if self.sync_result is not None:
            return Submitted(remote_id=None, result=self.sync_result)
        return Submitted(remote_id=f"remote-{request.job_id}", state={"job": request.job_id}, retry_after=0.1)

    async def poll(self, remote_id: str, state: dict[str, Any]) -> PollResult:
        self.polls.append(remote_id)
        step = self.poll_script.pop(0) if self.poll_script else self.default_poll
        if isinstance(step, Exception):
            raise step
        return step

    async def cancel(self, remote_id: str, state: dict[str, Any]) -> None:
        self.cancels.append(remote_id)

    @asynccontextmanager
    async def open_output(self, ref: OutputRef) -> AsyncIterator[AsyncIterator[bytes]]:
        if self.before_output is not None:
            await self.before_output()
        if isinstance(self.output, Exception):
            raise self.output
        data = self.output

        async def chunks() -> AsyncIterator[bytes]:
            yield data

        yield chunks()

    async def aclose(self) -> None:
        return None


RUNNING = PollResult("running", progress=0.5, retry_after=0.1)
FAILED = PollResult("failed", error_code="provider_error", error_message="boom")
MODERATED = PollResult("moderated", error_code="content_policy", error_message="not allowed")
TRANSIENT = ProviderError("network", "temporary", retryable=True)
