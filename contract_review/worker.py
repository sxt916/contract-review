from __future__ import annotations

import json
import logging
import signal
import time
from pathlib import Path

from .config import get_settings
from .db import connection, init_db, utc_now
from .reviewer import review_docx
from .textin import get_adapter

logger = logging.getLogger(__name__)
stopping = False


def recover_interrupted() -> None:
    with connection() as conn:
        conn.execute("UPDATE amount_review_tasks SET status = 'queued' WHERE status = 'processing'")
        conn.execute("UPDATE comparison_submissions SET status = 'queued' WHERE status = 'submitting' AND textin_task_id IS NULL")
        conn.execute("UPDATE combined_check_tasks SET comparison_status = 'queued' WHERE comparison_status = 'submitting' AND textin_task_id IS NULL")
        conn.execute("UPDATE combined_check_tasks SET review_status = 'queued' WHERE review_status = 'processing'")


def process_amount() -> bool:
    with connection() as conn:
        row = conn.execute("SELECT * FROM amount_review_tasks WHERE status = 'queued' ORDER BY created_at LIMIT 1").fetchone()
        if not row:
            return False
        updated = conn.execute("UPDATE amount_review_tasks SET status = 'processing' WHERE id = ? AND status = 'queued'", (row["id"],)).rowcount
    if not updated:
        return True
    path = Path(row["temp_path"])
    try:
        result = review_docx(path, row["filename"])
        with connection() as conn:
            conn.execute("""UPDATE amount_review_tasks SET contract_no=?, status=?, errors_json=?, summary_json=?, parser_version=?, completed_at=?, temp_path=NULL WHERE id=?""",
                         (result["contract_no"], result["status"], json.dumps(result["errors"], ensure_ascii=False), json.dumps(result["summary"], ensure_ascii=False), result["parser_version"], utc_now(), row["id"]))
    except Exception as exc:
        error = [{"location": "文件", "error_type": "文件解析失败", "original": row["filename"], "expected": "有效且未加密的 DOCX", "reason": str(exc)}]
        with connection() as conn:
            conn.execute("UPDATE amount_review_tasks SET status='parse_error', errors_json=?, completed_at=?, temp_path=NULL WHERE id=?", (json.dumps(error, ensure_ascii=False), utc_now(), row["id"]))
        logger.exception("Amount task failed: %s", row["id"])
    finally:
        path.unlink(missing_ok=True)
    return True


def process_comparison() -> bool:
    with connection() as conn:
        row = conn.execute("SELECT * FROM comparison_submissions WHERE status = 'queued' ORDER BY created_at LIMIT 1").fetchone()
        if not row:
            return False
        updated = conn.execute("UPDATE comparison_submissions SET status='submitting' WHERE id=? AND status='queued'", (row["id"],)).rowcount
    if not updated:
        return True
    paths = [Path(row["word_temp_path"]), Path(row["pdf_temp_path"])]
    try:
        adapter = get_adapter()
        ignore_options = json.loads(row["ignore_options_json"] or "{}")
        created = adapter.create(paths[0], paths[1], row["word_filename"], row["pdf_filename"], ignore_options)
        with connection() as conn:
            # Persist the remote id before polling. TextIn commonly reports
            # 10306 (comparison still processing) immediately after creation;
            # treating that response as a create failure loses the task id and
            # makes the otherwise valid remote task impossible to synchronize.
            conn.execute("""UPDATE comparison_submissions SET status='processing', textin_task_id=?, preview_url=?,
                         completed_at=NULL, similarity=NULL, difference_count=NULL, error_message=NULL,
                         word_temp_path=NULL, pdf_temp_path=NULL WHERE id=?""",
                         (created.task_id, created.preview_url, row["id"]))
    except Exception as exc:
        with connection() as conn:
            conn.execute("UPDATE comparison_submissions SET status='failed', error_message=?, completed_at=?, word_temp_path=NULL, pdf_temp_path=NULL WHERE id=?", (str(exc), utc_now(), row["id"]))
        logger.exception("Comparison task failed: %s", row["id"])
    finally:
        for path in paths:
            path.unlink(missing_ok=True)
    return True


def sync_comparison() -> bool:
    with connection() as conn:
        row = conn.execute("SELECT * FROM comparison_submissions WHERE status = 'processing' AND textin_task_id IS NOT NULL ORDER BY created_at LIMIT 1").fetchone()
    if not row:
        return False
    try:
        remote = get_adapter().status(row["textin_task_id"])
        with connection() as conn:
            conn.execute("""UPDATE comparison_submissions SET status=?, similarity=?, difference_count=?, error_message=?, completed_at=? WHERE id=?""",
                         (remote.state, remote.similarity, remote.difference_count, remote.error_message, utc_now() if remote.state in {"completed", "failed"} else None, row["id"]))
    except Exception as exc:
        logger.warning("Comparison status sync failed for %s: %s", row["id"], exc)
    return True


def _cleanup_combined_word(task_id: str) -> None:
    comparison_done_reading = {"processing", "completed", "failed", "not_applicable"}
    review_done = {"passed", "failed_review", "parse_error"}
    path: Path | None = None
    with connection() as conn:
        row = conn.execute(
            "SELECT comparison_status, review_status, word_temp_path FROM combined_check_tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if (
            row
            and row["word_temp_path"]
            and row["comparison_status"] in comparison_done_reading
            and row["review_status"] in review_done
        ):
            path = Path(row["word_temp_path"])
            conn.execute("UPDATE combined_check_tasks SET word_temp_path = NULL WHERE id = ?", (task_id,))
    if path:
        path.unlink(missing_ok=True)


def process_combined_comparison() -> bool:
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM combined_check_tasks WHERE comparison_status = 'queued' ORDER BY created_at LIMIT 1"
        ).fetchone()
        if not row:
            return False
        updated = conn.execute(
            "UPDATE combined_check_tasks SET comparison_status = 'submitting' WHERE id = ? AND comparison_status = 'queued'",
            (row["id"],),
        ).rowcount
    if not updated:
        return True
    word_path = Path(row["word_temp_path"])
    pdf_path = Path(row["pdf_temp_path"])
    try:
        adapter = get_adapter()
        ignore_options = json.loads(row["ignore_options_json"] or "{}")
        created = adapter.create(word_path, pdf_path, row["word_filename"], row["pdf_filename"], ignore_options)
        with connection() as conn:
            conn.execute(
                """UPDATE combined_check_tasks SET comparison_status='processing', textin_task_id=?,
                   preview_url=?, comparison_error=NULL, pdf_temp_path=NULL WHERE id=?""",
                (created.task_id, created.preview_url, row["id"]),
            )
    except Exception as exc:
        with connection() as conn:
            conn.execute(
                """UPDATE combined_check_tasks SET comparison_status='failed', comparison_error=?,
                   comparison_completed_at=?, pdf_temp_path=NULL WHERE id=?""",
                (str(exc), utc_now(), row["id"]),
            )
        logger.exception("Combined comparison submission failed: %s", row["id"])
    finally:
        pdf_path.unlink(missing_ok=True)
        _cleanup_combined_word(row["id"])
    return True


def process_combined_review() -> bool:
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM combined_check_tasks WHERE review_status = 'queued' ORDER BY created_at LIMIT 1"
        ).fetchone()
        if not row:
            return False
        updated = conn.execute(
            "UPDATE combined_check_tasks SET review_status = 'processing' WHERE id = ? AND review_status = 'queued'",
            (row["id"],),
        ).rowcount
    if not updated:
        return True
    path = Path(row["word_temp_path"])
    try:
        result = review_docx(path, row["word_filename"])
        with connection() as conn:
            conn.execute(
                """UPDATE combined_check_tasks SET contract_no=COALESCE(contract_no, ?), review_status=?,
                   review_errors_json=?, review_summary_json=?, review_parser_version=?, review_completed_at=?
                   WHERE id=?""",
                (
                    result["contract_no"], result["status"], json.dumps(result["errors"], ensure_ascii=False),
                    json.dumps(result["summary"], ensure_ascii=False), result["parser_version"], utc_now(), row["id"],
                ),
            )
    except Exception as exc:
        errors = [{
            "location": "文件", "error_type": "文件解析失败", "original": row["word_filename"],
            "expected": "有效且未加密的 DOCX", "reason": str(exc),
        }]
        with connection() as conn:
            conn.execute(
                """UPDATE combined_check_tasks SET review_status='parse_error', review_errors_json=?,
                   review_completed_at=? WHERE id=?""",
                (json.dumps(errors, ensure_ascii=False), utc_now(), row["id"]),
            )
        logger.exception("Combined review failed: %s", row["id"])
    finally:
        _cleanup_combined_word(row["id"])
    return True


def sync_combined_comparison() -> bool:
    with connection() as conn:
        row = conn.execute(
            """SELECT * FROM combined_check_tasks WHERE comparison_status = 'processing'
               AND textin_task_id IS NOT NULL ORDER BY created_at LIMIT 1"""
        ).fetchone()
    if not row:
        return False
    try:
        remote = get_adapter().status(row["textin_task_id"])
        with connection() as conn:
            conn.execute(
                """UPDATE combined_check_tasks SET comparison_status=?, similarity=?, difference_count=?,
                   comparison_error=?, comparison_completed_at=? WHERE id=?""",
                (
                    remote.state, remote.similarity, remote.difference_count, remote.error_message,
                    utc_now() if remote.state in {"completed", "failed"} else None, row["id"],
                ),
            )
    except Exception as exc:
        logger.warning("Combined comparison status sync failed for %s: %s", row["id"], exc)
    return True


def run_once() -> bool:
    return (
        process_amount()
        or process_comparison()
        or process_combined_comparison()
        or process_combined_review()
        or sync_comparison()
        or sync_combined_comparison()
    )


def run() -> None:
    global stopping
    logging.basicConfig(level=logging.INFO)
    init_db(); recover_interrupted()
    signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("stopping", True))
    signal.signal(signal.SIGINT, lambda *_: globals().__setitem__("stopping", True))
    while not stopping:
        if not run_once():
            time.sleep(get_settings().worker_poll_seconds)


if __name__ == "__main__":
    run()
