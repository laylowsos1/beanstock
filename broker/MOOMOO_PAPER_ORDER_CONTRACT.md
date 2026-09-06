# Moomoo simulated (paper) order REST contract

Source of truth: https://open.moomoo.com/api/... (same portal as
broker/MOOMOO_API_CONTRACT.md). Every path/field below was fetched from
that site during this verification pass. This file documents the
**write** endpoints for `broker/moomoo_paper.py`; it does not itself
grant write access -- see "Write permission gate" at the end.

As with the read-only contract, prefer a real authenticated call over a
docs-summary reading wherever the two disagree (broker/MOOMOO_API_CONTRACT.md
records two real cases of the docs missing or misstating a required
field). No write endpoint has been called for real yet, so none of the
docs readings below have that same real-call cross-check -- treat field
names here as doc-sourced-only until a real write call confirms them,
the same caution the read contract now attaches to anything not yet
independently verified.

## 1. Place Order (simulated)

- Doc: https://open.moomoo.com/api/sim-trade/input-order
- Method / path: `POST /api/v1.0/sim-trade/{acc_id}/orders` (`acc_id` path segment)
- Body — required: `market` (int, "from `market_id` in account list"), `symbol` (string), `order_type` (int), `order_side` (int), `qty` (string)
- Body — optional: `price` (string, "required for limit orders"), `text` (string, remark, ≤100 bytes)
- `order_type` enum: `1`=Limit, `3`=Market
- `order_side` enum: `1`=Buy, `2`=Sell, `3`=Short Sell, `4`=Buy Back
- Response envelope: `{"ret_code": 0, "data": {"order_id": "string"}}` — **no fill/status information at all.** Status must be learned by a follow-up read (Today's Orders / History Orders, already implemented in `broker/moomoo_readonly.py`).
- Constraints documented: "Limit orders require a price; orders priced above market price are filled immediately." "HK stock quantities must be multiples of the board lot size."
- **Fractional shares: NOT confirmed supported.** Nothing in the fetched page states fractional/partial-share quantities are accepted for US stock; the only quantity granularity rule given is HK's board-lot-multiple requirement. Per the explicit instruction for this build, this means: **fail closed, do not guess.** `MoomooPaperBroker` only accepts `instrument_type == "stock"` and always computes a **whole-share** quantity (`floor(dollar_amount / quote)`), rejecting `"fractional_share"` outright and rejecting a BUY/ADD whose dollar_amount rounds to zero whole shares, rather than ever sending a fractional `qty`.
- Auth: not restated on this specific page, but Bearer-token auth is confirmed project-wide (broker/MOOMOO_API_CONTRACT.md's OAuth section) and applies uniformly to every other sim-trade endpoint already verified against a real call — treated as required here too, not a per-endpoint guess.
- This adapter always uses `order_type=3` (Market) and never sends `price` — see `broker/moomoo_paper.py` docstring for why (a deliberate Beanstock design choice, not a documented moomoo constraint).
- This is the SAME path as the already-implemented `OPEN_ORDERS_PATH_TEMPLATE` (GET lists orders, POST places one) — reused from `broker/moomoo_readonly.py` rather than redefined.

## 2. Modify Order (simulated)

- Doc: https://open.moomoo.com/api/sim-trade/modify-order
- Method / path: `POST /api/v1.0/sim-trade/{acc_id}/orders/{order_id}/modify`
- Body — optional: `new_qty` (string, "must be a multiple of board lot"), `new_price` (string)
- Constraint: "Only orders with status=2 (Submitted) can be modified."
- Response envelope: `{"ret_code": int, "ret_msg": string, "data": {"order_id": "string"}}`
- Auth: not restated on this page; same project-wide Bearer requirement applies.
- **Not wired into `MoomooPaperBroker` in this build.** Nothing in Beanstock's execution pipeline (`ExecutionIntent` -> `BrokerGateway`) ever produces a "modify an existing order" action — only BUY/ADD/REDUCE/EXIT, which map to new orders, not modifications of resting ones. Recorded here for completeness per the verification request; add it only if a real use case appears.

## 3. Cancel Order (simulated)

- Doc: https://open.moomoo.com/api/sim-trade/cancel-order
- Method / path: `POST /api/v1.0/sim-trade/{acc_id}/orders/{order_id}/cancel`
- Body: empty JSON object (`{}`)
- Constraint: "Only orders with status=2 (Submitted) can be cancelled."
- Response envelope: `{"ret_code": 0, "ret_msg": "success", "data": {"order_id": "string"}}`
- Auth: not restated on this page; same project-wide Bearer requirement applies.
- Wired into `MoomooPaperBroker.cancel_order()`, gated by the same write-permission gate as order placement.

## 4. Max Buy/Sell Quantity (simulated)

- Doc: https://open.moomoo.com/api/sim-trade/max-buy-sell
- Method / path: `GET /api/v1.0/sim-trade/{acc_id}/max-buy-sell`
- Query — required: `symbol` (string), `order_type` (int, same enum as #1)
- Query — optional: `price` (string, "required for limit orders"), `order_id` (string, "for modification")
- **`market` query parameter, corrected against a real call:** not
  listed on this endpoint's own doc page at all, but a real call
  without it returned `ret_code=-3`, `ret_msg="missing required
  parameter: market"` — the identical failure mode already seen on the
  Today's Orders endpoint (#1). The adapter now always sends
  `market=<US_MARKET_ID>` here too. Confirmed during the first real
  readiness audit (this session): F/SOFI/AAL all returned this error
  before the fix, and returned valid `max_cash_buy_qty_round_lot`
  values after it.
- Response fields used: `max_cash_buy_qty_round_lot` (string)
- Response fields not used: `max_margin_buy_qty_round_lot`, `max_sell_qty_round_lot`, `max_sell_short_qty`, `max_buy_back_qty`, `required_im_long`, `required_im_short` (futures margin, not applicable — Beanstock trades no margin/futures)
- Auth: not restated on this page; same project-wide Bearer requirement applies.
- This is a **read**, not a write — used as an extra, broker-authoritative pre-flight check before constructing a BUY/ADD order (on top of Beanstock's own cash-based sizing), and therefore runs regardless of the write-permission gate below, the same way every other read in this project does.

## What remains unverified

- Whether `price` must be entirely absent (vs. an explicit null) for a Market order — the adapter simply never includes the key.
- The exact wording/values of any error `ret_code` this endpoint returns for insufficient funds, invalid quantity, or a stale/halted quote — none of these were shown in the fetched examples. `MoomooPaperBroker` treats **any** non-zero `ret_code` as a generic `MoomooApiError` (fail closed) rather than pattern-matching specific codes it hasn't actually seen.
- Whether the moomoo simulator fills a Market order synchronously (so a follow-up order-list read immediately shows `FILLED`) or asynchronously. The adapter's follow-up read handles either: it reports whatever status the order-list actually shows (`PENDING`/`FILLED`/etc.), it does not assume immediate fill.

## Write permission gate

None of the endpoints above are ever called by production code before `BEANSTOCK_PAPER_WRITE_ENABLED` is explicitly set (default: `False`, see `broker/moomoo_paper.py`). Independently of that gate, every request from this adapter still passes through the same path-prefix allowlist as the read-only adapter (`/api/v1.0/sim-trade/`, `/api/v1.0/quote/`) — a live-account write endpoint (e.g. anything under `/api/v1.0/trading/`) is architecturally unreachable regardless of the gate's state.
