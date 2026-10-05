"""Canonical data kinds (DESIGN.md §11.1)."""

from __future__ import annotations

from enum import StrEnum


class DataKind(StrEnum):
    TRADE = "trade"
    QUOTE_L1 = "quote_l1"
    BOOK_L2 = "book_l2"
    BAR = "bar"
    AMM_EVENT = "amm_event"
    AMM_STATE = "amm_state"
    FUNDING = "funding"
    OPEN_INTEREST = "open_interest"
    ONCHAIN_TRANSFER = "onchain_transfer"
    REFERENCE = "reference"
