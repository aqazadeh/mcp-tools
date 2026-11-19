"""Who is calling. v1 has one local workspace; OAuth later only changes how this is resolved."""

from dataclasses import dataclass

LOCAL_WORKSPACE = "ws_local"


@dataclass(frozen=True)
class Principal:
    workspace_id: str = LOCAL_WORKSPACE


LOCAL = Principal()
