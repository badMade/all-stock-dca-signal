"""inventory_signals: research-only, shrink-only inventory-reservation suggester.

Spec: docs/inventory_reservation_suggester.md. Pure math over caller-supplied inputs (no
network, no broker, no yfinance, no repo config), one SizingSuggester wrapper, and a
publisher that writes only this package's own file. Shadow mode: not wired into any live
DCA path, and never imports or is imported by another suggester.
"""

from inventory_signals.publish import (
    FILENAME,
    InventoryReservationPublisher,
    inventory_publisher,
    publish_outcome,
)
from inventory_signals.reservation import (
    SLOTS,
    inventory_excess,
    max_buy_price,
    normalized_profile,
    remaining_weight,
    reservation_discount,
    size_multiplier,
)
from inventory_signals.suggester import (
    SOURCE,
    UNSCORED_MAE,
    InventoryInputs,
    InventoryOutcome,
    InventoryReservationParams,
    InventoryReservationSuggester,
    InventorySuggestion,
    InventoryUnavailable,
)

__all__ = [
    "FILENAME",
    "SLOTS",
    "SOURCE",
    "UNSCORED_MAE",
    "InventoryInputs",
    "InventoryOutcome",
    "InventoryReservationParams",
    "InventoryReservationPublisher",
    "InventoryReservationSuggester",
    "InventorySuggestion",
    "InventoryUnavailable",
    "inventory_excess",
    "inventory_publisher",
    "max_buy_price",
    "normalized_profile",
    "publish_outcome",
    "remaining_weight",
    "reservation_discount",
    "size_multiplier",
]
