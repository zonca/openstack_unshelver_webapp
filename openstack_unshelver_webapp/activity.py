from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional


_LOGGER = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CaddyActivityMonitor:
    """Tails a Caddy JSON access log and triggers a callback when idle."""

    def __init__(
        self,
        *,
        log_path: str,
        upstream_label: str,
        idle_timeout: timedelta,
        poll_interval: int,
        on_idle: Callable[[], Awaitable[None]],
    ) -> None:
        self._log_path = Path(log_path)
        self._upstream_label = upstream_label
        self._idle_timeout = idle_timeout
        self._poll_interval = poll_interval
        self._on_idle = on_idle
        self._last_activity: Optional[datetime] = _utcnow()
        self._tail_task: Optional[asyncio.Task] = None
        self._idle_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._offset = 0

    def last_activity(self) -> Optional[datetime]:
        return self._last_activity

    async def start(self) -> None:
        self._stop_event.clear()
        self._tail_task = asyncio.create_task(self._tail_loop())
        self._idle_task = asyncio.create_task(self._idle_loop())

    async def stop(self) -> None:
        self._stop_event.set()
        for task in (self._tail_task, self._idle_task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def _tail_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._scan_once()
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("Failed to scan Caddy log: %s", exc, exc_info=True)
            await asyncio.sleep(self._poll_interval)

    async def _scan_once(self) -> None:
        path = self._log_path
        if not path.exists():
            return
        size = path.stat().st_size
        if size < self._offset:
            self._offset = 0  # log rotated/truncated
        with path.open("r", encoding="utf-8") as handle:
            handle.seek(self._offset)
            while True:
                line = handle.readline()
                if not line:
                    break
                self._offset = handle.tell()
                self._process_line(line)

    def _process_line(self, line: str) -> None:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return
        upstream = payload.get("upstream") or {}
        upstream_name = upstream.get("name") or ""
        if upstream_name != self._upstream_label:
            return
        self._last_activity = _utcnow()

    async def _idle_loop(self) -> None:
        while not self._stop_event.is_set():
            now = _utcnow()
            last = self._last_activity
            if last and now - last >= self._idle_timeout:
                try:
                    await self._on_idle()
                    self._last_activity = now
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.warning("Idle callback failed: %s", exc, exc_info=True)
            await asyncio.sleep(self._poll_interval)
