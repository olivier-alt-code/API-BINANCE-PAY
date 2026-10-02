"""Owner command line: generate keys and manage clients/tokens directly in the database.

Usage::

    python -m app.cli generate-admin-key          # value for ADMIN_API_KEYS
    python -m app.cli generate-encryption-key     # value for CREDENTIALS_ENCRYPTION_KEY
    python -m app.cli create-client "Olivier"     # creates a client + prints its token ONCE
    python -m app.cli create-token 1 --name tienda [--expires-in-days 90]
    python -m app.cli list-clients
    python -m app.cli list-tokens 1
    python -m app.cli revoke-token 7

Same operations are available over HTTP under /v1/admin/* with the admin key.
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import sys
from collections.abc import Sequence

from app.config import get_settings
from app.core.encryption import generate_key
from app.db.session import get_engine, get_sessionmaker
from app.services.tenants import IssuedToken, TenantError, TenantService


def _print_token(token: IssuedToken) -> None:
    print(f"token id:   {token.id}")
    print(f"name:       {token.name}")
    print(f"expires:    {token.expires_at or 'never'}")
    print()
    print("TOKEN (shown only once, store it now):")
    print(token.token)


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    service = TenantService(
        get_sessionmaker(), last_used_update_seconds=settings.token_last_used_update_seconds
    )
    try:
        if args.command == "create-client":
            tenant, token = await service.create_tenant(
                args.name, token_name=args.token_name, expires_in_days=args.expires_in_days
            )
            print(f"client id:  {tenant.id} ({tenant.name})")
            _print_token(token)
        elif args.command == "create-token":
            _print_token(await service.issue_token(args.client_id, args.name, args.expires_in_days))
        elif args.command == "list-clients":
            for t in await service.list_tenants():
                state = "enabled" if t.enabled else "DISABLED"
                binance = "binance:yes" if t.binance_configured else "binance:no"
                print(f"{t.id:>4}  {t.name:<30} {state:<8} tokens:{t.active_tokens}  {binance}")
        elif args.command == "list-tokens":
            for tok in await service.list_tokens(args.client_id):
                status = "revoked" if tok.revoked_at else "active"
                print(
                    f"{tok.id:>4}  {tok.prefix}…  {tok.name:<24} {status}  last used: "
                    f"{tok.last_used_at or 'never'}"
                )
        elif args.command == "revoke-token":
            if not await service.revoke_token(args.token_id):
                print("token not found or already revoked", file=sys.stderr)
                return 1
            print(f"token {args.token_id} revoked")
        elif args.command == "set-client-enabled":
            tenant = await service.set_enabled(args.client_id, args.enabled == "true")
            print(f"client {tenant.id} enabled={tenant.enabled}")
    except TenantError as exc:
        print(exc.public_message, file=sys.stderr)
        return 1
    finally:
        await get_engine().dispose()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("generate-admin-key", help="print a new random admin key")
    sub.add_parser("generate-encryption-key", help="print a new CREDENTIALS_ENCRYPTION_KEY")

    p = sub.add_parser("create-client", help="create a client and its first token")
    p.add_argument("name")
    p.add_argument("--token-name", default="default")
    p.add_argument("--expires-in-days", type=int, default=None)

    p = sub.add_parser("create-token", help="issue another token for a client")
    p.add_argument("client_id", type=int)
    p.add_argument("--name", default="default")
    p.add_argument("--expires-in-days", type=int, default=None)

    sub.add_parser("list-clients")
    p = sub.add_parser("list-tokens")
    p.add_argument("client_id", type=int)
    p = sub.add_parser("revoke-token")
    p.add_argument("token_id", type=int)
    p = sub.add_parser("set-client-enabled")
    p.add_argument("client_id", type=int)
    p.add_argument("enabled", choices=["true", "false"])

    args = parser.parse_args(argv)
    if args.command == "generate-admin-key":
        print(secrets.token_urlsafe(48))
        return 0
    if args.command == "generate-encryption-key":
        print(generate_key())
        return 0
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
