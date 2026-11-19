"""Command line entry point: `mcp-tools <command>`."""

import argparse
import sys
from collections.abc import Sequence

from mcp_tools import __version__
from mcp_tools.config import Settings


def _cmd_serve(args: argparse.Namespace) -> int:
    from mcp_tools.server.build import build_server

    if args.transport != "stdio":
        print("error: only the stdio transport is available in this build", file=sys.stderr)
        return 2
    built = build_server(Settings())
    built.mcp.run("stdio")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mcp-tools", description="Generative media MCP server.")
    parser.add_argument("--version", action="version", version=f"mcp-tools {__version__}")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="Run the MCP server.")
    serve.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    serve.set_defaults(func=_cmd_serve)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help(sys.stderr)
        return 2
    return int(args.func(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
