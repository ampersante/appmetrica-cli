"""`appmetrica` command line entry point."""
from __future__ import annotations

import asyncio
import getpass
import json
import os
import sys

import click
import httpx

from appmetrica_cli import auth
from appmetrica_cli.client import AppMetricaClient, AppMetricaError
from appmetrica_cli.config import AppMetricaConfig


def _out(data) -> None:
    click.echo(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _err(msg: str, code: int = 1) -> None:
    click.echo(msg, err=True)
    sys.exit(code)


@click.group()
@click.version_option(package_name="appmetrica-cli")
def main() -> None:
    """Read-only AppMetrica analytics CLI (unofficial)."""
    os.umask(0o077)


@main.group(name="auth")
def auth_group() -> None:
    """Manage the AppMetrica OAuth token (stored in the OS credential store)."""


async def _list_apps(token: str) -> list[dict]:
    config = AppMetricaConfig(oauth_token=token)
    async with httpx.AsyncClient(
        headers={"Authorization": f"OAuth {token}"}, timeout=config.request_timeout
    ) as http:
        return await AppMetricaClient(http, config).list_applications()


@auth_group.command("login")
@click.option("--stdin", "from_stdin", is_flag=True, help="Read the token from stdin instead of a hidden prompt.")
def auth_login(from_stdin: bool) -> None:
    """Store the token in the OS credential store after checking it against the API."""
    try:
        if from_stdin:
            # Raw bytes: text-mode reading would treat a stray \r as end of line and truncate.
            raw = click.get_binary_stream("stdin").read(auth.MAX_FILE_BYTES + 1)
            token = auth.token_from_bytes(raw, "stdin")
        else:
            token = auth.validate_token(getpass.getpass("AppMetrica OAuth token: "))
        store = auth.require_store()  # before any network call: never send a token we cannot keep
    except auth.AuthError as e:
        _err(e.message, e.exit_code)
    try:
        apps = asyncio.run(_list_apps(token))
    except AppMetricaError as e:
        _err(f"Token check failed: {e.message}", auth.EXIT_API_REJECTED)
    try:
        store.set(token)
    except auth.AuthError as e:
        _err(e.message, e.exit_code)
    _out({"stored_in": store.name, "apps_visible": len(apps)})


@auth_group.command("status")
def auth_status() -> None:
    """Show where the token comes from and whether it works (never the token itself)."""
    try:
        token, source = auth.get_token()
    except auth.AuthError as e:
        _out({"source": "error", "valid": False, "error": e.message})
        sys.exit(e.exit_code)
    if not token:
        _out({"source": "none", "valid": False})
        sys.exit(auth.EXIT_API_REJECTED)
    try:
        apps = asyncio.run(_list_apps(token))
        _out({"source": source, "valid": True, "apps_visible": len(apps)})
    except AppMetricaError as e:
        _out({"source": source, "valid": False, "error": e.message})
        sys.exit(auth.EXIT_API_REJECTED)


@auth_group.command("logout")
def auth_logout() -> None:
    """Delete the token from the OS credential store."""
    try:
        deleted = auth.delete_token()
    except auth.AuthError as e:
        _err(e.message, e.exit_code)
    result: dict = {"deleted": deleted}
    still = auth.external_source()
    if still:
        result["warning"] = f"{still} is still set and will keep supplying a token"
    _out(result)


@main.command("mcp")
def mcp_command() -> None:
    """Run the MCP server over stdio (for Claude Code / Desktop / Cursor)."""
    from appmetrica_cli.server import mcp

    mcp.run()
