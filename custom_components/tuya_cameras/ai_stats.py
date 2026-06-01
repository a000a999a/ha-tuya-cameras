"""Rolling 7-day AI detection statistics, persisted via HA Store."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

_LOGGER = logging.getLogger(__name__)
STORAGE_VERSION = 1
RETENTION_DAYS  = 7


class AIStats:
    """Stores per-event records and exposes 7-day rolling counts."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store = Store(hass, STORAGE_VERSION, f"tuya_cameras_ai_stats_{entry_id}")
        self._events: list[dict] = []

    async def async_load(self) -> None:
        data = await self._store.async_load()
        if data:
            self._events = data.get("events", [])
        self._prune()

    def _prune(self) -> None:
        cutoff = (datetime.now(tz=timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat()
        self._events = [e for e in self._events if e["ts"] >= cutoff]

    async def async_record(self, *, human: bool, area: str, camera: str) -> None:
        self._prune()
        self._events.append({
            "ts":     datetime.now(tz=timezone.utc).isoformat(),
            "human":  human,
            "area":   area,
            "camera": camera,
        })
        await self._store.async_save({"events": self._events})

    def counts(self) -> dict:
        """Return {total, human, other} for the rolling 7-day window."""
        self._prune()
        total = len(self._events)
        human = sum(1 for e in self._events if e["human"])
        return {"total": total, "human": human, "other": total - human}

    def last_human_ts(self, area: str) -> str | None:
        """ISO timestamp of the most recent human detection in an area, or None."""
        hits = [e["ts"] for e in self._events if e["human"] and e["area"] == area]
        return max(hits, default=None)

    def known_areas(self) -> set[str]:
        return {e["area"] for e in self._events}
