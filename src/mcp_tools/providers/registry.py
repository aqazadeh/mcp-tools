"""The providers this server process can use."""

from collections.abc import Iterable

from mcp_tools.core.catalog.loader import Availability
from mcp_tools.core.errors import PROVIDER_UNAVAILABLE, ApiError
from mcp_tools.providers.base import Provider


class ProviderRegistry:
    def __init__(self, providers: Iterable[Provider] = ()) -> None:
        self._providers: dict[str, Provider] = {p.name: p for p in providers}

    def add(self, provider: Provider) -> None:
        self._providers[provider.name] = provider

    def names(self) -> list[str]:
        return sorted(self._providers)

    def usable(self) -> list[str]:
        """Providers with credentials in this process: the only jobs this process may claim."""
        return sorted(name for name, p in self._providers.items() if p.availability().available)

    def availability(self, name: str) -> Availability:
        provider = self._providers.get(name)
        if provider is None:
            return Availability(False, f"provider {name!r} is not installed in this server")
        return provider.availability()

    def get(self, name: str) -> Provider:
        state = self.availability(name)
        if not state.available:
            raise ApiError(
                PROVIDER_UNAVAILABLE,
                f"provider {name!r} is not available: {state.reason}",
                hint="Pick a model marked available:true, or ask the operator to configure the provider key.",
                recovery_tool="models_explore",
            )
        return self._providers[name]

    def peek(self, name: str) -> Provider | None:
        return self._providers.get(name)

    async def aclose(self) -> None:
        for provider in self._providers.values():
            await provider.aclose()
