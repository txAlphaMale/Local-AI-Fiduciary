#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""
snaptrade_register.py — one-time SnapTrade setup (SDK 13.x).

PERSONAL keys (client ID starts with "PERS-", the free key from your SnapTrade dashboard):
    There is nothing to register — the user is built into the key. This script just prints a Connection
    Portal link to link a brokerage (or SnapTrade's SANDBOX test broker with --sandbox). You then only need
    SNAPTRADE_CLIENT_ID and SNAPTRADE_CONSUMER_KEY (SETTINGS → SECRETS & KEYS, or .env). You can also use the
    in-app button: SETTINGS → ACCOUNTS → SnapTrade → CONNECT.

COMMERCIAL keys:
    Registers a SnapTrade user, prints the Connection Portal link, and prints SNAPTRADE_USER_ID /
    SNAPTRADE_USER_SECRET to save alongside the client ID and consumer key.

Then set PORTFOLIO_PROVIDER=snaptrade and rebuild — the Fiduciary pulls holdings live across every linked brokerage.

Usage:
  pip install "snaptrade-python-sdk>=13"
  export SNAPTRADE_CLIENT_ID=...  SNAPTRADE_CONSUMER_KEY=...
  python3 snaptrade_register.py [--sandbox] [optional-user-id (commercial keys only)]

The connect link expires in about 5 minutes; just re-run for a fresh one.
"""
import os
import sys
import uuid

PERSONAL_PREFIX = "PERS-"


def _body(r):
    return getattr(r, "body", r)


def main(argv):
    sandbox = "--sandbox" in argv
    args = [a for a in argv[1:] if not a.startswith("--")]
    cid = os.environ.get("SNAPTRADE_CLIENT_ID")
    ckey = os.environ.get("SNAPTRADE_CONSUMER_KEY")
    if not cid or not ckey:
        sys.exit("Set SNAPTRADE_CLIENT_ID and SNAPTRADE_CONSUMER_KEY (from your SnapTrade dashboard) first.")

    from snaptrade_client import SnapTrade, SnapTradeAuth
    personal = cid.startswith(PERSONAL_PREFIX)
    login_kwargs = {"connection_type": "read"}
    if sandbox:
        login_kwargs["broker"] = "SANDBOX"

    if personal:
        st = SnapTrade(auth=SnapTradeAuth.personal_api_key(consumer_key=ckey, client_id=cid))
        print("Personal key detected — no user to register.")
        user_id = user_secret = None
    else:
        st = SnapTrade(auth=SnapTradeAuth.commercial_api_key(consumer_key=ckey, client_id=cid))
        user_id = args[0] if args else f"fiduciary-{uuid.uuid4().hex[:8]}"
        print(f"Registering SnapTrade user: {user_id}")
        reg = _body(st.authentication.register_snap_trade_user(user_id=user_id))
        user_secret = reg.get("userSecret") if hasattr(reg, "get") else None
        if not user_secret:
            sys.exit(f"Registration returned no userSecret. Raw response: {reg}")
        login_kwargs.update(user_id=user_id, user_secret=user_secret)

    print("\nGenerating a Connection Portal link" + (" for the SANDBOX test broker" if sandbox else "") + " (valid ~5 minutes)...")
    login = _body(st.authentication.login_snap_trade_user(**login_kwargs))
    uri = login.get("redirectURI") if hasattr(login, "get") else None
    print(f"  Link: {uri or ('(no redirectURI; raw: %s)' % login)}")

    print("\n--- Save these (SETTINGS → SECRETS & KEYS or .env), set PORTFOLIO_PROVIDER=snaptrade, then rebuild ---")
    print(f"SNAPTRADE_CLIENT_ID={cid}")
    print("SNAPTRADE_CONSUMER_KEY=<your consumer key>")
    if not personal:
        print(f"SNAPTRADE_USER_ID={user_id}")
        print(f"SNAPTRADE_USER_SECRET={user_secret}")
    print("\nOpen the link, connect your brokerage(s), and you're done. Re-run any time for a fresh link.")


if __name__ == "__main__":
    main(sys.argv)
