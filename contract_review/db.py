from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .config import get_settings


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    settings = get_settings()
    settings.ensure_dirs()
    conn = sqlite3.connect(settings.database_path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connection() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS amount_review_tasks (
            id TEXT PRIMARY KEY,
            contract_no TEXT,
            filename TEXT NOT NULL,
            status TEXT NOT NULL,
            errors_json TEXT NOT NULL DEFAULT '[]',
            summary_json TEXT NOT NULL DEFAULT '{}',
            parser_version TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            completed_at TEXT,
            temp_path TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_amount_created ON amount_review_tasks(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_amount_status ON amount_review_tasks(status);

        CREATE TABLE IF NOT EXISTS comparison_submissions (
            id TEXT PRIMARY KEY,
            contract_no TEXT,
            word_filename TEXT NOT NULL,
            pdf_filename TEXT NOT NULL,
            status TEXT NOT NULL,
            textin_task_id TEXT,
            preview_url TEXT,
            error_message TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            word_temp_path TEXT,
            pdf_temp_path TEXT,
            similarity REAL,
            difference_count INTEGER,
            ignore_options_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_comparison_created ON comparison_submissions(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_comparison_status ON comparison_submissions(status);
        """)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(comparison_submissions)")}
        if "ignore_options_json" not in columns:
            conn.execute("ALTER TABLE comparison_submissions ADD COLUMN ignore_options_json TEXT NOT NULL DEFAULT '{}'")


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def decode_task(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    task = dict(row)
    for field in ("errors_json", "summary_json"):
        if field in task:
            target = field.removesuffix("_json")
            task[target] = json.loads(task.pop(field) or ("[]" if field == "errors_json" else "{}"))
    return task
