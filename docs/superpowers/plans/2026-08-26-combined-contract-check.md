# Combined Contract Check Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 保持原合同对比和金额审核功能不变，以独立任务模型实现对比/审核并行、未配对 Word 审核和文件/文件夹双选择入口。

**Architecture:** 新增 `combined_check_tasks` 表和 `/api/combined-checks` 路由，合并页面不再复用 `/api/comparisons` 的任务模型。Worker 分别领取合并任务的对比和审核状态，TextIn 提交后立即可处理本地 Word 审核，两者不互相等待。原对比页、结果页、API 和 Worker 恢复原行为。

**Tech Stack:** Python 3.12, FastAPI, SQLite, python-docx, pypdf, vanilla HTML/CSS/JavaScript, pytest, Node.js VM frontend tests.

**Spec:** `docs/superpowers/specs/2026-08-26-combined-contract-check-design.md`

## Global Constraints

- 不更改 `/api/amount-reviews` 的请求、状态和结果结构。
- 不在 `/api/comparisons` 中接收或执行审核。
- 未配对 Word 必须创建合并任务，`comparison_status='not_applicable'`。
- 审核处理不能以对比状态为前置条件。
- 所有工作页右上角保留“合同对比 / 金额审核 / 对比与审核”三个入口。

---

### Task 1: Restore the two standalone workflows

**Files:**
- Modify: `contract_review/api.py`
- Modify: `contract_review/db.py`
- Modify: `contract_review/worker.py`
- Modify: `contract_review/static/comparison.html`
- Modify: `contract_review/static/comparison.js`
- Modify: `contract_review/static/comparison-results.html`
- Modify: `contract_review/static/comparison-results.js`
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: existing `POST /api/comparisons`, `GET /api/comparisons`, `process_comparison()`, `sync_comparison()`.
- Produces: the original comparison-only API and UI behavior, with the third navigation link as the only visible addition.

- [ ] **Step 1: Write the failing standalone regression tests**

Add assertions that `/api/comparisons` rejects the removed `auto_review` behavior by ignoring the extra form field, returns no `review_status` or `review_errors`, deletes both temporary files after TextIn submission, and never calls `review_docx`. Keep the navigation test expecting exactly three top links.

```python
def test_standalone_comparison_does_not_run_amount_review(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("COMPARISON_ADAPTER", "mock")
    get_settings.cache_clear(); init_db()
    word = BytesIO(); Document().save(word)
    pdf = BytesIO(); writer = PdfWriter(); writer.add_blank_page(width=595, height=842); writer.write(pdf)
    monkeypatch.setattr("contract_review.worker.review_docx", lambda *_: pytest.fail("standalone comparison invoked review"))
    with TestClient(app) as client:
        response = client.post(
            "/api/comparisons",
            files=[
                ("word_files", ("NPA2026-A-1.docx", word.getvalue(), DOCX_MIME)),
                ("pdf_files", ("NPA2026-A-1.pdf", pdf.getvalue(), "application/pdf")),
            ],
            data={"pairs_json": '[{"word_index":0,"pdf_index":0}]', "auto_review": "true"},
        )
        assert response.status_code == 202
        assert run_once()
        submitted = client.get("/api/comparisons").json()["items"][0]
        assert submitted["word_temp_path"] is None
        assert submitted["pdf_temp_path"] is None
        assert "review_status" not in submitted
        assert "review_errors" not in submitted
        assert run_once()
```

- [ ] **Step 2: Run the regression test and verify RED**

Run: `.venv/bin/python -m pytest tests/test_api.py::test_standalone_comparison_does_not_run_amount_review -q`

Expected: FAIL because comparison responses currently expose review fields and the Worker retains the Word for review.

- [ ] **Step 3: Restore the original comparison path**

Remove `auto_review` from `create_comparisons()`, return plain comparison rows, restore immediate Word/PDF cleanup after remote submission, and restore the comparison-only table/filters/scripts. Retain only the third navigation link and the improved asset cache version where needed.

- [ ] **Step 4: Run the standalone and amount-review tests**

Run: `.venv/bin/python -m pytest tests/test_api.py tests/test_amount_upload_frontend.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the isolated restoration**

```bash
git add contract_review/api.py contract_review/db.py contract_review/worker.py \
  contract_review/static/comparison.html contract_review/static/comparison.js \
  contract_review/static/comparison-results.html contract_review/static/comparison-results.js \
  tests/test_api.py
git commit -m "fix: restore standalone contract workflows"
```

### Task 2: Add the independent combined task API and migration

**Files:**
- Modify: `contract_review/db.py`
- Modify: `contract_review/api.py`
- Create: `tests/test_combined_checks.py`

**Interfaces:**
- Produces: `POST /api/combined-checks`, `GET /api/combined-checks`, `GET /api/combined-checks/{id}/preview`, `DELETE /api/combined-checks/{id}`.
- Produces: `decode_combined_task(row) -> dict`.
- Produces for later tests: `submit_combined_fixture(client, word_bytes: bytes, pdf_bytes: bytes) -> str` in `tests/test_combined_checks.py`.
- Consumes: existing `_store_upload()`, pairing indexes, `DEFAULT_IGNORE_OPTIONS`, and TextIn preview/delete adapters.

- [ ] **Step 1: Write failing API tests for paired and unmatched Word tasks**

```python
def test_combined_submission_creates_paired_and_review_only_tasks(client, word_bytes, pdf_bytes):
    response = client.post(
        "/api/combined-checks",
        files=[
            ("word_files", ("NPA2026-A-1.docx", word_bytes, DOCX_MIME)),
            ("word_files", ("NPA2026-B-2.docx", word_bytes, DOCX_MIME)),
            ("pdf_files", ("NPA2026-A-1.pdf", pdf_bytes, "application/pdf")),
        ],
        data={
            "pairs_json": '[{"word_index":0,"pdf_index":0}]',
            "unmatched_word_indices_json": "[1]",
            "ignore_options_json": "{}",
        },
    )
    assert response.status_code == 202
    items = client.get("/api/combined-checks").json()["items"]
    assert {(item["word_filename"], item["comparison_status"]) for item in items} == {
        ("NPA2026-A-1.docx", "queued"),
        ("NPA2026-B-2.docx", "not_applicable"),
    }
```

Also test duplicate/out-of-range indexes and submission with zero pairs but one unmatched Word.

- [ ] **Step 2: Run the new API tests and verify RED**

Run: `.venv/bin/python -m pytest tests/test_combined_checks.py -q`

Expected: FAIL with 404 because `/api/combined-checks` does not exist.

- [ ] **Step 3: Create `combined_check_tasks` and decoding**

Add the independent table with nullable PDF fields, independent statuses, JSON result fields, remote comparison fields, timestamps, temporary paths and indexes on creation/status fields. Implement `decode_combined_task()` to expose `review_errors` and `review_summary` as decoded values.

- [ ] **Step 4: Implement the combined CRUD API**

Validate that every Word index appears exactly once across `pairs_json` and `unmatched_word_indices_json`. Insert paired rows with `comparison_status='queued'` and unmatched rows with `comparison_status='not_applicable'`; every row begins with `review_status='queued'`.

- [ ] **Step 5: Add and test compatibility migration**

Create an old-format database row with `auto_review=1`, call `init_db()`, then assert the row exists once in `combined_check_tasks` with the same ID/path and no longer appears in standalone comparison results. Migration must check `PRAGMA table_info` so clean installations do not depend on legacy columns.

- [ ] **Step 6: Run API tests and verify GREEN**

Run: `.venv/bin/python -m pytest tests/test_combined_checks.py tests/test_api.py -q`

Expected: PASS.

- [ ] **Step 7: Commit the independent API**

```bash
git add contract_review/db.py contract_review/api.py tests/test_combined_checks.py
git commit -m "feat: add independent combined check tasks"
```

### Task 3: Process comparison and review independently

**Files:**
- Modify: `contract_review/worker.py`
- Test: `tests/test_combined_checks.py`

**Interfaces:**
- Produces: `process_combined_comparison() -> bool`, `process_combined_review() -> bool`, `sync_combined_comparison() -> bool`, `_cleanup_combined_word(task_id) -> None`.
- Consumes: `review_docx()`, `get_adapter()`, SQLite task claiming, `utc_now()`.

- [ ] **Step 1: Write the failing independence test**

```python
def test_combined_review_finishes_before_remote_comparison(client, word_bytes, pdf_bytes):
    task_id = submit_combined_fixture(client, word_bytes, pdf_bytes)
    assert process_combined_comparison()  # remote state becomes processing
    assert process_combined_review()      # review runs without sync completion
    task = client.get("/api/combined-checks").json()["items"][0]
    assert task["id"] == task_id
    assert task["comparison_status"] == "processing"
    assert task["review_status"] == "passed"
```

Add a second test proving a `not_applicable` comparison row completes review and displays no PDF, plus cleanup assertions for Word/PDF paths.

- [ ] **Step 2: Run worker tests and verify RED**

Run: `.venv/bin/python -m pytest tests/test_combined_checks.py -k 'finishes_before or review_only' -q`

Expected: FAIL because the combined Worker functions do not exist.

- [ ] **Step 3: Implement separate claim/process functions**

`process_combined_comparison()` selects only `comparison_status='queued'`; `process_combined_review()` selects only `review_status='queued'` and has no comparison predicate. Both use guarded status updates before doing work.

- [ ] **Step 4: Implement safe shared-file cleanup**

After review finishes, retain Word while a paired comparison has not yet been submitted. After remote submission, retain Word only while review is queued/processing. `_cleanup_combined_word()` re-reads the row and deletes/clears the path only when both consumers have finished reading it.

- [ ] **Step 5: Add combined functions to `run_once()` and recovery**

Order one pass as comparison submission, combined review, existing amount review, existing comparison sync, combined comparison sync. Reset interrupted `review_status='processing'` to `queued` and comparison `submitting` without a remote ID to `queued`.

- [ ] **Step 6: Run worker tests and verify GREEN**

Run: `.venv/bin/python -m pytest tests/test_combined_checks.py tests/test_api.py -q`

Expected: PASS.

- [ ] **Step 7: Commit Worker behavior**

```bash
git add contract_review/worker.py tests/test_combined_checks.py
git commit -m "feat: process combined comparison and review independently"
```

### Task 4: Build the isolated combined frontend and dual pickers

**Files:**
- Modify: `contract_review/static/contract-check.html`
- Create: `contract_review/static/contract-check.js`
- Create: `contract_review/static/contract-check-results.html`
- Create: `contract_review/static/contract-check-results.js`
- Modify: `contract_review/static/styles.css`
- Modify: `contract_review/static/common.js`
- Modify: `contract_review/static/index.html`
- Modify: `contract_review/static/amount-review.html`
- Modify: `contract_review/static/amount-review-results.html`
- Create: `tests/test_combined_frontend.py`
- Modify: `tests/test_api.py`

**Interfaces:**
- Consumes: `/api/comparisons/pair-preview` for name pairing and `/api/combined-checks` for submission/results.
- Produces: explicit `#word-files-button`, `#word-folder-button`, `#pdf-files-button`, `#pdf-folder-button`; candidate checkboxes default selected.

- [ ] **Step 1: Write failing frontend behavior tests**

Use a Node VM DOM harness to call the combined page selection functions with file objects. Assert that directory files are all selected initially, one can be unchecked and reselected, and paired plus unmatched Word indexes are submitted. Parse rendered HTML to assert both file and folder controls exist for Word and PDF and that standalone pages still reference their original scripts/result pages.

- [ ] **Step 2: Run frontend tests and verify RED**

Run: `.venv/bin/python -m pytest tests/test_combined_frontend.py tests/test_api.py::test_work_pages_offer_three_workflow_navigation -q`

Expected: FAIL because the combined page shares `comparison.js`, has no independent result page, and the explicit dual controls are absent.

- [ ] **Step 3: Implement explicit independent picker controls**

Remove whole-zone click as the only file entry. Use visible labels bound to normal `multiple` inputs and `multiple webkitdirectory directory` inputs. Stop propagation on each control, render one shared checkbox list per type, and default all accepted files to selected.

- [ ] **Step 4: Submit paired and unmatched rows**

After pair preview, render matched rows and unmatched Word rows labeled “仅审核”. Submit `pairs_json` and literal unmatched Word indexes to `/api/combined-checks`; allow submit whenever at least one Word is selected.

- [ ] **Step 5: Implement combined current-task and history result tables**

Show empty PDF/comparison cells as “—”. Poll while either status is nonterminal. Show comparison preview only for completed paired tasks. Render passed/failed review states and expandable error reasons.

- [ ] **Step 6: Restore standalone result pages and update three-link navigation**

`comparison-results.html/js` query only `/api/comparisons`; `contract-check-results.html/js` query only `/api/combined-checks`. All work pages contain the three navigation links in the same order.

- [ ] **Step 7: Run frontend tests and verify GREEN**

Run: `.venv/bin/python -m pytest tests/test_combined_frontend.py tests/test_api.py -q`

Expected: PASS.

- [ ] **Step 8: Commit frontend isolation**

```bash
git add contract_review/static tests/test_combined_frontend.py tests/test_api.py
git commit -m "feat: add isolated combined check interface"
```

### Task 5: Migrate live queued tasks and verify the complete system

**Files:**
- Modify: `README.md`
- Verify: `data/contract_review.sqlite3`

**Interfaces:**
- Consumes: `init_db()` migration and both `contract-review-web` / `contract-review-worker` commands.
- Produces: current queued combined tasks visible through `/api/combined-checks` and processing under the Worker.

- [ ] **Step 1: Run the full automated suite**

Run: `node --check contract_review/static/contract-check.js && node --check contract_review/static/contract-check-results.js && python3 -m compileall -q contract_review && .venv/bin/python -m pytest -q`

Expected: all tests pass.

- [ ] **Step 2: Stop the manually launched Web-only process**

Resolve the exact PID listening on `127.0.0.1:8000`, stop only that process, and verify the port is free before relaunching. Do not use broad process-name termination.

- [ ] **Step 3: Start Web and Worker together**

Start `.venv/bin/uvicorn contract_review.main:app --host 127.0.0.1 --port 8000` and `.venv/bin/python -m contract_review.worker` as separate sessions. Confirm both processes remain alive.

- [ ] **Step 4: Verify migration and processing state**

Call `/api/combined-checks` and confirm the two task IDs formerly queued in `comparison_submissions` appear in the combined endpoint. Poll until review reaches a terminal state and comparison leaves `queued`; report any real adapter error without hiding it.

- [ ] **Step 5: Browser-verify the requested UI**

Open `contract-check.html`, confirm three top entries, separately open the file and folder choosers, import controlled test files, uncheck/recheck candidates, and inspect paired/unmatched rows plus independent status columns. Check browser console errors.

- [ ] **Step 6: Update operating instructions and run final checks**

Document that local operation requires both Web and Worker. Run `git diff --check`, the full test suite, and `git status --short`. Do not modify unrelated `.worktrees/` content.

- [ ] **Step 7: Commit documentation and final adjustments**

```bash
git add README.md
git commit -m "docs: explain combined workflow runtime"
```
