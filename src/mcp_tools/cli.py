"""Command line entry point: `mcp-tools <command>`."""

import argparse
import sys
from collections.abc import Sequence

from mcp_tools import __version__
from mcp_tools.config import Settings


def _cmd_serve(args: argparse.Namespace) -> int:
    settings = Settings()
    if args.host:
        settings.http_host = args.host
    if args.port:
        settings.http_port = args.port
    if args.transport == "stdio":
        from mcp_tools.server.build import build_server

        build_server(settings).mcp.run("stdio")
        return 0
    from mcp_tools.server.http.app import HttpConfigError, serve_http

    try:
        return serve_http(
            settings,
            no_auth=args.no_auth,
            allow_insecure=args.insecure_http,
            tls_cert=args.tls_cert,
            tls_key=args.tls_key,
        )
    except HttpConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _cmd_key_rotate(args: argparse.Namespace) -> int:
    import secrets

    from mcp_tools.core.datadir import prepare_data_dir, write_private
    from mcp_tools.server.http.gate import hash_api_key

    data = prepare_data_dir(Settings().data_dir)
    key = secrets.token_urlsafe(32)
    write_private(data.api_key_file, hash_api_key(key).encode("ascii"))
    print("New API key (shown once; restart the HTTP server to use it):", file=sys.stderr)
    print(key)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mcp-tools", description="Generative media MCP server.")
    parser.add_argument("--version", action="version", version=f"mcp-tools {__version__}")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="Run the MCP server.")
    serve.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    serve.add_argument("--host", help="HTTP bind address (default 127.0.0.1).")
    serve.add_argument("--port", type=int, help="HTTP port (default 8000).")
    serve.add_argument("--no-auth", action="store_true", help="HTTP without an API key; loopback only.")
    serve.add_argument("--tls-cert", help="TLS certificate file (PEM).")
    serve.add_argument("--tls-key", help="TLS private key file (PEM).")
    serve.add_argument(
        "--insecure-http", action="store_true", help="Allow plain HTTP on a non-loopback address (behind a TLS proxy)."
    )
    serve.set_defaults(func=_cmd_serve)

    key = sub.add_parser("key", help="Manage the HTTP API key.")
    key_sub = key.add_subparsers(dest="key_command", required=True)
    rotate = key_sub.add_parser("rotate", help="Replace the API key and print the new one.")
    rotate.set_defaults(func=_cmd_key_rotate)
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
