"""Widget HTML: static files, addressed by content.

The HTML never contains anything from a request: data reaches a widget only through the
MCP Apps bridge. The resource URI carries a hash of the file because hosts may cache a
`ui://` document; a changed widget must get a new address.
"""

import hashlib
from dataclasses import dataclass
from importlib import resources


@dataclass(frozen=True)
class Widget:
    name: str
    uri: str
    html: str
    title: str
    description: str


def _load(name: str, title: str, description: str) -> Widget:
    html = (resources.files("mcp_tools") / "server" / "ui" / f"{name}.html").read_text(encoding="utf-8")
    digest = hashlib.sha256(html.encode()).hexdigest()[:10]
    return Widget(name, f"ui://mcp-tools/{name}.{digest}.html", html, title, description)


RESULTS = _load("results", "Generation results", "Shows generation jobs as they finish, with image previews.")
WIDGETS: dict[str, Widget] = {RESULTS.uri: RESULTS}
