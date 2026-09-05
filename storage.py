"""Atomic repository operations and verified SQLite backups."""

from functools import wraps
from contextlib import closing
from pathlib import Path
import sqlite3


def atomic(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            if getattr(self, "_transaction_depth", 0):
                return method(self, *args, **kwargs)
            self._transaction_depth = 1
            try:
                with self._conn:
                    self._conn.execute("BEGIN IMMEDIATE")
                    return method(self, *args, **kwargs)
            finally:
                self._transaction_depth = 0

    return wrapped


def verify_backup(path):
    path = Path(path).resolve()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        result = conn.execute("PRAGMA integrity_check").fetchall()
        if result != [("ok",)]:
            raise ValueError("SQLite integrity check failed")
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not {"users", "groups", "expenses"}.issubset(tables):
            raise ValueError("Not an expense bot database")


def copy_database(source, destination):
    """Copy a consistent snapshot, refusing to overwrite any existing file."""
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Reserve the path exclusively, including against another backup process.
    with destination.open("xb"):
        pass
    try:
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()
        verify_backup(destination)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return destination


def restore_backup(source, destination):
    """Restore to a new file; callers can point DB_PATH at it after inspection."""
    verify_backup(source)
    source = Path(source).resolve()
    conn = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    try:
        return copy_database(conn, destination)
    finally:
        conn.close()
