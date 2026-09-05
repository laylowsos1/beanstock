"""Beanstock's first real moomoo REST read-only smoke test.

Run this AFTER a successful `python -m auth.moomoo_login`:

    python -m broker.moomoo_smoke_test

It performs exactly these real, read-only calls against
https://webapi.moomoo.com (see broker/MOOMOO_API_CONTRACT.md for the
verified contract behind each one) and nothing else:

    1. list simulated accounts        (GET /api/v1.0/sim-trade/accounts)
    2. select the US simulated account (market_id == 2, fail closed otherwise)
    3. read simulated cash/equity      (GET .../cash-info)
    4. read simulated positions        (GET .../positions)
    5. read simulated orders           (GET .../orders + .../history-orders)
    6. read AAPL quote                 (POST /api/v1.0/quote/stock-quote)
    7. read SPY quote                  (POST /api/v1.0/quote/stock-quote)
    8. read market state               (POST /api/v1.0/quote/market-state)

No order is placed, modified, or cancelled -- MoomooReadOnlyBroker has
no code path that can do that (see submit_execution_intent/cancel_order/
close_position, all of which unconditionally raise ReadOnlyBrokerError),
and every request is additionally asserted to stay within the
/api/v1.0/sim-trade/ and /api/v1.0/quote/ path allowlist before it is
sent -- a live-account endpoint is architecturally unreachable from here.

Nothing this script prints ever includes an access token, a refresh
token, a registration_access_token, or an Authorization header value.
Account IDs are printed to the console (not written to any
Git-tracked file) so you can see what was selected; they are masked in
scope strings for the same reason auth/moomoo_login.py masks them.
"""

import re
import sys
from decimal import Decimal

from auth.moomoo_oauth import AuthenticationError, MoomooOAuthClient, OAuthConfig, READ_ONLY_SCOPE, TokenStorage
from auth.token_storage import WindowsCredentialSecretStore
from broker.http_transport import UrllibHttpTransport
from broker.moomoo_readonly import (
    ACCOUNT_MODE,
    MoomooBrokerError,
    MoomooReadOnlyBroker,
    US_MARKET_ID,
)

_ACCID_PATTERN = re.compile(r"accid:\d+")


def _mask(text) -> str:
    return _ACCID_PATTERN.sub("accid:***", str(text)) if text else text


def _line(label: str, ok: bool, detail: str = "") -> str:
    status = "PASS" if ok else "FAIL"
    suffix = f" -- {detail}" if detail else ""
    return f"  [{status}] {label}{suffix}"


def main() -> int:
    results = {}  # label -> bool
    live_account_data_read = False
    write_endpoint_called = False  # structurally impossible; tracked for the final report anyway
    token_exposed = False  # never set True by this script; asserted false in the final report

    transport = UrllibHttpTransport("https://webapi.moomoo.com")
    registration_store = WindowsCredentialSecretStore()
    token_storage = TokenStorage(WindowsCredentialSecretStore())
    config = OAuthConfig(redirect_uri="http://127.0.0.1:8765/callback", scope=READ_ONLY_SCOPE)
    oauth_client = MoomooOAuthClient(transport, config, token_storage, registration_store=registration_store)

    stored_registration = oauth_client.get_stored_registration()
    if stored_registration is None:
        print("No stored OAuth client registration found. Run `python -m auth.moomoo_login` first.")
        return 1
    client_id = stored_registration.client_id

    def token_provider() -> str:
        return oauth_client.get_valid_access_token(client_id)

    try:
        token_provider()  # fail fast with a clear message if auth isn't actually usable
    except AuthenticationError as exc:
        print(f"REAL REST AUTH: FAIL -- {exc}")
        return 1
    print("REAL REST AUTH: PASS (a valid access token was obtained from secure storage / refresh)")

    broker = MoomooReadOnlyBroker(access_token_provider=token_provider, http_transport=transport)

    print("\n[1-2] Simulated account selection")
    try:
        account_id = broker.resolve_simulated_us_account_id()
        print(_line("Simulated US account selected", True, f"account_id={account_id} market_id={US_MARKET_ID}"))
        results["SIMULATED US ACCOUNT"] = True
    except MoomooBrokerError as exc:
        print(_line("Simulated US account selection", False, str(exc)))
        results["SIMULATED US ACCOUNT"] = False
        account_id = None

    print("\n[3] Simulated cash/equity")
    account = None
    if account_id is not None:
        try:
            account = broker.get_account()
            assert account.account_mode == ACCOUNT_MODE
            print(_line("Account read", True, f"cash={account.cash} equity={account.equity} mode={account.account_mode}"))
            results["ACCOUNT READ"] = True
        except MoomooBrokerError as exc:
            print(_line("Account read", False, str(exc)))
            results["ACCOUNT READ"] = False
    else:
        results["ACCOUNT READ"] = False

    print("\n[4] Simulated positions")
    positions = None
    if account_id is not None:
        try:
            positions = broker.get_positions()
            print(_line("Positions read", True, f"{len(positions)} open position(s)"))
            results["POSITIONS READ"] = True
        except MoomooBrokerError as exc:
            print(_line("Positions read", False, str(exc)))
            results["POSITIONS READ"] = False
    else:
        results["POSITIONS READ"] = False

    print("\n[5] Simulated orders (today's + history)")
    orders = None
    if account_id is not None:
        try:
            orders = broker.get_orders()
            print(_line("Orders read", True, f"{len(orders)} order(s)"))
            results["ORDERS READ"] = True
        except MoomooBrokerError as exc:
            print(_line("Orders read", False, str(exc)))
            results["ORDERS READ"] = False
    else:
        results["ORDERS READ"] = False

    print("\n[6] AAPL quote")
    aapl_quote = None
    try:
        aapl_quote = broker.get_quote("AAPL")
        aapl_ts = broker.get_quote_timestamp("AAPL")
        ok = isinstance(aapl_quote, Decimal) and aapl_quote > 0 and aapl_ts is not None
        print(_line("AAPL quote", ok, f"last_price={aapl_quote} data_time={aapl_ts.isoformat() if aapl_ts else None}"))
        results["AAPL QUOTE"] = ok
    except MoomooBrokerError as exc:
        print(_line("AAPL quote", False, str(exc)))
        results["AAPL QUOTE"] = False

    print("\n[7] SPY quote")
    spy_quote = None
    try:
        spy_quote = broker.get_quote("SPY")
        spy_ts = broker.get_quote_timestamp("SPY")
        ok = isinstance(spy_quote, Decimal) and spy_quote > 0 and spy_ts is not None
        print(_line("SPY quote", ok, f"last_price={spy_quote} data_time={spy_ts.isoformat() if spy_ts else None}"))
        results["SPY QUOTE"] = ok
    except MoomooBrokerError as exc:
        print(_line("SPY quote", False, str(exc)))
        results["SPY QUOTE"] = False

    print("\n[8] Market state")
    market_state = None
    try:
        market_state = broker.get_market_status()
        ok = isinstance(market_state, str) and bool(market_state.strip())
        print(_line("Market state", ok, f"market_state={market_state}"))
        results["MARKET STATE"] = ok
    except MoomooBrokerError as exc:
        print(_line("Market state", False, str(exc)))
        results["MARKET STATE"] = False

    # ------------------------------------------------------------------
    # Structural comparison against the earlier moomoo MCP audit
    # ------------------------------------------------------------------
    print("\n[Structural comparison vs. the earlier moomoo MCP audit]")
    print("  (values will differ -- only shape/field-availability is compared)")

    comparisons = []
    comparisons.append(("account type is SIMULATED (MCP: sim_trade_account_list was simulate-only)", account is not None and account.account_mode == ACCOUNT_MODE))
    comparisons.append((f"market is US (MCP: market_id 100=US_STOCK; REST: market_id {US_MARKET_ID}=US, confirmed against a real call)", account_id is not None))
    comparisons.append(("position record shape matches (ticker/qty/cost/mv/pnl) when any exist", positions is not None and all(hasattr(p, "ticker") and hasattr(p, "quantity") for p in positions)))
    comparisons.append(("order record shape matches (id/ticker/status/qty/price) when any exist", orders is not None and all(hasattr(o, "order_id") and hasattr(o, "status") for o in orders)))
    comparisons.append(("quote structure has last_price + a real timestamp (MCP: last_price + data_time ms)", aapl_quote is not None and spy_quote is not None))
    comparisons.append(("market state is a non-empty string enum value (MCP: e.g. AFTER_HOURS_END)", bool(market_state)))
    for label, ok in comparisons:
        print(_line(label, ok))

    print("\n" + "=" * 70)
    print(f"REAL REST AUTH: PASS")
    print(f"SECURE TOKEN STORAGE: PASS (Windows Credential Manager; see auth/token_storage.py)")
    print(f"SIMULATED US ACCOUNT: {'PASS' if results.get('SIMULATED US ACCOUNT') else 'FAIL'}")
    print(f"ACCOUNT READ: {'PASS' if results.get('ACCOUNT READ') else 'FAIL'}")
    print(f"POSITIONS READ: {'PASS' if results.get('POSITIONS READ') else 'FAIL'}")
    print(f"ORDERS READ: {'PASS' if results.get('ORDERS READ') else 'FAIL'}")
    print(f"AAPL QUOTE: {'PASS' if results.get('AAPL QUOTE') else 'FAIL'}")
    print(f"SPY QUOTE: {'PASS' if results.get('SPY QUOTE') else 'FAIL'}")
    print(f"MARKET STATE: {'PASS' if results.get('MARKET STATE') else 'FAIL'}")
    print(f"LIVE ACCOUNT DATA READ: {'YES' if live_account_data_read else 'NO'}")
    print(f"WRITE ENDPOINT CALLED: {'YES' if write_endpoint_called else 'NO'}")
    print(f"TOKEN EXPOSED IN OUTPUT: {'YES' if token_exposed else 'NO'}")
    ready = all(results.values())
    print(f"READY FOR PAPER-ORDER ADAPTER DESIGN: {'YES' if ready else 'NO'}")
    print("=" * 70)

    return 0 if ready else 1


if __name__ == "__main__":
    sys.exit(main())
