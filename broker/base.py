"""Beanstock broker abstraction.

Defines the contract any broker adapter -- fake or real -- must
implement, so the research/risk-engine/execution layers never depend on
which concrete broker is wired in. broker.fake_paper.FakePaperBroker
implements this interface today; a future MoomooPaperBroker (paper) or
MoomooBroker (live) would implement the same interface without requiring
any change above this layer.

This module defines no concrete broker, makes no network calls, and
contains no live-trading path.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional


@dataclass(frozen=True)
class Account:
    """Snapshot of broker account state. equity is always
    cash + sum(market value of all open positions), recomputed live from
    the current fake quotes -- never stored/cached separately from cash.
    """

    cash: Decimal
    equity: Decimal
    account_mode: str


@dataclass(frozen=True)
class Position:
    """An open position. average_entry_price is the cost basis (weighted
    average across BUY/ADD fills); market_value/unrealized_pnl are
    derived live from the current quote at the time this was fetched.
    """

    ticker: str
    quantity: Decimal
    average_entry_price: Decimal
    market_value: Decimal
    unrealized_pnl: Decimal


@dataclass(frozen=True)
class Order:
    """A single submit_execution_intent() attempt and its outcome.
    Immutable -- a new Order record is created for every attempt, filled
    or rejected, so the order history doubles as the audit trail.
    """

    order_id: str
    ticker: Optional[str]
    action: Optional[str]
    status: str  # PENDING | FILLED | REJECTED | CANCELED
    requested_quantity: Optional[Decimal]
    requested_dollar_amount: Optional[Decimal]
    fill_price: Optional[Decimal]
    filled_quantity: Optional[Decimal]
    realized_pnl: Optional[Decimal]
    audit_reference: Optional[str]
    rejection_reason: Optional[str]
    created_at: str


class Broker(ABC):
    """Abstract broker interface. No method here makes a network call or
    talks to a real brokerage -- that is a property of the concrete
    subclass, not of this interface.
    """

    @abstractmethod
    def get_account(self) -> Account:
        ...

    @abstractmethod
    def get_positions(self) -> list:
        ...

    @abstractmethod
    def get_position(self, ticker: str) -> Optional[Position]:
        ...

    @abstractmethod
    def get_orders(self) -> list:
        ...

    @abstractmethod
    def get_order(self, order_id: str) -> Optional[Order]:
        ...

    @abstractmethod
    def get_quote(self, ticker: str) -> Optional[Decimal]:
        ...

    @abstractmethod
    def get_market_status(self) -> str:
        ...

    @abstractmethod
    def submit_execution_intent(self, intent) -> Order:
        ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> Order:
        ...

    @abstractmethod
    def close_position(self, ticker: str) -> Order:
        ...
