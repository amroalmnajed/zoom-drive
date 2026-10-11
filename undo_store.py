"""undo_store.py - remembers what each run did so it can be reverted."""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("zoom_to_drive")

MAX_ENTRIES = 50


class UndoStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: list[dict] = []
        if path.exists():
            try:
                self._data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("Could not read %s (%s); starting empty.", path, exc)

    def _save(self) -> None:
        self._data = self._data[-MAX_ENTRIES:]
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def add(self, entry: dict) -> str:
        entry = {"id": uuid.uuid4().hex[:10],
                 "at": datetime.now(timezone.utc).isoformat(),
                 "undone": False, **entry}
        self._data.append(entry)
        self._save()
        return entry["id"]

    def get(self, entry_id: str) -> Optional[dict]:
        return next((e for e in self._data if e["id"] == entry_id), None)

    def recent(self, n: int = 5) -> list[dict]:
        """Newest first, only entries that can still be undone."""
        return [e for e in reversed(self._data) if not e.get("undone")][:n]

    def mark_undone(self, entry_id: str) -> None:
        e = self.get(entry_id)
        if e:
            e["undone"] = True
            self._save()
