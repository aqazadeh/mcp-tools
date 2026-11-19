"""YAML loading for catalog files and workflow front matter.

`yaml.safe_load` cannot construct arbitrary objects, but it shares alias nodes without
limit, so a tiny document can expand exponentially once validated or serialised. Catalog
and workflow files never need anchors, so aliases are rejected outright, and input size
is capped before parsing.
"""

from typing import Any

import yaml
from yaml.events import AliasEvent

MAX_YAML_BYTES = 512 * 1024


class YamlError(ValueError):
    """The document is too large, uses aliases, or is not valid YAML."""


class _NoAliasSafeLoader(yaml.SafeLoader):
    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(AliasEvent):
            raise YamlError("YAML aliases are not allowed")
        return super().compose_node(parent, index)


def load_yaml(text: str, *, max_bytes: int = MAX_YAML_BYTES) -> Any:
    if len(text.encode("utf-8", "replace")) > max_bytes:
        raise YamlError(f"YAML document exceeds {max_bytes} bytes")
    try:
        return yaml.load(text, Loader=_NoAliasSafeLoader)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as exc:
        raise YamlError(f"invalid YAML: {exc}") from exc
