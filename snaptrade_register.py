#!/usr/bin/env python3
"""
snaptrade_register.py — one-time SnapTrade setup.

Registers a SnapTrade user, prints a Connection Portal link to link your brokerage(s),
and prints the USER_ID / USER_SECRET to paste into .env. Then set PORTFOLIO_PROVIDER=snaptrade
and rebuild — the Fiduciary will pull holdings live across every linked brokerage.

Prereqs:
  1. SnapTrade dev account (free) -> https://snaptrade.com/  gives a Client ID + Consumer Key.
  2. pip install snaptrade-python-sdk
  3. export SNAPTRADE_CLIENT_ID=...   SNAPTRADE_CONSUMER_KEY=...
  4. python3 snaptrade_register.py [optional-user-id]

Note: SDK method names/signatures can shift between versions. If a call errors, check
https://github.com/passiv/snaptrade-python-sdk and adjust the two calls below.
"""
import os
import sys
import uuid


def _body(r):
    return getattr(r, "body", r)


def main():
    cid = os.environ.get("SNAPTRADE_CLIENT_ID")
    ckey = os.environ.get("SNAPTRADE_CONSUMER_KEY")
    if not cid or not ckey:
        sys.exit("Set SNAPTRADE_CLIENT_ID and SNAPTRADE_CONSUMER_KEY (from your SnapTrade dev signup) first.")
    user_id = sys.argv[1] if len(sys.argv) > 1 else f"fiduciary-{uuid.uuid4().hex[:8]}"

    from snaptrade_client import SnapTrade
    st = SnapTrade(consumer_key=ckey, client_id=cid)

    print(f"Registering SnapTrade user: {user_id}")
    reg = _body(st.authentication.register_snap_trade_user(body={"userId": user_id}))
    user_secret = reg.get("userSecret") if isinstance(reg, dict) else None
    if not user_secret:
        sys.exit(f"Registration returned no userSecret. Raw response: {reg}")
    print(f"  userSecret: {user_secret}")

    print("\nGenerating Connection Portal link (open it in a browser to link a brokerage)...")
    login = _body(st.authentication.login_snap_trade_user(
        query_params={"userId": user_id, "userSecret": user_secret}))
    uri = login.get("redirectURI") if isinstance(login, dict) else None
    print(f"  Link: {uri or ('(no redirectURI; raw: %s)' % login)}")

    print("\n--- Add these to .env, set PORTFOLIO_PROVIDER=snaptrade, then rebuild ---")
    print(f"SNAPTRADE_USER_ID={user_id}")
    print(f"SNAPTRADE_USER_SECRET={user_secret}")
    print("\nOpen the link above, connect your brokerage(s), and you're done. Re-run any time to add more.")


if __name__ == "__main__":
    main()
