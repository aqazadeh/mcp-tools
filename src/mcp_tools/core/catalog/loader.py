"""Load the model catalog from YAML and answer list / search / get / recommend."""

import difflib
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from mcp_tools.core.catalog.spec import ModelSpec, OutputType, Task
from mcp_tools.core.errors import INVALID_PARAMS, UNKNOWN_MODEL, ApiError
from mcp_tools.core.pagination import Page, PageRequest, encode_cursor
from mcp_tools.core.yamlsafe import YamlError, load_yaml


class CatalogError(ValueError):
    """A catalog file is malformed. Raised at startup, never at request time."""


@dataclass(frozen=True)
class Availability:
    available: bool
    reason: str | None = None


AvailabilityFn = Callable[[str], Availability]

_PROVIDER_DEFAULT_KEYS = ("provider", "provider_name", "output_hosts", "execution")
_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def parse_catalog_document(text: str, *, source: str) -> list[ModelSpec]:
    """Parse one catalog YAML document: provider-level defaults plus a `models` list."""
    try:
        doc = load_yaml(text)
    except YamlError as exc:
        raise CatalogError(f"{source}: {exc}") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("models"), list):
        raise CatalogError(f"{source}: expected a mapping with a 'models' list")
    defaults = {k: doc[k] for k in _PROVIDER_DEFAULT_KEYS if k in doc}
    models: list[ModelSpec] = []
    for index, raw in enumerate(doc["models"]):
        if not isinstance(raw, dict):
            raise CatalogError(f"{source}: models[{index}] is not a mapping")
        try:
            models.append(ModelSpec.model_validate({**defaults, **raw}))
        except ValidationError as exc:
            raise CatalogError(f"{source}: models[{index}] ({raw.get('id', '?')}): {exc}") from exc
    return models


def _packaged_documents() -> Iterable[tuple[str, str]]:
    root = resources.files("mcp_tools") / "data" / "catalog"
    for entry in sorted(root.iterdir(), key=lambda e: e.name):
        if entry.name.endswith((".yaml", ".yml")) and entry.is_file():
            yield f"catalog/{entry.name}", entry.read_text(encoding="utf-8")


def _directory_documents(directory: Path) -> Iterable[tuple[str, str]]:
    for path in sorted(directory.glob("*.y*ml")):
        if path.is_file() and not path.is_symlink():
            yield str(path), path.read_text(encoding="utf-8")


class Catalog:
    """An immutable, in-memory model catalog."""

    def __init__(self, models: Sequence[ModelSpec], availability: AvailabilityFn | None = None) -> None:
        seen: dict[str, ModelSpec] = {}
        for model in models:
            if model.id in seen:
                raise CatalogError(f"duplicate model id {model.id!r}")
            seen[model.id] = model
        self._models = seen
        self._availability: AvailabilityFn = availability or (lambda _provider: Availability(True))

    # -- lookups ---------------------------------------------------------------------------
    def all(self) -> list[ModelSpec]:
        """Every model, hidden ones included (for operator tooling)."""
        return list(self._models.values())

    def __len__(self) -> int:
        return len(self._models)

    def providers(self) -> list[str]:
        return sorted({m.provider for m in self._models.values()})

    def availability(self, model: ModelSpec) -> Availability:
        return self._availability(model.provider)

    def set_availability(self, availability: AvailabilityFn) -> None:
        self._availability = availability

    def get(self, model_id: str) -> ModelSpec:
        model = self._models.get(model_id)
        if model is None:
            close = difflib.get_close_matches(model_id, list(self._models), n=3, cutoff=0.5)
            raise ApiError(
                UNKNOWN_MODEL,
                f"no model with id {model_id!r}",
                hint="Call models_explore with action 'list' or 'search' to find a model id.",
                recovery_tool="models_explore",
                allowed=close or None,
            )
        return model

    # -- queries ---------------------------------------------------------------------------
    def _filtered(self, *, type: OutputType | None, input: str | None, task: Task | None) -> list[ModelSpec]:
        out = []
        for model in self._models.values():
            if model.hidden:
                continue
            if type and model.output_type != type:
                continue
            if task and model.task != task:
                continue
            if input == "text" and model.prompt == "none":
                continue
            if input == "media" and not model.accepts_media:
                continue
            out.append(model)
        # available models first, then stable by id
        out.sort(key=lambda m: (not self.availability(m).available, m.id))
        return out

    @staticmethod
    def _page(models: list[ModelSpec], page: PageRequest) -> Page[ModelSpec]:
        offset = 0
        if page.cursor is not None:
            offset = page.cursor.get("o", 0)
            if not isinstance(offset, int) or offset < 0:
                raise ApiError(INVALID_PARAMS, "cursor is not valid", hint="Pass next_cursor exactly as returned.")
        chunk = models[offset : offset + page.limit]
        end = offset + len(chunk)
        has_more = end < len(models)
        return Page(items=chunk, has_more=has_more, next_cursor=encode_cursor({"o": end}) if has_more else None)

    def browse(
        self,
        page: PageRequest,
        *,
        type: OutputType | None = None,
        input: str | None = None,
        task: Task | None = "generate",
    ) -> Page[ModelSpec]:
        return self._page(self._filtered(type=type, input=input, task=task), page)

    def _score(self, model: ModelSpec, words: list[str]) -> tuple[int, int]:
        """Return (matched word count, weighted score)."""
        strong = set(_tokens(model.id) + _tokens(model.name))
        medium = set(_tokens(" ".join([*model.tags, *model.use_cases, model.provider_name, model.output_type])))
        weak = set(_tokens(model.description))
        matched = score = 0
        for word in words:
            if word in strong:
                matched, score = matched + 1, score + 3
            elif word in medium:
                matched, score = matched + 1, score + 2
            elif word in weak or any(word in token for token in strong | medium):
                matched, score = matched + 1, score + 1
        return matched, score

    def search(
        self,
        query: str,
        page: PageRequest,
        *,
        type: OutputType | None = None,
        input: str | None = None,
        task: Task | None = None,
    ) -> Page[ModelSpec]:
        """Every query word must match somewhere; best match first."""
        words = _tokens(query)
        candidates = self._filtered(type=type, input=input, task=task)
        if not words:
            return self._page(candidates, page)
        ranked: list[tuple[int, ModelSpec]] = []
        for model in candidates:
            matched, score = self._score(model, words)
            if matched == len(words):
                ranked.append((score, model))
        ranked.sort(key=lambda pair: (-pair[0], not self.availability(pair[1]).available, pair[1].id))
        return self._page([m for _, m in ranked], page)

    def recommend(
        self,
        query: str,
        *,
        type: OutputType | None = None,
        input: str | None = None,
        limit: int = 5,
    ) -> list[ModelSpec]:
        """Best-effort ranking for a goal description; partial matches are fine."""
        words = _tokens(query)
        candidates = self._filtered(type=type, input=input, task="generate")
        ranked = []
        for model in candidates:
            _, score = self._score(model, words)
            # A model the server cannot run is no recommendation, and a placeholder is the last resort.
            ranked.append((not self.availability(model).available, model.provider == "mock", -score, model.id, model))
        ranked.sort(key=lambda entry: entry[:4])
        return [entry[4] for entry in ranked[:limit]]

    def by_task(self, task: Task) -> list[ModelSpec]:
        return self._filtered(type=None, input=None, task=task)

    def public(self, model: ModelSpec, *, detail: bool = False) -> dict[str, Any]:
        state = self.availability(model)
        if detail:
            return model.detail(available=state.available, reason=state.reason)
        return model.summary(available=state.available, reason=state.reason)


def load_catalog(
    extra_dirs: Sequence[Path] = (),
    *,
    availability: AvailabilityFn | None = None,
    include_packaged: bool = True,
) -> Catalog:
    models: list[ModelSpec] = []
    documents: list[tuple[str, str]] = list(_packaged_documents()) if include_packaged else []
    for directory in extra_dirs:
        documents.extend(_directory_documents(directory))
    for source, text in documents:
        models.extend(parse_catalog_document(text, source=source))
    return Catalog(models, availability)
