from __future__ import annotations

from io import BytesIO
import sqlite3

import pytest
from docx import Document
from fastapi.testclient import TestClient
from pypdf import PdfWriter

from contract_review.config import get_settings
from contract_review.db import connection, init_db
from contract_review.main import app
from contract_review import worker


DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def word_bytes() -> bytes:
    stream = BytesIO()
    Document().save(stream)
    return stream.getvalue()


def pdf_bytes() -> bytes:
    stream = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    writer.write(stream)
    return stream.getvalue()


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("COMPARISON_ADAPTER", "mock")
    get_settings.cache_clear()
    init_db()
    yield
    get_settings.cache_clear()


def test_combined_submission_creates_paired_and_review_only_tasks():
    word = word_bytes()
    pdf = pdf_bytes()

    with TestClient(app) as client:
        response = client.post(
            "/api/combined-checks",
            files=[
                ("word_files", ("NPA2026-A-1.docx", word, DOCX_MIME)),
                ("word_files", ("NPA2026-B-2.docx", word, DOCX_MIME)),
                ("pdf_files", ("NPA2026-A-1.pdf", pdf, "application/pdf")),
            ],
            data={
                "pairs_json": '[{"word_index":0,"pdf_index":0,"contract_no":"NPA2026-A-1"}]',
                "unmatched_word_indices_json": "[1]",
                "ignore_options_json": "{}",
            },
        )

        assert response.status_code == 202
        assert len(response.json()["task_ids"]) == 2
        items = client.get("/api/combined-checks").json()["items"]
        assert {
            (item["word_filename"], item["pdf_filename"], item["comparison_status"], item["review_status"])
            for item in items
        } == {
            ("NPA2026-A-1.docx", "NPA2026-A-1.pdf", "queued", "queued"),
            ("NPA2026-B-2.docx", None, "not_applicable", "queued"),
        }


def test_combined_submission_allows_only_unmatched_word_files():
    with TestClient(app) as client:
        response = client.post(
            "/api/combined-checks",
            files=[("word_files", ("NPA2026-B-2.docx", word_bytes(), DOCX_MIME))],
            data={
                "pairs_json": "[]",
                "unmatched_word_indices_json": "[0]",
                "ignore_options_json": "{}",
            },
        )

        assert response.status_code == 202
        item = client.get("/api/combined-checks").json()["items"][0]
        assert item["pdf_filename"] is None
        assert item["comparison_status"] == "not_applicable"


@pytest.mark.parametrize(
    "pairs_json,unmatched_json",
    [
        ('[{"word_index":0,"pdf_index":0}]', "[0]"),
        ('[{"word_index":1,"pdf_index":0}]', "[0]"),
        ('[{"word_index":0,"pdf_index":1}]', "[]"),
    ],
)
def test_combined_submission_rejects_duplicate_or_out_of_range_indexes(pairs_json, unmatched_json):
    with TestClient(app) as client:
        response = client.post(
            "/api/combined-checks",
            files=[
                ("word_files", ("NPA2026-A-1.docx", word_bytes(), DOCX_MIME)),
                ("pdf_files", ("NPA2026-A-1.pdf", pdf_bytes(), "application/pdf")),
            ],
            data={
                "pairs_json": pairs_json,
                "unmatched_word_indices_json": unmatched_json,
                "ignore_options_json": "{}",
            },
        )

        assert response.status_code == 400


def test_standalone_comparison_response_has_no_review_fields():
    with TestClient(app) as client:
        response = client.post(
            "/api/comparisons",
            files=[
                ("word_files", ("NPA2026-A-1.docx", word_bytes(), DOCX_MIME)),
                ("pdf_files", ("NPA2026-A-1.pdf", pdf_bytes(), "application/pdf")),
            ],
            data={"pairs_json": '[{"word_index":0,"pdf_index":0}]'},
        )
        assert response.status_code == 202
        item = client.get("/api/comparisons").json()["items"][0]
        assert "review_status" not in item
        assert "review_errors" not in item


def submit_combined_fixture(client: TestClient, include_pdf: bool = True) -> str:
    files = [("word_files", ("NPA2026-A-1.docx", word_bytes(), DOCX_MIME))]
    data = {"ignore_options_json": "{}"}
    if include_pdf:
        files.append(("pdf_files", ("NPA2026-A-1.pdf", pdf_bytes(), "application/pdf")))
        data.update({"pairs_json": '[{"word_index":0,"pdf_index":0}]', "unmatched_word_indices_json": "[]"})
    else:
        data.update({"pairs_json": "[]", "unmatched_word_indices_json": "[0]"})
    response = client.post("/api/combined-checks", files=files, data=data)
    assert response.status_code == 202
    return response.json()["task_ids"][0]


def test_combined_review_finishes_before_remote_comparison():
    with TestClient(app) as client:
        task_id = submit_combined_fixture(client)

        assert worker.process_combined_comparison()
        assert worker.process_combined_review()

        item = client.get("/api/combined-checks", params={"task_ids": task_id}).json()["items"][0]
        assert item["comparison_status"] == "processing"
        assert item["review_status"] == "passed"
        assert item["word_temp_path"] is None
        assert item["pdf_temp_path"] is None

        assert worker.sync_combined_comparison()
        completed = client.get("/api/combined-checks", params={"task_ids": task_id}).json()["items"][0]
        assert completed["comparison_status"] == "completed"


def test_unmatched_word_is_reviewed_without_comparison():
    with TestClient(app) as client:
        task_id = submit_combined_fixture(client, include_pdf=False)

        assert worker.process_combined_review()

        item = client.get("/api/combined-checks", params={"task_ids": task_id}).json()["items"][0]
        assert item["comparison_status"] == "not_applicable"
        assert item["pdf_filename"] is None
        assert item["review_status"] == "passed"
        assert item["word_temp_path"] is None


def test_init_db_migrates_legacy_auto_review_tasks(tmp_path, monkeypatch):
    legacy_data = tmp_path / "legacy-data"
    legacy_data.mkdir()
    database = legacy_data / "contract_review.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.executescript("""
        CREATE TABLE comparison_submissions (
            id TEXT PRIMARY KEY, contract_no TEXT, word_filename TEXT NOT NULL,
            pdf_filename TEXT NOT NULL, status TEXT NOT NULL, textin_task_id TEXT,
            preview_url TEXT, error_message TEXT, created_at TEXT NOT NULL, completed_at TEXT,
            word_temp_path TEXT, pdf_temp_path TEXT, similarity REAL, difference_count INTEGER,
            ignore_options_json TEXT NOT NULL DEFAULT '{}', auto_review INTEGER NOT NULL DEFAULT 0,
            review_status TEXT NOT NULL DEFAULT 'pending', review_errors_json TEXT NOT NULL DEFAULT '[]',
            review_summary_json TEXT NOT NULL DEFAULT '{}', review_parser_version TEXT NOT NULL DEFAULT '',
            review_completed_at TEXT
        );
        """)
        conn.execute(
            """INSERT INTO comparison_submissions (
                id, word_filename, pdf_filename, status, created_at, word_temp_path,
                pdf_temp_path, auto_review, review_status
            ) VALUES ('11111111-1111-1111-1111-111111111111', 'legacy.docx', 'legacy.pdf',
                      'queued', '2026-08-26T00:00:00+00:00', '/tmp/legacy.docx',
                      '/tmp/legacy.pdf', 1, 'pending')"""
        )

    monkeypatch.setenv("DATA_DIR", str(legacy_data))
    get_settings.cache_clear()
    init_db()

    with connection() as conn:
        migrated = conn.execute(
            "SELECT * FROM combined_check_tasks WHERE id = '11111111-1111-1111-1111-111111111111'"
        ).fetchone()
        old = conn.execute(
            "SELECT * FROM comparison_submissions WHERE id = '11111111-1111-1111-1111-111111111111'"
        ).fetchone()
    assert migrated["comparison_status"] == "queued"
    assert migrated["review_status"] == "queued"
    assert migrated["word_temp_path"] == "/tmp/legacy.docx"
    assert old is None
