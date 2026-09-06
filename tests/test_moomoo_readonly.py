"""Tests for MoomooReadOnlyBroker (broker/moomoo_readonly.py) and the
OAuth 2.1 + PKCE client it relies on (auth/moomoo_oauth.py).

Every fixture in this file mirrors the OFFICIAL response envelope and
field names verified against https://open.moomoo.com/api/... on the
contract-verification pass documented in broker/MOOMOO_API_CONTRACT.md
-- not an invented schema, and not the older openapi.moomoo.com
OpenD/SDK docs' field names.

Every HTTP interaction goes through FakeHttpTransport, which never
touches a socket. test_no_real_network_call_possible additionally
proves that a real socket connection would be caught if one were ever
attempted.
"""

import json
import os
import socket
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from broker.http_transport import HttpResponse, HttpTransport, UrllibHttpTransport
from broker.moomoo_readonly import (
    ACCOUNTS_PATH,
    CASH_INFO_PATH_TEMPLATE,
    HISTORY_ORDERS_PATH_TEMPLATE,
    MARKET_STATE_PATH,
    OPEN_ORDERS_PATH_TEMPLATE,
    POSITIONS_PATH_TEMPLATE,
    QUOTE_PATH,
    US_MARKET_ID,
    AmbiguousSimulatedAccountError,
    LiveAccountRejectedError,
    MalformedResponseError,
    MoomooApiError,
    MoomooAuthenticationError,
    MoomooReadOnlyBroker,
    NoSimulatedAccountError,
    ReadOnlyBrokerError,
)
from auth.token_storage import InMemorySecretStore
from auth.moomoo_oauth import (
    DEFAULT_AUTHORIZE_PATH,
    DEFAULT_REGISTER_PATH,
    DEFAULT_TOKEN_PATH,
    AuthenticationError,
    CallbackStateError,
    MoomooOAuthClient,
    OAuthConfig,
    TokenRefreshError,
    TokenSet,
    TokenStorage,
    generate_pkce_pair,
    generate_state,
)

SECRET_MARKER = "SUPER-SECRET-TOKEN-abc123xyz-do-not-leak"


# ---------------------------------------------------------------------
# Fake transport -- no test using this ever opens a socket.
# ---------------------------------------------------------------------


class FakeHttpTransport(HttpTransport):
    def __init__(self):
        self._get_queue: dict = {}
        self._post_queue: dict = {}
        self.calls: list = []

    def queue_get(self, path: str, response: HttpResponse) -> None:
        self._get_queue.setdefault(path, []).append(response)

    def queue_post(self, path: str, response: HttpResponse) -> None:
        self._post_queue.setdefault(path, []).append(response)

    def get(self, path, *, params=None, headers=None, timeout=10.0):
        self.calls.append(("GET", path, params, headers))
        return self._resolve(self._get_queue, path)

    def post(self, path, *, form=None, json_body=None, headers=None, timeout=10.0):
        self.calls.append(("POST", path, form if form is not None else json_body, headers))
        return self._resolve(self._post_queue, path)

    def _resolve(self, table, path):
        queue = table.get(path)
        if not queue:
            raise AssertionError(f"FakeHttpTransport: no response queued for {path!r}")
        return queue.pop(0) if len(queue) > 1 else queue[0]


def envelope(data: dict, ret_code: int = 0, ret_msg: str = "success") -> dict:
    return {"ret_code": ret_code, "ret_msg": ret_msg, "data": data}


def json_response(status_code: int, payload) -> HttpResponse:
    return HttpResponse(status_code=status_code, body=json.dumps(payload))


def accounts_envelope(us_account_ids=None, other_accounts=None):
    """other_accounts: list of (market_id, account_id) for non-US sim accounts."""
    accounts = [{"account_id": acc_id, "market_id": US_MARKET_ID} for acc_id in (us_account_ids or [])]
    for market_id, acc_id in other_accounts or []:
        accounts.append({"account_id": acc_id, "market_id": market_id})
    return envelope({"accounts": accounts})


def make_broker(transport, *, account_id_override=None, token=None):
    return MoomooReadOnlyBroker(
        access_token_provider=token or (lambda: "fake-access-token"),
        http_transport=transport,
        simulated_account_id=account_id_override,
    )


# ---------------------------------------------------------------------
# Endpoint paths match the verified contract (broker/MOOMOO_API_CONTRACT.md)
# ---------------------------------------------------------------------


def test_endpoint_paths_match_verified_contract():
    assert ACCOUNTS_PATH == "/api/v1.0/sim-trade/accounts"
    assert CASH_INFO_PATH_TEMPLATE == "/api/v1.0/sim-trade/{acc_id}/cash-info"
    assert POSITIONS_PATH_TEMPLATE == "/api/v1.0/sim-trade/{acc_id}/positions"
    assert OPEN_ORDERS_PATH_TEMPLATE == "/api/v1.0/sim-trade/{acc_id}/orders"
    assert HISTORY_ORDERS_PATH_TEMPLATE == "/api/v1.0/sim-trade/{acc_id}/history-orders"
    assert QUOTE_PATH == "/api/v1.0/quote/stock-quote"
    assert MARKET_STATE_PATH == "/api/v1.0/quote/market-state"


def test_live_account_endpoints_are_never_reachable():
    transport = FakeHttpTransport()
    broker = make_broker(transport)
    # Real, documented live-account endpoints -- confirmed to exist by
    # official docs, and confirmed here to be permanently unreachable.
    for live_path in (
        "/api/v1.0/accounts/authorized_trd_accs",
        "/api/v1.0/accounts/some-acc/funds",
        "/api/v1.0/trading/trade/place-order",
    ):
        with pytest.raises(LiveAccountRejectedError):
            broker._guard_path(live_path)
    assert transport.calls == []


# ---------------------------------------------------------------------
# Simulated account resolution
# ---------------------------------------------------------------------


def test_simulated_us_account_selected_correctly():
    transport = FakeHttpTransport()
    transport.queue_get(ACCOUNTS_PATH, json_response(200, accounts_envelope(us_account_ids=["9000001"])))
    transport.queue_get(
        CASH_INFO_PATH_TEMPLATE.format(acc_id="9000001"),
        json_response(200, envelope({"balance": "1000000", "total_asset": "1000000"})),
    )

    broker = make_broker(transport)
    account = broker.get_account()

    assert account.cash == Decimal("1000000")
    assert any(c[1] == CASH_INFO_PATH_TEMPLATE.format(acc_id="9000001") for c in transport.calls)


def test_market_id_filtering_selects_only_us_among_multiple_markets():
    # A real account-list call auto-creates one sim account per market.
    # market_id==100 (US) and ==1 (HK) are confirmed against a real
    # response (see broker/MOOMOO_API_CONTRACT.md); the other two ids
    # here are arbitrary non-100 sentinels, not asserted real values --
    # this test only exercises the filtering logic.
    transport = FakeHttpTransport()
    transport.queue_get(
        ACCOUNTS_PATH,
        json_response(
            200,
            accounts_envelope(
                us_account_ids=["us-acc"],
                other_accounts=[(1, "hk-acc"), (11, "futures-acc"), (999, "other-acc")],
            ),
        ),
    )
    transport.queue_get(
        CASH_INFO_PATH_TEMPLATE.format(acc_id="us-acc"),
        json_response(200, envelope({"balance": "500", "total_asset": "500"})),
    )

    broker = make_broker(transport)
    broker.get_account()

    cash_calls = [c for c in transport.calls if c[0] == "GET" and "cash-info" in c[1]]
    assert cash_calls[0][1] == CASH_INFO_PATH_TEMPLATE.format(acc_id="us-acc")


def test_account_list_matches_real_observed_response_shape():
    # Exact response captured from a real authenticated call to
    # GET /api/v1.0/sim-trade/accounts during the first live smoke test
    # (see broker/MOOMOO_API_CONTRACT.md) -- not an invented fixture.
    # This is what caught US_MARKET_ID being 2 instead of the real 100.
    real_response = {
        "ret_code": 0,
        "ret_msg": "success",
        "data": {
            "accounts": [
                {
                    "account_id": "9000001",
                    "account_title": "美股融资融券模拟账户",
                    "account_type": 1,
                    "broker_id": 0,
                    "intra_account_id": 0,
                    "market_id": 100,
                },
                {
                    "account_id": "9000002",
                    "account_title": "港股模拟账户",
                    "account_type": 1,
                    "broker_id": 0,
                    "intra_account_id": 0,
                    "market_id": 1,
                },
                {
                    "account_id": "9000003",
                    "account_title": "美国期货模拟账户",
                    "account_type": 1,
                    "broker_id": 0,
                    "intra_account_id": 0,
                    "market_id": 11,
                },
            ]
        },
    }
    assert US_MARKET_ID == 100

    transport = FakeHttpTransport()
    transport.queue_get(ACCOUNTS_PATH, json_response(200, real_response))
    transport.queue_get(
        CASH_INFO_PATH_TEMPLATE.format(acc_id="9000001"),
        json_response(200, envelope({"balance": "1000000", "total_asset": "1000000"})),
    )

    broker = make_broker(transport)
    assert broker.resolve_simulated_us_account_id() == "9000001"
    broker.get_account()


def test_no_simulated_us_account_found_fails_closed():
    transport = FakeHttpTransport()
    transport.queue_get(ACCOUNTS_PATH, json_response(200, accounts_envelope(other_accounts=[(1, "hk-acc")])))
    broker = make_broker(transport)
    with pytest.raises(NoSimulatedAccountError):
        broker.get_account()


def test_multiple_ambiguous_simulated_us_accounts_fail_closed():
    transport = FakeHttpTransport()
    transport.queue_get(ACCOUNTS_PATH, json_response(200, accounts_envelope(us_account_ids=["s1", "s2"])))
    broker = make_broker(transport)
    with pytest.raises(AmbiguousSimulatedAccountError):
        broker.get_account()


def test_misconfigured_override_from_another_market_cannot_substitute():
    # A caller mistakenly configures simulated_account_id to an id that
    # belongs to a DIFFERENT market's sim account (not US). It must be
    # rejected, never silently adopted.
    transport = FakeHttpTransport()
    transport.queue_get(
        ACCOUNTS_PATH, json_response(200, accounts_envelope(us_account_ids=["genuine-us"], other_accounts=[(1, "hk-acc")]))
    )
    broker = make_broker(transport, account_id_override="hk-acc")
    with pytest.raises(NoSimulatedAccountError):
        broker.get_account()


# ---------------------------------------------------------------------
# Cash / account mapping
# ---------------------------------------------------------------------


def test_account_cash_response_maps_correctly():
    transport = FakeHttpTransport()
    transport.queue_get(ACCOUNTS_PATH, json_response(200, accounts_envelope(us_account_ids=["acc-1"])))
    transport.queue_get(
        CASH_INFO_PATH_TEMPLATE.format(acc_id="acc-1"),
        json_response(200, envelope({"balance": "1234.56", "total_asset": "1500.00"})),
    )

    broker = make_broker(transport)
    account = broker.get_account()

    assert account.cash == Decimal("1234.56")
    assert account.equity == Decimal("1500.00")
    assert account.account_mode == "SIMULATED"


def test_account_equity_falls_back_to_cash_when_total_asset_missing():
    transport = FakeHttpTransport()
    transport.queue_get(ACCOUNTS_PATH, json_response(200, accounts_envelope(us_account_ids=["acc-1"])))
    transport.queue_get(
        CASH_INFO_PATH_TEMPLATE.format(acc_id="acc-1"),
        json_response(200, envelope({"balance": "300.00"})),
    )
    account = make_broker(transport).get_account()
    assert account.cash == Decimal("300.00")
    assert account.equity == Decimal("300.00")


# ---------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------


def test_positions_map_correctly():
    transport = FakeHttpTransport()
    transport.queue_get(ACCOUNTS_PATH, json_response(200, accounts_envelope(us_account_ids=["acc-1"])))
    transport.queue_get(
        POSITIONS_PATH_TEMPLATE.format(acc_id="acc-1"),
        json_response(
            200,
            envelope({"positions": [{"symbol": "AAPL", "qty": "10", "cost_price": "150.00", "mv": "1600.00", "profit": "100.00"}]}),
        ),
    )

    positions = make_broker(transport).get_positions()

    assert len(positions) == 1
    pos = positions[0]
    assert pos.ticker == "AAPL"
    assert pos.quantity == Decimal("10")
    assert pos.average_entry_price == Decimal("150.00")
    assert pos.market_value == Decimal("1600.00")
    assert pos.unrealized_pnl == Decimal("100.00")

    # A real call without `market` failed with a backend error (see
    # broker/MOOMOO_API_CONTRACT.md) -- lock in that it's always sent.
    positions_call = next(c for c in transport.calls if c[1] == POSITIONS_PATH_TEMPLATE.format(acc_id="acc-1"))
    assert positions_call[2] == {"market": US_MARKET_ID}


def test_get_position_filters_deterministically():
    transport = FakeHttpTransport()
    transport.queue_get(ACCOUNTS_PATH, json_response(200, accounts_envelope(us_account_ids=["acc-1"])))
    transport.queue_get(
        POSITIONS_PATH_TEMPLATE.format(acc_id="acc-1"),
        json_response(
            200,
            envelope(
                {
                    "positions": [
                        {"symbol": "AAPL", "qty": "10", "cost_price": "150", "mv": "1600", "profit": "100"},
                        {"symbol": "MSFT", "qty": "5", "cost_price": "300", "mv": "1550", "profit": "50"},
                    ]
                }
            ),
        ),
    )

    broker = make_broker(transport)
    assert broker.get_position(" msft ").ticker == "MSFT"
    assert broker.get_position("GOOG") is None


# ---------------------------------------------------------------------
# Quotes and timestamp preservation
# ---------------------------------------------------------------------


def test_quote_maps_correctly_and_sends_documented_request_shape():
    transport = FakeHttpTransport()
    transport.queue_post(
        QUOTE_PATH,
        json_response(200, envelope({"quote_list": [{"code": "US.AAPL", "last_price": 319.97, "data_time": 1788552000000}]})),
    )
    broker = make_broker(transport)
    assert broker.get_quote("aapl") == Decimal("319.97")

    # code_list must be an array of "{market}.{code}" strings, per docs.
    quote_call = next(c for c in transport.calls if c[1] == QUOTE_PATH)
    assert quote_call[2] == {"code_list": ["US.AAPL"]}


def test_daily_change_pct_computed_from_real_fields():
    transport = FakeHttpTransport()
    transport.queue_post(
        QUOTE_PATH,
        json_response(200, envelope({"quote_list": [{"code": "US.XLK", "last_price": 187.28, "prev_close_price": 185.97, "data_time": 1788552000000}]})),
    )
    broker = make_broker(transport)
    change_pct = broker.get_daily_change_pct("XLK")
    assert change_pct == (Decimal("187.28") - Decimal("185.97")) / Decimal("185.97") * 100


def test_daily_change_pct_missing_prev_close_fails_closed():
    transport = FakeHttpTransport()
    transport.queue_post(
        QUOTE_PATH,
        json_response(200, envelope({"quote_list": [{"code": "US.XLK", "last_price": 187.28, "data_time": 1788552000000}]})),
    )
    broker = make_broker(transport)
    with pytest.raises(MalformedResponseError):
        broker.get_daily_change_pct("XLK")


def test_quote_timestamp_is_preserved_from_server_not_local_now():
    transport = FakeHttpTransport()
    transport.queue_post(
        QUOTE_PATH,
        json_response(200, envelope({"quote_list": [{"code": "US.AAPL", "last_price": 319.97, "data_time": 1788552000000}]})),
    )
    broker = make_broker(transport)

    assert broker.get_quote_timestamp("AAPL") is None  # nothing fetched yet -- never fabricate "now"

    broker.get_quote("AAPL")
    ts = broker.get_quote_timestamp("AAPL")
    assert ts == datetime.fromtimestamp(1788552000000 / 1000.0, tz=timezone.utc)
    assert abs((datetime.now(timezone.utc) - ts).total_seconds()) > 1


def test_malformed_quote_timestamp_yields_none_not_a_crash_or_fabricated_now():
    transport = FakeHttpTransport()
    transport.queue_post(
        QUOTE_PATH,
        json_response(200, envelope({"quote_list": [{"code": "US.AAPL", "last_price": 319.97, "data_time": "not-a-timestamp"}]})),
    )
    broker = make_broker(transport)
    broker.get_quote("AAPL")
    assert broker.get_quote_timestamp("AAPL") is None


# ---------------------------------------------------------------------
# Market status
# ---------------------------------------------------------------------


def test_market_state_maps_correctly_and_sends_documented_request_shape():
    transport = FakeHttpTransport()
    transport.queue_post(
        MARKET_STATE_PATH,
        json_response(200, envelope({"market_state_list": [{"code": "US.SPY", "market_state": "AFTER_HOURS_END"}]})),
    )
    broker = make_broker(transport)
    assert broker.get_market_status() == "AFTER_HOURS_END"
    call = next(c for c in transport.calls if c[1] == MARKET_STATE_PATH)
    assert call[2] == {"code_list": ["US.SPY"]}


# ---------------------------------------------------------------------
# Orders (Today's Orders + History Orders merge)
# ---------------------------------------------------------------------


def _order_record(order_id, symbol="AAPL", side=1, status=4, qty="10", cum_qty="10", price="150.25", create_time="2026-09-05T09:35:00Z"):
    return {
        "order_id": order_id,
        "symbol": symbol,
        "side": side,
        "status": status,
        "qty": qty,
        "cum_qty": cum_qty,
        "price": price,
        "create_time": create_time,
    }


def test_simulated_orders_map_correctly():
    transport = FakeHttpTransport()
    transport.queue_get(ACCOUNTS_PATH, json_response(200, accounts_envelope(us_account_ids=["acc-1"])))
    transport.queue_get(OPEN_ORDERS_PATH_TEMPLATE.format(acc_id="acc-1"), json_response(200, envelope({"orders": []})))
    transport.queue_get(
        HISTORY_ORDERS_PATH_TEMPLATE.format(acc_id="acc-1"),
        json_response(200, envelope({"orders": [_order_record("o-1")], "pagination": {"has_more": False, "next_key": ""}})),
    )
    orders = make_broker(transport).get_orders()
    assert len(orders) == 1
    order = orders[0]
    assert order.order_id == "o-1"
    assert order.ticker == "AAPL"
    assert order.action == "BUY"
    assert order.status == "FILLED"
    assert order.fill_price == Decimal("150.25")
    assert order.filled_quantity == Decimal("10")

    # A real call without `market` failed with ret_code=-3 "missing
    # required parameter: market" (see broker/MOOMOO_API_CONTRACT.md).
    open_call = next(c for c in transport.calls if c[1] == OPEN_ORDERS_PATH_TEMPLATE.format(acc_id="acc-1"))
    history_call = next(c for c in transport.calls if c[1] == HISTORY_ORDERS_PATH_TEMPLATE.format(acc_id="acc-1"))
    assert open_call[2] == {"market": US_MARKET_ID}
    assert history_call[2] == {"market": US_MARKET_ID}


def test_orders_merge_today_and_history_history_wins_on_duplicate():
    transport = FakeHttpTransport()
    transport.queue_get(ACCOUNTS_PATH, json_response(200, accounts_envelope(us_account_ids=["acc-1"])))
    transport.queue_get(
        OPEN_ORDERS_PATH_TEMPLATE.format(acc_id="acc-1"),
        json_response(200, envelope({"orders": [_order_record("o-1", status=2, cum_qty="0")]})),  # still Submitted
    )
    transport.queue_get(
        HISTORY_ORDERS_PATH_TEMPLATE.format(acc_id="acc-1"),
        json_response(200, envelope({"orders": [_order_record("o-1", status=4, cum_qty="10")], "pagination": {"has_more": False, "next_key": ""}})),
    )
    orders = make_broker(transport).get_orders()
    assert len(orders) == 1
    assert orders[0].status == "FILLED"  # history's final state wins
    assert orders[0].filled_quantity == Decimal("10")


def test_unrecognized_order_status_fails_closed():
    transport = FakeHttpTransport()
    transport.queue_get(ACCOUNTS_PATH, json_response(200, accounts_envelope(us_account_ids=["acc-1"])))
    transport.queue_get(
        OPEN_ORDERS_PATH_TEMPLATE.format(acc_id="acc-1"),
        json_response(200, envelope({"orders": [{"order_id": "o-1", "status": 999}]})),
    )
    with pytest.raises(MalformedResponseError):
        make_broker(transport).get_orders()


def test_unrecognized_order_side_fails_closed():
    transport = FakeHttpTransport()
    transport.queue_get(ACCOUNTS_PATH, json_response(200, accounts_envelope(us_account_ids=["acc-1"])))
    transport.queue_get(
        OPEN_ORDERS_PATH_TEMPLATE.format(acc_id="acc-1"),
        json_response(200, envelope({"orders": [_order_record("o-1", side=99)]})),
    )
    with pytest.raises(MalformedResponseError):
        make_broker(transport).get_orders()


# ---------------------------------------------------------------------
# Envelope / malformed-response contract tests
# ---------------------------------------------------------------------


def test_missing_ret_code_fails_closed():
    transport = FakeHttpTransport()
    transport.queue_get(ACCOUNTS_PATH, json_response(200, {"data": {"accounts": []}}))  # no ret_code
    with pytest.raises(MalformedResponseError):
        make_broker(transport).get_account()


def test_nonzero_ret_code_is_an_application_error_even_under_http_200():
    transport = FakeHttpTransport()
    transport.queue_get(ACCOUNTS_PATH, json_response(200, {"ret_code": 1, "ret_msg": "rate limited", "data": {}}))
    with pytest.raises(MoomooApiError):
        make_broker(transport).get_account()


def test_missing_data_object_fails_closed():
    transport = FakeHttpTransport()
    transport.queue_get(ACCOUNTS_PATH, json_response(200, {"ret_code": 0, "ret_msg": "ok"}))  # no data
    with pytest.raises(MalformedResponseError):
        make_broker(transport).get_account()


def test_malformed_positions_response_fails_closed():
    transport = FakeHttpTransport()
    transport.queue_get(ACCOUNTS_PATH, json_response(200, accounts_envelope(us_account_ids=["acc-1"])))
    transport.queue_get(POSITIONS_PATH_TEMPLATE.format(acc_id="acc-1"), json_response(200, envelope({"not_positions": []})))
    with pytest.raises(MalformedResponseError):
        make_broker(transport).get_positions()


def test_account_entry_missing_market_id_fails_closed():
    transport = FakeHttpTransport()
    transport.queue_get(ACCOUNTS_PATH, json_response(200, envelope({"accounts": [{"account_id": "acc-1"}]})))
    with pytest.raises(MalformedResponseError):
        make_broker(transport).get_account()


def test_non_json_body_fails_closed():
    transport = FakeHttpTransport()
    transport.queue_get(ACCOUNTS_PATH, HttpResponse(status_code=200, body="not json at all"))
    with pytest.raises(MalformedResponseError):
        make_broker(transport).get_account()


# ---------------------------------------------------------------------
# Unauthorized / rate limit / server error / timeout
# ---------------------------------------------------------------------


def test_unauthorized_response_fails_closed():
    transport = FakeHttpTransport()
    transport.queue_get(ACCOUNTS_PATH, json_response(401, {"error": "invalid_token"}))
    with pytest.raises(MoomooAuthenticationError):
        make_broker(transport).get_account()


def test_forbidden_response_fails_closed():
    transport = FakeHttpTransport()
    transport.queue_get(ACCOUNTS_PATH, json_response(403, {"error": "forbidden"}))
    with pytest.raises(MoomooAuthenticationError):
        make_broker(transport).get_account()


# ---------------------------------------------------------------------
# OAuth token refresh (endpoints verified against getting-started doc)
# ---------------------------------------------------------------------


def _oauth_client(transport, storage):
    config = OAuthConfig(redirect_uri="http://localhost:8765/callback", scope="quote_read trade_read")
    assert config.base_host == "https://webapi.moomoo.com"
    assert config.authorize_endpoint == DEFAULT_AUTHORIZE_PATH == "/oauth2/authorize/confirm"
    assert config.token_endpoint == DEFAULT_TOKEN_PATH == "/oauth2/token"
    assert config.registration_endpoint == DEFAULT_REGISTER_PATH == "/oauth2/register"
    return MoomooOAuthClient(transport, config, TokenStorage(storage))


def test_authorization_url_is_a_fully_qualified_url_not_a_bare_path():
    # Regression: build_authorization_url() once returned only the path
    # (e.g. "/oauth2/authorize/confirm?...") with no scheme/host, which
    # is not a URL a browser can open at all. This is caught only by
    # actually checking the produced string, not by any mocked HTTP
    # call -- browsers never go through HttpTransport.
    transport = FakeHttpTransport()
    client = _oauth_client(transport, InMemorySecretStore())
    url, state, verifier = client.build_authorization_url(client_id="client-abc")
    assert url.startswith("https://webapi.moomoo.com/oauth2/authorize/confirm?")
    assert f"client_id=client-abc" in url
    assert f"state={state}" in url


def test_token_refresh_path_works_with_fake_tokens():
    transport = FakeHttpTransport()
    storage = InMemorySecretStore()
    client = _oauth_client(transport, storage)

    expired = TokenSet(
        access_token="old-access-token",
        refresh_token="old-refresh-token",
        token_type="Bearer",
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    TokenStorage(storage).save(expired)

    transport.queue_post(
        DEFAULT_TOKEN_PATH,
        json_response(200, {"access_token": "new-access-token", "refresh_token": "new-refresh-token", "expires_in": 3600, "token_type": "Bearer"}),
    )

    token = client.get_valid_access_token(client_id="client-abc")
    assert token == "new-access-token"
    assert TokenStorage(storage).load().access_token == "new-access-token"


def test_refresh_failure_fails_closed():
    transport = FakeHttpTransport()
    storage = InMemorySecretStore()
    client = _oauth_client(transport, storage)

    expired = TokenSet(
        access_token="old-access-token",
        refresh_token="old-refresh-token",
        token_type="Bearer",
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    TokenStorage(storage).save(expired)

    transport.queue_post(DEFAULT_TOKEN_PATH, json_response(400, {"error": "invalid_grant"}))

    with pytest.raises(AuthenticationError):
        client.get_valid_access_token(client_id="client-abc")


# ---------------------------------------------------------------------
# Tokens never appear in exception text (or repr/str)
# ---------------------------------------------------------------------


def test_access_token_never_appears_in_broker_exception_text():
    transport = FakeHttpTransport()
    transport.queue_get(ACCOUNTS_PATH, json_response(500, {"error": "boom"}))

    broker = make_broker(transport, token=lambda: SECRET_MARKER)
    with pytest.raises(Exception) as excinfo:
        broker.get_account()
    assert SECRET_MARKER not in str(excinfo.value)
    assert SECRET_MARKER not in repr(excinfo.value)


def test_tokens_never_appear_in_oauth_exception_or_repr():
    transport = FakeHttpTransport()
    storage = InMemorySecretStore()
    client = _oauth_client(transport, storage)

    token_set = TokenSet(
        access_token=SECRET_MARKER,
        refresh_token=SECRET_MARKER + "-refresh",
        token_type="Bearer",
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    TokenStorage(storage).save(token_set)
    assert SECRET_MARKER not in repr(token_set)
    assert SECRET_MARKER not in str(token_set)

    transport.queue_post(DEFAULT_TOKEN_PATH, json_response(400, {"error": "invalid_grant"}))
    with pytest.raises(AuthenticationError) as excinfo:
        client.get_valid_access_token(client_id="client-abc")
    assert SECRET_MARKER not in str(excinfo.value)


# ---------------------------------------------------------------------
# Execution permanently disabled
# ---------------------------------------------------------------------


def test_submit_execution_intent_always_rejects():
    transport = FakeHttpTransport()
    broker = make_broker(transport)
    for bogus_intent in (None, "APPROVED", {"action": "BUY"}, object()):
        with pytest.raises(ReadOnlyBrokerError):
            broker.submit_execution_intent(bogus_intent)
    assert transport.calls == []


def test_cancel_order_always_rejects():
    transport = FakeHttpTransport()
    broker = make_broker(transport)
    with pytest.raises(ReadOnlyBrokerError):
        broker.cancel_order("any-order-id")
    assert transport.calls == []


def test_close_position_always_rejects():
    transport = FakeHttpTransport()
    broker = make_broker(transport)
    with pytest.raises(ReadOnlyBrokerError):
        broker.close_position("AAPL")
    assert transport.calls == []


def test_read_only_broker_error_is_a_permission_error():
    transport = FakeHttpTransport()
    broker = make_broker(transport)
    with pytest.raises(PermissionError):
        broker.submit_execution_intent(None)


# ---------------------------------------------------------------------
# No test makes a network call
# ---------------------------------------------------------------------


def test_no_real_network_call_possible(monkeypatch):
    def guard(*args, **kwargs):
        raise AssertionError("Attempted to open a real network socket during tests.")

    monkeypatch.setattr(socket.socket, "__init__", guard)

    transport = FakeHttpTransport()
    transport.queue_get(ACCOUNTS_PATH, json_response(200, accounts_envelope(us_account_ids=["acc-1"])))
    transport.queue_get(CASH_INFO_PATH_TEMPLATE.format(acc_id="acc-1"), json_response(200, envelope({"balance": "1", "total_asset": "1"})))
    make_broker(transport).get_account()  # succeeds using only the fake transport

    with pytest.raises(AssertionError):
        UrllibHttpTransport("https://webapi.moomoo.com").get(ACCOUNTS_PATH)


# ---------------------------------------------------------------------
# PKCE / callback-state mechanics
# ---------------------------------------------------------------------


def test_pkce_challenge_is_derived_from_verifier():
    import base64
    import hashlib

    pair = generate_pkce_pair()
    expected = base64.urlsafe_b64encode(hashlib.sha256(pair.verifier.encode()).digest()).rstrip(b"=").decode()
    assert pair.challenge == expected
    assert pair.method == "S256"
    assert SECRET_MARKER not in repr(pair)


def test_callback_state_mismatch_is_rejected():
    transport = FakeHttpTransport()
    client = _oauth_client(transport, InMemorySecretStore())
    with pytest.raises(CallbackStateError):
        client.verify_callback_state(generate_state(), "attacker-supplied-state")
