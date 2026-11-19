"""Provider contract: submit -> remote handle -> idempotent poll.

Every provider follows the same shape so that any server process can pick a job up where
another left off. The remote handle is persisted as soon as `submit` returns; `poll` and
`cancel` must be safe to call repeatedly and from a different process than the one that
submitted. A provider whose API is synchronous returns the finished result from `submit`.
"""

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from mcp_tools.core.catalog.loader import Availability
from mcp_tools.core.catalog.spec import ModelSpec

JsonDict = dict[str, Any]
PollState = Literal["running", "succeeded", "failed", "canceled", "moderated"]


@dataclass(frozen=True)
class MediaInput:
    """A reference file handed to a provider. Providers cannot pull from localhost: they push it."""

    role: str
    media_id: str
    kind: str
    mime: str
    path: Path
    size: int


@dataclass(frozen=True)
class ProviderRequest:
    job_id: str
    model: ModelSpec
    prompt: str | None
    aspect_ratio: str | None
    duration: float | None
    params: JsonDict
    medias: tuple[MediaInput, ...] = ()


@dataclass(frozen=True)
class OutputRef:
    """Where one output can be fetched from: a URL, or provider-private state."""

    url: str | None = None
    state: JsonDict = field(default_factory=dict)


@dataclass(frozen=True)
class PollResult:
    state: PollState
    progress: float | None = None
    outputs: tuple[OutputRef, ...] = ()
    error_code: str | None = None
    error_message: str | None = None
    retry_after: float | None = None


@dataclass(frozen=True)
class Submitted:
    """`result` is set when the provider finished synchronously; then `remote_id` may be None."""

    remote_id: str | None
    state: JsonDict = field(default_factory=dict)
    result: PollResult | None = None
    retry_after: float | None = None  # seconds until the first poll is worth making


class ProviderError(Exception):
    """A provider call failed.

    `ambiguous` means the request may have reached the provider (a timeout after sending):
    the job must not be resubmitted. `retryable` means the same call can simply be repeated
    (a poll or a download that hit a transient error).
    """

    def __init__(self, code: str, message: str, *, retryable: bool = False, ambiguous: bool = False) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.retryable = retryable
        self.ambiguous = ambiguous


class Provider(Protocol):
    name: str

    def availability(self) -> Availability: ...

    async def submit(self, request: ProviderRequest) -> Submitted: ...

    async def poll(self, remote_id: str, state: JsonDict) -> PollResult: ...

    async def cancel(self, remote_id: str, state: JsonDict) -> None: ...

    def open_output(self, ref: OutputRef) -> AbstractAsyncContextManager[AsyncIterator[bytes]]: ...

    async def aclose(self) -> None: ...
