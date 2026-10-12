import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
import config

log = logging.getLogger("zoom_to_drive")

class StateStore:
    """Manages persistence of processed recording state."""

    def __init__(self, path: Path) -> None:
        """Initialize state store from file.

        Args:
            path: Path to the state JSON file
        """
        self.path = path
        self._data: dict[str, dict] = {}
        if path.exists():
            try:
                self._data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("Could not read %s (%s); starting empty.", path, exc)

    def should_hide(self, key: str) -> bool:
        """Check if a recording should be hidden from future runs.

        Args:
            key: Recording key to check

        Returns:
            True if recording was uploaded or (skipped and REMEMBER_SKIPPED is True)
        """
        entry = self._data.get(key)
        if not entry:
            return False
        if entry.get("status") == "uploaded":
            return True
        return entry.get("status") == "skipped" and config.REMEMBER_SKIPPED

    def mark(self, key: str, status: str, **info) -> None:
        """Mark a recording with a status and additional metadata.

        Args:
            key: Recording key to mark
            status: Status string (e.g., "uploaded", "skipped", "failed")
            **info: Additional metadata to store
        """
        self._data[key] = {"status": status, "at": datetime.now(timezone.utc).isoformat(), **info}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


    def forget(self, key: str) -> None:
        """Remove a recording from state so it will be offered again.

        Args:
            key: Recording key to forget
        """
        if key in self._data:
            del self._data[key]
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
