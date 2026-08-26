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

        CREATE TABLE IF NOT EXISTS combined_check_tasks (
            id TEXT PRIMARY KEY,
            contract_no TEXT,
            word_filename TEXT NOT NULL,
            pdf_filename TEXT,
            comparison_status TEXT NOT NULL,
            review_status TEXT NOT NULL,
            textin_task_id TEXT,
            preview_url TEXT,
            comparison_error TEXT,
            created_at TEXT NOT NULL,
            comparison_completed_at TEXT,
            review_completed_at TEXT,
            word_temp_path TEXT,
            pdf_temp_path TEXT,
            similarity REAL,
            difference_count INTEGER,
            ignore_options_json TEXT NOT NULL DEFAULT '{}',
            review_errors_json TEXT NOT NULL DEFAULT '[]',
            review_summary_json TEXT NOT NULL DEFAULT '{}',
            review_parser_version TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_combined_created ON combined_check_tasks(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_combined_comparison_status ON combined_check_tasks(comparison_status);
        CREATE INDEX IF NOT EXISTS idx_combined_review_status ON combined_check_tasks(review_status);
        """)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(comparison_submissions)")}
        if "ignore_options_json" not in columns:
            conn.execute("ALTER TABLE comparison_submissions ADD COLUMN ignore_options_json TEXT NOT NULL DEFAULT '{}'")
        _migrate_legacy_combined_tasks(conn, columns)


def _migrate_legacy_combined_tasks(conn: sqlite3.Connection, comparison_columns: set[str]) -> None:
    """Move tasks created by the short-lived auto_review implementation."""
    if "auto_review" not in comparison_columns:
        return
    rows = conn.execute("SELECT * FROM comparison_submissions WHERE auto_review = 1").fetchall()
    for row in rows:
        task = dict(row)
        legacy_review_status = task.get("review_status") or "pending"
        review_status = "queued" if legacy_review_status in {"pending", "reviewing", "processing"} else legacy_review_status
        conn.execute(
            """INSERT INTO combined_check_tasks (
                id, contract_no, word_filename, pdf_filename, comparison_status, review_status,
                textin_task_id, preview_url, comparison_error, created_at,
                comparison_completed_at, review_completed_at, word_temp_path, pdf_temp_path,
                similarity, difference_count, ignore_options_json, review_errors_json,
                review_summary_json, review_parser_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                contract_no=excluded.contract_no,
                word_filename=excluded.word_filename,
                pdf_filename=excluded.pdf_filename,
                comparison_status=excluded.comparison_status,
                review_status=excluded.review_status,
                textin_task_id=excluded.textin_task_id,
                preview_url=excluded.preview_url,
                comparison_error=excluded.comparison_error,
                created_at=excluded.created_at,
                comparison_completed_at=excluded.comparison_completed_at,
                review_completed_at=excluded.review_completed_at,
                word_temp_path=excluded.word_temp_path,
                pdf_temp_path=excluded.pdf_temp_path,
                similarity=excluded.similarity,
                difference_count=excluded.difference_count,
                ignore_options_json=excluded.ignore_options_json,
                review_errors_json=excluded.review_errors_json,
                review_summary_json=excluded.review_summary_json,
                review_parser_version=excluded.review_parser_version""",
            (
                task["id"], task.get("contract_no"), task["word_filename"], task.get("pdf_filename") or None,
                task["status"], review_status, task.get("textin_task_id"),
                task.get("preview_url"), task.get("error_message"), task["created_at"],
                task.get("completed_at"), task.get("review_completed_at"), task.get("word_temp_path"),
                task.get("pdf_temp_path"), task.get("similarity"), task.get("difference_count"),
                task.get("ignore_options_json") or "{}", task.get("review_errors_json") or "[]",
                task.get("review_summary_json") or "{}", task.get("review_parser_version") or "",
            ),
        )
        conn.execute("DELETE FROM comparison_submissions WHERE id = ?", (task["id"],))


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def decode_task(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    task = dict(row)
    for field in ("errors_json", "summary_json"):
        if field in task:
            target = field.removesuffix("_json")
            task[target] = json.loads(task.pop(field) or ("[]" if field == "errors_json" else "{}"))
    return task


def decode_combined_task(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    task = dict(row)
    task["review_errors"] = json.loads(task.pop("review_errors_json") or "[]")
    task["review_summary"] = json.loads(task.pop("review_summary_json") or "{}")
    return task
