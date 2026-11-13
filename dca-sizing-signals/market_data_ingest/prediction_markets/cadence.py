"""Polling cadence (D3): every market at the default interval; watchlist markets faster, with
order-book snapshots. Both intervals live in config. Nothing here schedules anything -- a
future snapshot job asks these two functions what to poll and when (ADR §14)."""

from __future__ import annotations


def is_watched(config, venue: str, market_id: str) -> bool:
    # Venue-scoped: a Polymarket US slug never matches an Intl conditionId entry (ADR §3).
    return (venue, market_id) in config.watchlist


def interval_for(config, venue: str, market_id: str) -> int:
    if is_watched(config, venue, market_id):
        return config.cadence.watchlist_interval_s
    return config.cadence.default_interval_s


def captures_orderbook(config, venue: str, market_id: str) -> bool:
    # Neither venue serves historical books (ADR §4), so only watched markets get snapshots.
    return is_watched(config, venue, market_id)
