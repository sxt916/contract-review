from pathlib import Path
from html.parser import HTMLParser

from docx import Document
from fastapi.testclient import TestClient
from pypdf import PdfWriter

from contract_review.config import get_settings
from contract_review.db import init_db
from contract_review.main import app
from contract_review.worker import run_once


class _NavLinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_nav = False
        self.current_href = None
        self.current_text = []
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "nav":
            self.in_nav = True
        elif self.in_nav and tag == "a":
            self.current_href = dict(attrs).get("href")
            self.current_text = []

    def handle_data(self, data):
        if self.current_href is not None:
            self.current_text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.current_href is not None:
            self.links.append(("".join(self.current_text).strip(), self.current_href))
            self.current_href = None
        elif tag == "nav":
            self.in_nav = False


def test_work_pages_offer_comparison_and_amount_review_navigation():
    expected = [("合同对比", "comparison.html"), ("金额审核", "amount-review.html")]
    with TestClient(app) as client:
        for path in (
            "/comparison.html",
            "/comparison-results.html",
            "/amount-review.html",
            "/amount-review-results.html",
        ):
            response = client.get(path)
            assert response.status_code == 200
            parser = _NavLinkParser()
            parser.feed(response.text)
            assert parser.links == expected, path


def test_amount_upload_worker_and_delete(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    init_db()
    path = tmp_path / "no-amount-relationships.docx"
    other_path = tmp_path / "other-contract.docx"
    Document().save(path)
    Document().save(other_path)
    with TestClient(app) as client, path.open("rb") as stream:
        response = client.post("/api/amount-reviews", files={"files": (path.name, stream, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
        assert response.status_code == 202
        task_id = response.json()["tasks"][0]["id"]
        assert run_once()
        task = client.get(f"/api/amount-reviews/{task_id}").json()
        assert task["status"] == "passed"
        with other_path.open("rb") as other_stream:
            other_response = client.post("/api/amount-reviews", files={"files": (other_path.name, other_stream, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
        other_task_id = other_response.json()["tasks"][0]["id"]
        assert run_once()
        current_batch = client.get("/api/amount-reviews", params={"task_ids": task_id}).json()
        assert current_batch["total"] == 1
        assert current_batch["items"][0]["id"] == task_id
        filtered = client.get("/api/amount-reviews", params={"filename": "no-amount", "status": "passed"}).json()
        assert filtered["total"] == 1
        assert client.get("/api/amount-reviews", params={"task_ids": "invalid"}).status_code == 400
        assert task["temp_path"] is None
        assert client.delete(f"/api/amount-reviews/{task_id}").status_code == 204
        assert client.delete(f"/api/amount-reviews/{other_task_id}").status_code == 204
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
        filtered = client.get("/api/comparisons", params={"filename": "XWZCG-1874", "status": "completed"}).json()
        assert filtered["total"] == 1
        assert filtered["items"][0]["id"] == task_id
        current_batch = client.get("/api/comparisons", params={"task_ids": task_id}).json()
        assert current_batch["total"] == 1
        assert client.get("/api/comparisons", params={"status": "unknown"}).status_code == 400
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
