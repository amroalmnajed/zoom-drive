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
    """Manages undo history for uploads and docx changes."""

    def __init__(self, path: Path) -> None:
        """Initialize undo store from file.

        Args:
            path: Path to the undo history JSON file
        """
        self.path = path
        self._data: list[dict] = []
        if path.exists():
            try:
                self._data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("Could not read %s (%s); starting empty.", path, exc)

    def _save(self) -> None:
        """Persist undo history to disk, keeping only MAX_ENTRIES."""
        self._data = self._data[-MAX_ENTRIES:]
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def add(self, entry: dict) -> str:
        """Add an entry to undo history.

        Args:
            entry: Dictionary with upload/docx metadata

        Returns:
            Unique entry ID for undo operations
        """
        entry = {"id": uuid.uuid4().hex[:10],
                 "at": datetime.now(timezone.utc).isoformat(),
                 "undone": False, **entry}
        self._data.append(entry)
        self._save()
        return entry["id"]

    def get(self, entry_id: str) -> Optional[dict]:
        """Retrieve an entry by ID.

        Args:
            entry_id: Entry ID to retrieve

        Returns:
            Entry dictionary or None if not found
        """
        return next((e for e in self._data if e["id"] == entry_id), None)

    def recent(self, n: int = 5) -> list[dict]:
        """Get recent undoable entries.

        Args:
            n: Maximum number of entries to return

        Returns:
            List of entries (newest first) that haven't been undone
        """
        return [e for e in reversed(self._data) if not e.get("undone")][:n]

    def mark_undone(self, entry_id: str) -> None:
        """Mark an entry as undone.

        Args:
            entry_id: Entry ID to mark as undone
        """
        e = self.get(entry_id)
        if e:
            e["undone"] = True
            self._save()
