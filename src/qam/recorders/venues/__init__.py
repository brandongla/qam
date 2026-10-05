from __future__ import annotations

from qam.recorders.venues.base import Classified, Market, Subscription, VenueAdapter
from qam.recorders.venues.hyperliquid import HyperliquidAdapter
from qam.recorders.venues.lighter import LighterAdapter

ADAPTERS: dict[str, type[VenueAdapter]] = {
    HyperliquidAdapter.name: HyperliquidAdapter,
    LighterAdapter.name: LighterAdapter,
}

__all__ = [
    "ADAPTERS",
    "Classified",
    "HyperliquidAdapter",
    "LighterAdapter",
    "Market",
    "Subscription",
    "VenueAdapter",
]
