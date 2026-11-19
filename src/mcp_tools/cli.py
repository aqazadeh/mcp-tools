"""Command line entry point: `mcp-tools <command>`."""

import argparse
import sys
from collections.abc import Sequence
from typing import Any

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


def _cmd_catalog_validate(args: argparse.Namespace) -> int:
    from mcp_tools.core.catalog.loader import CatalogError, load_catalog

    try:
        catalog = load_catalog(Settings().catalog_paths)
    except CatalogError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    models = catalog.all()
    by_provider: dict[str, int] = {}
    for model in models:
        by_provider[model.provider] = by_provider.get(model.provider, 0) + 1
    unverified = [m.id for m in models if m.provider != "mock" and not m.verified_at]
    print(f"catalog ok: {len(models)} models ({', '.join(f'{k}: {v}' for k, v in sorted(by_provider.items()))})")
    if unverified:
        print(f"not yet verified against the provider ({len(unverified)}): {', '.join(unverified)}")
    return 0


def _cmd_credits(args: argparse.Namespace) -> int:
    import anyio

    from mcp_tools.core.platform import Platform

    async def run() -> int:
        platform = Platform(Settings(log_level="WARNING"), run_jobs=False)
        async with platform.running():
            workspace = platform.principal.workspace_id
            if args.credits_command == "grant":
                if args.amount <= 0:
                    print("error: the amount must be positive", file=sys.stderr)
                    return 2
                async with platform.store.write_tx() as conn:
                    await platform.ledger.grant(conn, workspace, args.amount, note=args.note or "cli grant")
            async with platform.store.read_tx() as conn:
                state = await platform.ledger.balance(conn, workspace)
        print(f"balance: {state.balance} credits; spent today: {state.spent_today}; daily cap: {state.daily_cap}")
        return 0

    return anyio.run(run)


def _cmd_doctor(args: argparse.Namespace) -> int:
    import shutil
    import sqlite3

    import anyio

    from mcp_tools.core.platform import Platform

    async def run() -> int:
        settings = Settings(log_level="WARNING")
        platform = Platform(settings, run_jobs=False)
        problems = 0
        async with platform.running():
            print(f"data directory : {platform.data.root}")
            print(f"sqlite         : {sqlite3.sqlite_version} (WAL, foreign keys on)")
            egress = platform.importer.egress
            note = {
                "direct": "connections are pinned to vetted public addresses",
                "proxy": "through the environment's proxy; only the URL text can be checked (weaker)",
                "off": "URL imports and provider downloads are disabled",
            }[egress]
            print(f"egress         : {egress} - {note}")
            print(f"ffprobe        : {shutil.which('ffprobe') or 'not found (video/audio metadata will be missing)'}")
            print(f"import folders : {', '.join(str(r) for r in platform.importer.roots) or 'none'}")
            for name in platform.providers.names():
                state = platform.providers.availability(name)
                count = sum(1 for m in platform.catalog.all() if m.provider == name)
                status = "ready" if state.available else f"off - {state.reason}"
                print(f"provider {name:<10}: {status} ({count} models)")
            if args.live:
                for model in platform.catalog.all():
                    provider = platform.providers.peek(model.provider)
                    describe = getattr(provider, "describe_model", None)
                    if describe is None or not platform.providers.availability(model.provider).available:
                        continue
                    try:
                        info = await describe(model.provider_ref)
                    except Exception as exc:
                        problems += 1
                        print(f"  FAIL {model.id} ({model.provider_ref}): {exc}")
                        continue
                    sent = _provider_fields(model)
                    unknown = sorted(sent - set(info["inputs"])) if "inputs" in info else []
                    if unknown:
                        problems += 1
                        print(f"  WARN {model.id}: the provider does not list inputs {unknown}")
                    else:
                        print(f"  ok   {model.id} ({model.provider_ref})")
        return 1 if problems else 0

    return anyio.run(run)


def _provider_fields(model: Any) -> set[str]:
    """The provider-side input names a catalog entry would send."""
    renamed = model.common_map
    fields = {p.provider_key or p.name for p in model.parameters}
    if model.prompt != "none":
        fields.add(renamed.get("prompt", "prompt"))
    if model.aspect_ratios:
        fields.add(renamed.get("aspect_ratio", "aspect_ratio"))
    fields.update(m.provider_field or m.role for m in model.medias)
    return fields


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

    catalog = sub.add_parser("catalog", help="Work with the model catalog.")
    catalog_sub = catalog.add_subparsers(dest="catalog_command", required=True)
    catalog_sub.add_parser("validate", help="Load every catalog file and report problems.").set_defaults(
        func=_cmd_catalog_validate
    )

    credits = sub.add_parser("credits", help="Show or add credits (the only way to add them).")
    credits_sub = credits.add_subparsers(dest="credits_command", required=True)
    credits_sub.add_parser("balance", help="Show the balance.").set_defaults(func=_cmd_credits)
    grant = credits_sub.add_parser("grant", help="Add credits (1 credit = 0.001 USD of estimated spend).")
    grant.add_argument("amount", type=int)
    grant.add_argument("--note", default="")
    grant.set_defaults(func=_cmd_credits)

    doctor = sub.add_parser("doctor", help="Check the installation and show what is configured.")
    doctor.add_argument("--live", action="store_true", help="Also ask each configured provider about its models.")
    doctor.set_defaults(func=_cmd_doctor)

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
