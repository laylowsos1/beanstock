# Moomoo OpenAPI contract notes (official-docs verification pass)

Source of truth: the official REST developer portal at
https://open.moomoo.com/api/... (host serving the actual API:
`https://webapi.moomoo.com`). Every path/field below was fetched from
that site on this verification pass. Where a field could not be
confirmed there, it is marked **UNVERIFIED** and the adapter fails
closed rather than guessing.

Note: `https://openapi.moomoo.com/moomoo-api-doc/...` is a **different**
product (the older OpenD gateway + protobuf SDK, e.g. `get_acc_list()`
with a `trd_env` field). It is NOT the REST + OAuth2.1/PKCE API this
adapter targets, and none of its field names are used here even where
they look similar to what the moomoo MCP tool returned during the
earlier connection audit.

## Architectural safety boundary (this is the real "never live" guarantee)

The verified REST API has **no unified account-list endpoint with a
live/simulated discriminator field**. Simulated accounts live entirely
under the `/api/v1.0/sim-trade/*` path family (auto-created on first
call, no `trd_env` field at all); live business accounts live under
`/api/v1.0/accounts/*` and `/api/v1.0/trading/*` (confirmed via
`/api/v1.0/accounts/authorized_trd_accs`, which also has no
live/simulated field — because everything under that prefix already
*is* live).

So "never select a live account" is enforced by `MoomooReadOnlyBroker`
as a **path-prefix allowlist**, not a field check: every request is
asserted to start with `/api/v1.0/sim-trade/` or `/api/v1.0/quote/`
before it is sent. There is no code path capable of constructing a
request to `/api/v1.0/accounts/*` or `/api/v1.0/trading/*` at all.

## 1. Simulated Trading — Account List

- Doc: https://open.moomoo.com/api/sim-trade/account-list
- Method / path: `GET /api/v1.0/sim-trade/accounts`
- Params: none (user identity comes from the session/auth header)
- Envelope: `{"ret_code": int, "ret_msg": str, "data": {"accounts": [...]}}`
- Account fields used: `account_id` (str), `market_id` (int)
- Account fields not used: `broker_id`, `intra_account_id`, `account_type`, `account_title`
- `market_id` enum: **corrected against a real live call, superseding the
  earlier docs-summary reading.** The account-list doc page, as fetched
  and summarized by an AI-assisted tool during the first verification
  pass, was read as `1`=HK, `2`=US, `3`=US_OPTION, `9`=HKCC, `18`=CA.
  That reading was wrong for `market_id`. A real authenticated call to
  this exact endpoint (first live smoke test, this session) returned:

  ```json
  {"ret_code":0,"ret_msg":"success","data":{"accounts":[
    {"account_id":"9000001","account_title":"美股融资融券模拟账户","account_type":1,"broker_id":0,"intra_account_id":0,"market_id":100},
    {"account_id":"9000002","account_title":"港股模拟账户","account_type":1,"broker_id":0,"intra_account_id":0,"market_id":1},
    {"account_id":"9000003","account_title":"美国期货模拟账户","account_type":1,"broker_id":0,"intra_account_id":0,"market_id":11}
  ]}}
  ```

  `account_title` "美股融资融券模拟账户" ("US margin/securities simulated
  account") carries `market_id: 100`, not `2` — i.e. this REST endpoint
  actually uses the same numbering the older OpenD SDK used (`100`=US),
  which the first verification pass explicitly (and incorrectly) said
  was different. `US_MARKET_ID` in broker/moomoo_readonly.py is `100`.
  These are also the exact same account_id values (`9000001`/`9000002`/`9000003`)
  the original moomoo MCP audit returned, confirming this is the same
  underlying simulated account set. Lesson: prefer a real authenticated
  call over a summarized docs fetch wherever the two disagree, and treat
  a docs-summary-only reading of an enum as unconfirmed until checked
  against one.
- Auth: Bearer token required.
- Timestamps: none in this response.

## 2. Simulated Trading — Account Cash Info

- Doc: https://open.moomoo.com/api/sim-trade/cash-info
- Method / path: `GET /api/v1.0/sim-trade/{acc_id}/cash-info` (`acc_id` is a **path** segment, not a query param)
- Envelope: `{"ret_code": int, "data": {...}}`
- Fields used: `balance` (cash), `total_asset` (equity, falls back to `balance` if absent)
- Fields not used: `hold`, `max_power_long`, `mv`, `long_mv`, `short_mv`, `unrealized_profit`, `realized_profit`
- Auth: Bearer token required.

## 3. Simulated Trading — Position List

- Doc: https://open.moomoo.com/api/sim-trade/position-list
- Method / path: `GET /api/v1.0/sim-trade/{acc_id}/positions`
- Envelope: `{"ret_code": int, "data": {"positions": [...]}}`
- Position fields used: `symbol` (ticker), `qty`, `cost_price`, `mv` (market value), `profit` (unrealized P&L)
- Field not used: `profit_ratio`
- **Correction from the placeholder build:** field names are `symbol`/`mv`/`profit`, NOT `code`/`market_val`/`pl_val`.
- **`market` query parameter, corrected against a real call:** the docs
  page (re-fetched) documents `market` (int, query) as *optional* ("Market
  filter"). A real call with only `acc_id` returned `ret_code=-5`
  ("backend business error") on this account, which cleared once `market`
  (this account's own `market_id`, `100`) was included. Docs and live
  behavior disagree here; the adapter always sends `market` now.
- Auth: Bearer token required.

## 4. Simulated Trading — Today's Orders (open orders)

- Doc: https://open.moomoo.com/api/sim-trade/order-list
- Method / path: `GET /api/v1.0/sim-trade/{acc_id}/orders`
- Envelope: `{"ret_code": int, "data": {"orders": [...]}}`
- Order fields used: `order_id`, `symbol`, `side` (int), `status` (int), `qty`, `cum_qty` (filled qty), `price`, `create_time`
- `side` enum: `1`=Buy, `2`=Sell, `3`=Short Sell, `4`=Buy Back
- `status` enum: `2`=Submitted, `3`=Partially Filled, `4`=Filled, `5`=Cancelled, `6`=Rejected
- Auth: Bearer token required.
- **Partially verified:** the fetched page did not disambiguate whether
  `price` is the submitted limit price or the average fill price. The
  adapter maps it to `Order.fill_price` as the best-documented signal
  available; treat this mapping as provisional until confirmed.
- **`market` query parameter, corrected against a real call:** the
  fetched doc page for this endpoint did not list a `market` parameter
  at all. A real call with only `acc_id` returned `ret_code=-3`,
  `ret_msg="missing required parameter: market"`. The adapter now
  always sends `market=<this account's own market_id>` (`100` for US).
  This is the clearest evidence in this whole pass that a docs-summary
  fetch can simply omit a parameter the live API enforces.

## 5. Simulated Trading — History Orders

- Doc: https://open.moomoo.com/api/sim-trade/history-order-list
- Method / path: `GET /api/v1.0/sim-trade/{acc_id}/history-orders`
- Envelope: `{"ret_code": int, "data": {"orders": [...], "pagination": {"has_more": bool, "next_key": str}}}`
- Order fields: "same as Today's Orders" per docs (no separate enumeration given) — same mapping as #4 applied.
- Pagination is not currently consumed (`next_key` is read but not followed); flagged as a known gap, not a guess.
- Auth: Bearer token required.
- `market` is sent here too (same rationale as #4), though this
  endpoint's fetched doc page didn't mention `market` either and this
  specific endpoint has not yet been confirmed to require it (the live
  smoke test's error came from #4, not #5) — sent proactively pending
  the next live run's confirmation, since #4's live behavior already
  disproved that "the docs page doesn't mention it" implies "the live
  API doesn't need it."

`Broker.get_orders()` merges #4 and #5 (history entries win on a
duplicate `order_id`, since history reflects final state).

## 6. Quote — Stock Quote

- Doc: https://open.moomoo.com/api/quote/realtime/stock-quote
- Method / path: `POST /api/v1.0/quote/stock-quote`
- Body: `{"code_list": ["<MARKET>.<SYMBOL>", ...]}` (array, not a bare string)
- Envelope: `{"ret_code": int, "ret_msg": str, "data": {"quote_list": [...]}}`
- Fields used: `last_price` (double), `data_time` (int64, **milliseconds**, market-local timezone)
- Auth: Bearer token required.
- This is the one endpoint whose field names were already correct in
  the placeholder build (`last_price`/`data_time`) — confirmed here
  against official docs rather than only against the earlier MCP output.

## 7. Quote — Market State

- Doc: https://open.moomoo.com/api/quote/basic-data/market-state
- Method / path: `POST /api/v1.0/quote/market-state`
- Body: `{"code_list": ["<MARKET>.<SYMBOL>", ...]}`
- Envelope: `{"ret_code": int, "ret_msg": str, "data": {"market_state_list": [...]}}`
- Field used: `market_state` (string enum)
- Fields not used: `code`, `stock_name`, `sc_name`, `tc_name`, `time_date`, `traded_seconds`, `total_seconds`, `trade_section`
- `market_state` enum (from https://open.moomoo.com/api/quote/naming-dictionary, 38 values):
  `NONE, AUCTION, WAITING_OPEN, MORNING, REST, AFTERNOON, CLOSED, MAAUCTION,
  PRE_MARKET_BEGIN, PRE_MARKET_END, AFTER_HOURS_BEGIN, AFTER_HOURS_END,
  FUTU_SWITCH_DATE, NIGHT_OPEN, NIGHT_END, FUTURE_DAY_OPEN, FUTURE_DAY_BREAK,
  FUTURE_DAY_CLOSE, FUTURE_DAY_WAIT_OPEN, HK_CAS, FUTURE_NIGHT_WAIT,
  FUTURE_AFTERNOON, FUTURE_SWITCH_DATE, FUTURE_OPEN, FUTURE_BREAK,
  FUTURE_BREAK_OVER, FUTURE_CLOSE, STIB_AFTER_HOURS_WAIT,
  STIB_AFTER_HOURS_BEGIN, STIB_AFTER_HOURS_END, CLOSE_AUCTION,
  AFTERNOON_END, NIGHT, OVERNIGHT_BEGIN, OVERNIGHT_END, TRADE_AT_LAST,
  TRADE_AUCTION, OVERNIGHT`
- Auth: Bearer token required.

## OAuth 2.1 + PKCE

- Doc: https://open.moomoo.com/api/overview/getting-started
- Host: `https://webapi.moomoo.com`

| Step | Method / Path | Body / Query | Response fields |
|---|---|---|---|
| Register client | `POST /oauth2/register` | `redirect_uris`, `token_endpoint_auth_method`, `grant_types`, `response_types`, `client_name` | `client_id`, `client_id_issued_at`, `client_name`, `redirect_uris`, `grant_types`, `token_endpoint_auth_method`, `response_types`, `registration_access_token`, `registration_client_uri`, `scope`, `pkce_required` |
| Authorize | `GET /oauth2/authorize/confirm` | query: `client_id`, `code_challenge`, `code_challenge_method=S256`, `redirect_uri`, `response_type=code`, `state` | (redirect with `code`, `state`) |
| Exchange code | `POST /oauth2/token` | `grant_type=authorization_code`, `code`, `client_id`, `redirect_uri`, `code_verifier` | `access_token`, `token_type=Bearer`, `expires_in`, `refresh_token`, `scope` |
| Refresh | `POST /oauth2/token` | `grant_type=refresh_token`, `refresh_token`, `client_id` | same as exchange (`refresh_token` optional in the response) |

- **No `client_secret` appears anywhere in the documented registration
  response** — `token_endpoint_auth_method: "none"` is a public,
  PKCE-only client. `client_secret` is kept as an optional parameter in
  this codebase only for forward-compatibility; the verified flow never
  produces or requires one.
- Bearer usage: `Authorization: Bearer <access_token>` header on every API call.
- **Scope values (verified on a follow-up pass):** the documented scope
  vocabulary is `quote:read`, `quote:write`, `trade:read`, `trade:write`,
  `accid:*`. The registration-response example shows
  `"scope": "quote:read quote:write trade:read trade:write accid:*"`;
  the token-response example shows a narrowed instantiation,
  `"scope": "quote:read trade:read accid:123456"` — i.e. `accid:*`
  requested at registration/authorization resolves to a specific
  `accid:{account_id}` once a token is actually issued.

  **`accid` is deliberately NOT requested.** That token-response example
  is the *only* accid evidence this project has found — it shows the
  scope vocabulary exists, not that any specific endpoint (sim-trade or
  otherwise) requires it. No fetched page states that
  `/api/v1.0/sim-trade/*` needs an account-scoped grant. Requesting
  `accid:*` would also mean the consent screen asks the user to
  authorize *all* of their accounts — including real/live ones — which
  is exactly what this read-only, simulated-only adapter must not do.
  Beanstock's read-only adapter therefore requests exactly
  `"quote:read trade:read"` (`auth.moomoo_oauth.READ_ONLY_SCOPE`) — no
  `quote:write`, `trade:write`, or `accid`. If a real sim-trade call
  ever fails with a scope/authorization error, that error should be
  read carefully and a specific citation obtained before requesting
  anything broader — never widen this scope speculatively.
  `OAuthConfig.scope` remains a required constructor argument (no
  class-wide default) since it's a general-purpose client, not specific
  to this read-only scope choice.

## Disallowed (live-account) endpoints — never called by this adapter

- `GET /api/v1.0/accounts/authorized_trd_accs` (https://open.moomoo.com/api/trading/account/get-accounts)
- `GET /api/v1.0/accounts/{acc_id}/funds` (https://open.moomoo.com/api/trading/account/get-funds)
- Everything else under `/api/v1.0/accounts/*` and `/api/v1.0/trading/*`

These are documented, real, live-money endpoints. `MoomooReadOnlyBroker`
enforces a path-prefix allowlist (`/api/v1.0/sim-trade/`,
`/api/v1.0/quote/`) so none of the above can ever be reached from this
codebase, regardless of configuration.
