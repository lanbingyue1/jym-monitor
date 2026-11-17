"""Filter rules applied to newly seen listings.

An empty value in :class:`~monitor.config.Filters` means "no restriction".
Exclude keywords always win: a listing matching both an include and an exclude
keyword is rejected.
"""

from __future__ import annotations

from monitor.config import Filters
from monitor.models import Listing


def matches(listing: Listing, filters: Filters) -> bool:
    """Return True when *listing* satisfies every configured rule."""
    if not _price_matches(listing.price, filters):
        return False
    if not _exact_matches(listing.extra.get("platform"), filters.platforms):
        return False
    if not _exact_matches(listing.extra.get("server"), filters.servers):
        return False

    haystack = _haystack(listing)
    if any(keyword.casefold() in haystack for keyword in filters.exclude_keywords if keyword):
        return False

    include = [keyword.casefold() for keyword in filters.include_keywords if keyword]
    if include and not any(keyword in haystack for keyword in include):
        return False
    return True


def _price_matches(price: int | None, filters: Filters) -> bool:
    if price is None:
        # An unknown price can only pass when no price range is configured.
        return filters.price_min is None and filters.price_max is None
    if filters.price_min is not None and price < filters.price_min:
        return False
    if filters.price_max is not None and price > filters.price_max:
        return False
    return True


def _exact_matches(value: str | None, allowed: tuple[str, ...]) -> bool:
    if not allowed:
        return True
    return value is not None and value in allowed


def _haystack(listing: Listing) -> str:
    parts = (
        listing.title or "",
        str(listing.extra.get("description") or ""),
        str(listing.extra.get("sell_points") or ""),
    )
    return " ".join(parts).casefold()
