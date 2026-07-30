import json
import os
import threading
import time
from pathlib import Path


class JournalEngine:
    def __init__(self, limit=1000, path=None):
        self.limit = limit
        default_path = Path(__file__).resolve().parents[1] / "data" / "trade_journal.json"
        self.path = Path(path or os.environ.get("BOT_JOURNAL_PATH") or default_path)
        self._lock = threading.Lock()
        self.entries = self._load()

    def add(self, event, payload=None):
        entry = {
            "time": int(time.time()),
            "event": event,
            "payload": payload or {},
        }
        with self._lock:
            self.entries.append(entry)
            self.entries = self.entries[-self.limit:]
            self._save()
        return entry

    def recent(self, limit=50):
        return self.entries[-limit:]

    def _load(self):
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(data, list):
            return []
        return data[-self.limit:]

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(self.entries, indent=2, default=str)
        self.path.write_text(body, encoding="utf-8")
