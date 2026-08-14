from pathlib import Path

from docx import Document
from fastapi.testclient import TestClient
from pypdf import PdfWriter

from contract_review.config import get_settings
from contract_review.db import init_db
from contract_review.main import app
from contract_review.worker import run_once


def test_amount_upload_worker_and_delete(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    init_db()
    path = tmp_path / "invalid-structure.docx"
    Document().save(path)
    with TestClient(app) as client, path.open("rb") as stream:
        response = client.post("/api/amount-reviews", files={"files": (path.name, stream, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
        assert response.status_code == 202
        task_id = response.json()["tasks"][0]["id"]
        assert run_once()
        task = client.get(f"/api/amount-reviews/{task_id}").json()
        assert task["status"] == "failed_review"
        assert task["temp_path"] is None
        assert client.delete(f"/api/amount-reviews/{task_id}").status_code == 204
    get_settings.cache_clear()


def test_pair_preview_api(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()


def test_comparison_mock_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("COMPARISON_ADAPTER", "mock")
    get_settings.cache_clear()
    init_db()
    word = tmp_path / "NPA2026-XWZCG-1874.docx"
    pdf = tmp_path / "NPA2026-XWZCG-1874-盖章.pdf"
    Document().save(word)
    writer = PdfWriter(); writer.add_blank_page(width=595, height=842)
    with pdf.open("wb") as output:
        writer.write(output)
    with TestClient(app) as client, word.open("rb") as word_stream, pdf.open("rb") as pdf_stream:
        response = client.post(
            "/api/comparisons",
            files=[
                ("word_files", (word.name, word_stream, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")),
                ("pdf_files", (pdf.name, pdf_stream, "application/pdf")),
            ],
            data={
                "pairs_json": '[{"word_index":0,"pdf_index":0,"contract_no":"NPA2026-XWZCG-1874"}]',
                "ignore_options_json": '{"comments":true,"headerfooter":false,"stamp":true,"symbols":false,"watermark":false}',
            },
        )
        assert response.status_code == 202
        task_id = response.json()["task_ids"][0]
        assert run_once()
        task = client.get("/api/comparisons").json()["items"][0]
        assert task["status"] == "processing"
        assert task["textin_task_id"]
        assert '"comments": true' in task["ignore_options_json"]
        assert run_once()
        task = client.get("/api/comparisons").json()["items"][0]
        assert task["status"] == "completed"
        preview = client.get(f"/api/comparisons/{task_id}/preview")
        assert preview.status_code == 200
        assert "/api/mock-preview/" in preview.json()["preview_url"]
        assert client.delete(f"/api/comparisons/{task_id}").status_code == 204
    get_settings.cache_clear()
    with TestClient(app) as client:
        response = client.post("/api/comparisons/pair-preview", json={"word_names": ["NPA2026-XWZCG-1874.docx"], "pdf_names": ["NPA2026-XWZCG-1874-盖章.pdf"]})
        assert response.status_code == 200
        assert len(response.json()["pairs"]) == 1
    get_settings.cache_clear()
