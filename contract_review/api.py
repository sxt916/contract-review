from __future__ import annotations

import json
import os
import re
import shutil
import uuid
import zipfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from pypdf import PdfReader

from .config import get_settings
from .db import connection, decode_task, utc_now
from .pairing import pair_files
from .textin import DEFAULT_IGNORE_OPTIONS, get_adapter

router = APIRouter(prefix="/api")


class PairPreviewRequest(BaseModel):
    word_names: list[str]
    pdf_names: list[str]


def _safe_name(name: str | None) -> str:
    value = Path(name or "upload").name
    return re.sub(r"[^\w\-.()（）\u4e00-\u9fff]", "_", value)[:180]


async def _store_upload(upload: UploadFile, allowed_suffix: str) -> tuple[Path, str]:
    settings = get_settings()
    filename = _safe_name(upload.filename)
    suffix = Path(filename).suffix.lower()
    if suffix == ".doc":
        raise HTTPException(400, "不支持 .doc，请另存为 .docx 后重新上传")
    if suffix != allowed_suffix:
        raise HTTPException(400, f"文件 {filename} 格式错误，仅支持 {allowed_suffix}")
    target = settings.upload_dir / f"{uuid.uuid4().hex}{suffix}"
    size = 0
    try:
        with target.open("wb") as output:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_file_size_mb * 1024 * 1024:
                    raise HTTPException(413, f"文件 {filename} 超过 {settings.max_file_size_mb} MB")
                output.write(chunk)
        if suffix == ".docx":
            if not zipfile.is_zipfile(target):
                raise HTTPException(400, f"文件 {filename} 不是有效的 DOCX")
            with zipfile.ZipFile(target) as archive:
                if "word/document.xml" not in archive.namelist():
                    raise HTTPException(400, f"文件 {filename} 缺少 Word 文档内容")
        else:
            with target.open("rb") as stream:
                if stream.read(5) != b"%PDF-":
                    raise HTTPException(400, f"文件 {filename} 不是有效的 PDF")
            try:
                if len(PdfReader(str(target)).pages) > settings.max_page_count:
                    raise HTTPException(400, f"文件 {filename} 超过 {settings.max_page_count} 页")
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(400, f"PDF {filename} 已损坏或无法读取") from exc
        return target, filename
    except Exception:
        target.unlink(missing_ok=True)
        raise


@router.get("/health")
def health():
    with connection() as conn:
        conn.execute("SELECT 1").fetchone()
    return {"status": "ok"}


@router.get("/config/public")
def public_config():
    settings = get_settings()
    return {"max_files_per_type": settings.max_files_per_type, "max_file_size_mb": settings.max_file_size_mb, "max_page_count": settings.max_page_count, "comparison_mode": settings.comparison_adapter}


@router.get("/textin/status")
def textin_status():
    settings = get_settings()
    configured = bool(settings.textin_app_id and settings.textin_secret_code)
    if not configured and settings.comparison_adapter != "mock":
        return {"configured": False, "mode": settings.comparison_adapter, "connected": False, "message": "凭证未配置"}
    try:
        connected, message = get_adapter().health()
        return {"configured": configured, "mode": settings.comparison_adapter, "connected": connected, "message": message}
    except Exception as exc:
        return {"configured": configured, "mode": settings.comparison_adapter, "connected": False, "message": str(exc)}


@router.post("/amount-reviews", status_code=202)
async def create_amount_reviews(files: list[UploadFile] = File(...)):
    settings = get_settings()
    if not files or len(files) > settings.max_files_per_type:
        raise HTTPException(400, f"一次须上传 1 至 {settings.max_files_per_type} 份文件")
    stored: list[tuple[Path, str]] = []
    try:
        for upload in files:
            stored.append(await _store_upload(upload, ".docx"))
        tasks = []
        with connection() as conn:
            for path, filename in stored:
                task_id = str(uuid.uuid4())
                conn.execute("INSERT INTO amount_review_tasks (id, filename, status, created_at, temp_path) VALUES (?, ?, 'queued', ?, ?)", (task_id, filename, utc_now(), str(path)))
                tasks.append({"id": task_id, "filename": filename, "status": "queued"})
        return {"tasks": tasks}
    except Exception:
        for path, _ in stored:
            path.unlink(missing_ok=True)
        raise


@router.get("/amount-reviews")
def list_amount_reviews(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    offset = (page - 1) * page_size
    with connection() as conn:
        total = conn.execute("SELECT COUNT(*) FROM amount_review_tasks").fetchone()[0]
        rows = conn.execute("SELECT * FROM amount_review_tasks ORDER BY created_at DESC LIMIT ? OFFSET ?", (page_size, offset)).fetchall()
    return {"items": [decode_task(row) for row in rows], "total": total, "page": page, "page_size": page_size}


@router.get("/amount-reviews/{task_id}")
def get_amount_review(task_id: str):
    with connection() as conn:
        row = conn.execute("SELECT * FROM amount_review_tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        raise HTTPException(404, "审核任务不存在")
    return decode_task(row)


@router.delete("/amount-reviews/{task_id}", status_code=204)
def delete_amount_review(task_id: str):
    with connection() as conn:
        row = conn.execute("SELECT temp_path FROM amount_review_tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(404, "审核任务不存在")
        conn.execute("DELETE FROM amount_review_tasks WHERE id = ?", (task_id,))
    if row["temp_path"]:
        Path(row["temp_path"]).unlink(missing_ok=True)


@router.post("/comparisons/pair-preview")
def comparison_pair_preview(payload: PairPreviewRequest):
    settings = get_settings()
    if len(payload.word_names) > settings.max_files_per_type or len(payload.pdf_names) > settings.max_files_per_type:
        raise HTTPException(400, f"每类文件最多 {settings.max_files_per_type} 份")
    return pair_files(payload.word_names, payload.pdf_names)


@router.post("/comparisons", status_code=202)
async def create_comparisons(
    word_files: list[UploadFile] = File(...),
    pdf_files: list[UploadFile] = File(...),
    pairs_json: str = Form(...),
    ignore_options_json: str = Form("{}"),
):
    settings = get_settings()
    if len(word_files) > settings.max_files_per_type or len(pdf_files) > settings.max_files_per_type:
        raise HTTPException(400, f"每类文件最多 {settings.max_files_per_type} 份")
    try:
        pairs = json.loads(pairs_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "配对数据格式错误") from exc
    if not isinstance(pairs, list) or not pairs:
        raise HTTPException(400, "没有可提交的配对")
    try:
        supplied_options = json.loads(ignore_options_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "忽略选项格式错误") from exc
    if not isinstance(supplied_options, dict) or any(key not in DEFAULT_IGNORE_OPTIONS or not isinstance(value, bool) for key, value in supplied_options.items()):
        raise HTTPException(400, "忽略选项无效")
    ignore_options = {**DEFAULT_IGNORE_OPTIONS, **supplied_options}
    seen_w: set[int] = set(); seen_p: set[int] = set()
    for pair in pairs:
        wi, pi = pair.get("word_index"), pair.get("pdf_index")
        if not isinstance(wi, int) or not isinstance(pi, int) or wi < 0 or pi < 0 or wi >= len(word_files) or pi >= len(pdf_files) or wi in seen_w or pi in seen_p:
            raise HTTPException(400, "配对索引无效或文件被重复使用")
        seen_w.add(wi); seen_p.add(pi)
    stored_words: list[tuple[Path, str]] = []; stored_pdfs: list[tuple[Path, str]] = []
    try:
        for upload in word_files:
            stored_words.append(await _store_upload(upload, ".docx"))
        for upload in pdf_files:
            stored_pdfs.append(await _store_upload(upload, ".pdf"))
        task_ids: list[str] = []
        paired_paths: set[Path] = set()
        with connection() as conn:
            for pair in pairs:
                word_path, word_name = stored_words[pair["word_index"]]
                pdf_path, pdf_name = stored_pdfs[pair["pdf_index"]]
                paired_paths.update((word_path, pdf_path))
                task_id = str(uuid.uuid4())
                conn.execute("""INSERT INTO comparison_submissions
                    (id, contract_no, word_filename, pdf_filename, status, created_at, word_temp_path, pdf_temp_path, ignore_options_json)
                    VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?)""",
                    (task_id, pair.get("contract_no"), word_name, pdf_name, utc_now(), str(word_path), str(pdf_path), json.dumps(ignore_options)))
                task_ids.append(task_id)
        for path, _ in [*stored_words, *stored_pdfs]:
            if path not in paired_paths:
                path.unlink(missing_ok=True)
        return {"task_ids": task_ids}
    except Exception:
        for path, _ in [*stored_words, *stored_pdfs]:
            path.unlink(missing_ok=True)
        raise


@router.get("/comparisons")
def list_comparisons(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    filename: str | None = Query(None, max_length=180),
    status: str | None = Query(None, max_length=30),
    date_from: str | None = Query(None, max_length=30),
    date_to: str | None = Query(None, max_length=30),
    task_ids: str | None = Query(None, max_length=4000),
):
    offset = (page - 1) * page_size
    conditions: list[str] = []
    params: list[object] = []
    if filename:
        conditions.append("(word_filename LIKE ? OR pdf_filename LIKE ?)")
        keyword = f"%{filename.strip()}%"
        params.extend((keyword, keyword))
    if status:
        allowed_statuses = {"queued", "submitting", "processing", "completed", "failed"}
        if status not in allowed_statuses:
            raise HTTPException(400, "处理进度筛选值无效")
        conditions.append("status = ?")
        params.append(status)
    if date_from:
        conditions.append("created_at >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("created_at < datetime(?, '+1 day')")
        params.append(date_to)
    if task_ids is not None:
        ids = [value for value in task_ids.split(",") if value]
        if not ids:
            return {"items": [], "total": 0, "page": page, "page_size": page_size}
        if len(ids) > 100 or any(not re.fullmatch(r"[0-9a-fA-F-]{36}", value) for value in ids):
            raise HTTPException(400, "任务 ID 筛选值无效")
        conditions.append(f"id IN ({','.join('?' for _ in ids)})")
        params.extend(ids)
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    with connection() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM comparison_submissions{where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM comparison_submissions{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            [*params, page_size, offset],
        ).fetchall()
    return {"items": [dict(row) for row in rows], "total": total, "page": page, "page_size": page_size}


@router.get("/comparisons/{task_id}/preview")
def comparison_preview(task_id: str, request: Request):
    with connection() as conn:
        row = conn.execute("SELECT textin_task_id, preview_url, status FROM comparison_submissions WHERE id = ?", (task_id,)).fetchone()
    if not row:
        raise HTTPException(404, "对比任务不存在")
    if not row["textin_task_id"]:
        raise HTTPException(409, "任务尚未生成预览")
    return {"preview_url": get_adapter().preview(row["textin_task_id"], row["preview_url"], str(request.base_url))}


@router.delete("/comparisons/{task_id}", status_code=204)
def delete_comparison(task_id: str):
    with connection() as conn:
        row = conn.execute("SELECT * FROM comparison_submissions WHERE id = ?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(404, "对比任务不存在")
        if row["textin_task_id"]:
            get_adapter().delete(row["textin_task_id"])
        conn.execute("DELETE FROM comparison_submissions WHERE id = ?", (task_id,))
    for field in ("word_temp_path", "pdf_temp_path"):
        if row[field]:
            Path(row[field]).unlink(missing_ok=True)


@router.get("/mock-preview/{remote_id}", response_class=HTMLResponse)
def mock_preview(remote_id: str):
    return f"""<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><style>body{{font-family:sans-serif;padding:36px;color:#183153}}.box{{border:1px solid #cbdaf5;padding:28px;border-radius:12px;background:#f6f9ff}}</style></head><body><div class='box'><h2>TextIn 模拟预览</h2><p>任务 {remote_id}</p><p>当前使用开发模拟适配器。配置真实凭证并完成接口联调后，此处将嵌入 TextIn 差异预览。</p></div></body></html>"""
