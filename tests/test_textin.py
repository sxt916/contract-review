from pathlib import Path

import httpx

from contract_review.textin import TextInAdapter


def test_create_uses_documented_v3_payload(tmp_path, monkeypatch):
    word = tmp_path / "标准合同.docx"
    pdf = tmp_path / "扫描合同.pdf"
    word.write_bytes(b"word")
    pdf.write_bytes(b"pdf")
    captured = {}

    adapter = object.__new__(TextInAdapter)

    def fake_request(method, path, **kwargs):
        captured.update(method=method, path=path, **kwargs)
        return {"code": 200, "data": {"task_id": "task-1", "preview_url": "https://example.test/preview"}}

    monkeypatch.setattr(adapter, "_request", fake_request)
    created = adapter.create(word, pdf)

    assert created.task_id == "task-1"
    assert captured["path"] == "/api/contracts/v3/comparison/external/create"
    assert captured["json"]["token_mode"] == 1
    assert captured["json"]["convert_arg"] == {
        "remove_stamp": 0,
        "remove_comments": 0,
        "remove_headerfooter": 1,
        "remove_symbol": 1,
        "merge_diff": 1,
    }
    assert captured["json"]["config"] == {
        "use_pdf_parser": "false",
        "remove_watermark": "false",
    }
    assert captured["json"]["standard_doc"][0]["filename"] == word.name
    assert captured["json"]["compare_doc"][0]["filename"] == pdf.name

    adapter.create(word, pdf, ignore_options={"comments": True, "headerfooter": False, "stamp": True, "symbols": False, "watermark": False})
    assert captured["json"]["convert_arg"]["remove_comments"] == 1
    assert captured["json"]["convert_arg"]["remove_headerfooter"] == 0
    assert captured["json"]["convert_arg"]["remove_stamp"] == 1
    assert captured["json"]["convert_arg"]["remove_symbol"] == 0
    assert captured["json"]["config"]["remove_watermark"] == "false"


def test_request_retries_textin_busy_response(monkeypatch):
    responses = [
        {"code": 10306, "msg": "比对任务处理中"},
        {"code": 10306, "msg": "比对任务处理中"},
        {"code": 200, "data": {"task_id": "task-1"}},
    ]

    class FakeClient:
        def request(self, method, url, **kwargs):
            request = httpx.Request(method, url)
            return httpx.Response(200, request=request, json=responses.pop(0))

    adapter = object.__new__(TextInAdapter)
    adapter.base_url = "https://example.test"
    adapter.client = FakeClient()
    sleeps = []
    monkeypatch.setattr("contract_review.textin.time.sleep", sleeps.append)

    result = adapter._request("POST", "/create", json={})

    assert result["code"] == 200
    assert sleeps == [2, 4]
