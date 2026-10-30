"""Backend logging: console, a rotating file in the data folder, and an in-memory buffer for the Logs page.

What is logged: API requests to this server, every call the toolkit makes to the target system (method,
path, status, duration — never headers, bodies or tokens), connector lookups, and every audit event of
every job. Secrets are never passed to the logger.
"""
import collections
import itertools
import logging
import logging.handlers
import threading
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


class MemoryHandler(logging.Handler):
    """Keeps the most recent records so the UI can show them without reading the log file."""

    def __init__(self, capacity=2000):
        super().__init__()
        self.records = collections.deque(maxlen=capacity)
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def emit(self, record):
        try:
            entry = {"ts": self.formatter.formatTime(record) if self.formatter else record.created,
                     "level": record.levelname, "logger": record.name, "message": record.getMessage()}
            if record.exc_info:
                entry["message"] += "\n" + logging.Formatter().formatException(record.exc_info)
        except Exception:  # never let logging break the app
            self.handleError(record)
            return
        with self._lock:
            entry["id"] = next(self._ids)
            self.records.append(entry)

    def since(self, after_id=0, level="DEBUG", limit=500):
        threshold = logging.getLevelName(level) if level in LEVELS else logging.DEBUG
        with self._lock:
            rows = [r for r in self.records if r["id"] > after_id and logging.getLevelName(r["level"]) >= threshold]
        return rows[-limit:]


memory = MemoryHandler()


def setup(data_dir=None, level="INFO"):
    """Configures the root logger once. Repeated calls only change the level."""
    level = (level or "INFO").upper()
    root = logging.getLogger()
    root.setLevel(level)
    if getattr(root, "_amtoolkit", False):
        return None
    formatter = logging.Formatter(FORMAT)
    console = logging.StreamHandler()
    handlers = [console, memory]
    log_file = None
    if data_dir is not None:
        log_dir = Path(data_dir) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "toolkit.log"
        handlers.append(logging.handlers.RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=5,
                                                             encoding="utf-8"))
    for handler in handlers:
        handler.setFormatter(formatter)
        root.addHandler(handler)
    root._amtoolkit = True
    # Never log request bodies or headers from the HTTP library; they can contain tokens.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    # Our own request log replaces the per-request access lines of the development server.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    return log_file
