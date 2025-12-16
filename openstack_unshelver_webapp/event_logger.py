from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from openstack import connection

from .config import OpenStackSettings


_LOGGER = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class EventEntry:
    timestamp: datetime
    action: str
    actor: str
    instance_name: str
    detail: Optional[str]

    def as_json(self) -> str:
        payload = {
            "timestamp": self.timestamp.isoformat(),
            "action": self.action,
            "actor": self.actor,
            "instance_name": self.instance_name,
            "detail": self.detail,
        }
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


class EventLogger:
    """Writes controller events to disk and optionally to OpenStack Swift."""

    def __init__(
        self,
        *,
        local_path: str,
        openstack_settings: OpenStackSettings,
        swift_container: Optional[str],
        swift_prefix: str,
    ) -> None:
        self._local_path = Path(local_path)
        self._swift_container = swift_container
        self._swift_prefix = swift_prefix.strip("/")
        self._openstack_settings = openstack_settings

    async def log(self, action: str, *, actor: str, instance_name: str, detail: Optional[str] = None) -> None:
        entry = EventEntry(timestamp=_utcnow(), action=action, actor=actor, instance_name=instance_name, detail=detail)
        line = entry.as_json()
        tasks = [self._write_local(line)]
        if self._swift_container:
            tasks.append(self._write_swift(line, entry.timestamp))
        await asyncio.gather(*tasks)

    async def _write_local(self, line: str) -> None:
        await asyncio.to_thread(self._append_line, line)

    def _append_line(self, line: str) -> None:
        self._local_path.parent.mkdir(parents=True, exist_ok=True)
        with self._local_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{line}\n")

    async def _write_swift(self, line: str, timestamp: datetime) -> None:
        await asyncio.to_thread(self._upload_object, line, timestamp)

    def _upload_object(self, line: str, timestamp: datetime) -> None:
        if not self._swift_container:
            return
        payload = self._openstack_settings.model_dump(exclude_none=True, mode="json")
        conn = connection.Connection(**payload)
        object_name = self._build_object_name(timestamp)
        try:
            conn.object_store.create_container(self._swift_container)  # idempotent
        except Exception:  # pragma: no cover - requires Swift
            _LOGGER.debug("Container creation failed or already exists", exc_info=True)
        try:
            conn.object_store.upload_object(
                container=self._swift_container,
                name=object_name,
                data=f"{line}\n",
            )
        except Exception as exc:  # pragma: no cover - requires Swift
            _LOGGER.warning("Failed to upload log entry to Swift: %s", exc, exc_info=True)
        finally:
            conn.close()

    def _build_object_name(self, timestamp: datetime) -> str:
        safe_timestamp = timestamp.strftime("%Y%m%dT%H%M%S.%fZ")
        if self._swift_prefix:
            return f"{self._swift_prefix}/{safe_timestamp}.jsonl"
        return f"{safe_timestamp}.jsonl"
