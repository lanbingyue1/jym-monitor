"""Protocol implemented by every site adapter."""

from __future__ import annotations

from typing import Protocol

from monitor.models import Listing


class SiteAdapter(Protocol):
    """Minimal interface the pipeline uses to fetch listings from one site."""

    site_id: str

    def fetch(self, known_ids: frozenset[str] = frozenset()) -> list[Listing]:
        """Return the current listings, using *known_ids* to stop paging."""
        ...
