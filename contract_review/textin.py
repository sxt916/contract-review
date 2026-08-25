from __future__ import annotations

import base64
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx


@dataclass
class CreatedComparison:
    task_id: str
    preview_url: str


@dataclass
class ComparisonStatus:
    state: str
    similarity: float | None = None
    difference_count: int | None = None
    error_message: str | None = None


class TextInError(RuntimeError):
    pass


DEFAULT_IGNORE_OPTIONS = {
    "comments": False,
    "headerfooter": True,
    "stamp": False,
    "symbols": True,
    "watermark": False,
}


ERROR_MESSAGES = {
    40003: "TextIn 余额不足",
    40004: "TextIn 请求参数错误",
    40007: "TextIn 机器人不存在或未发布",
    40008: "尚未开通 TextIn 合同比对服务",
    40101: "TextIn 凭证缺失",
    40102: "TextIn 凭证无效",
    40103: "服务器 IP 不在 TextIn 白名单",
    40104: "TextIn 应用已过期",
    40106: "TextIn 应用与合同比对服务不匹配",
    40107: "TextIn 服务额度已用完",
    40109: "TextIn 请求频率超限",
    40202: "TextIn 应用不支持合同比对服务",
    40203: "TextIn API 配置不正确",
    10114: "合同文件已加密，TextIn 无法解析",
    10115: "合同文件损坏，TextIn 无法解析",
}


class MockTextInAdapter:
    def create(self, word_path: Path, pdf_path: Path, word_filename: str | None = None, pdf_filename: str | None = None, ignore_options: dict[str, bool] | None = None) -> CreatedComparison:
        task_id = f"mock-{uuid.uuid4()}"
        return CreatedComparison(task_id, f"/api/mock-preview/{task_id}")

    def status(self, task_id: str) -> ComparisonStatus:
        return ComparisonStatus("completed", 1.0, 0)

    def delete(self, task_id: str) -> None:
        return None

    def preview(self, task_id: str, preview_url: str, base_url: str) -> str:
        return f"{base_url.rstrip('/')}/api/mock-preview/{task_id}"

    def health(self) -> tuple[bool, str]:
        return True, "模拟适配器正常"


class TextInAdapter:
    def __init__(self) -> None:
        from .config import get_settings

        settings = get_settings()
        if not settings.textin_app_id or not settings.textin_secret_code:
            raise TextInError("TextIn 凭证未配置")
        self.base_url = settings.textin_api_base.rstrip("/")
        self.headers = {
            "x-ti-app-id": settings.textin_app_id,
            "x-ti-secret-code": settings.textin_secret_code,
            "Content-Type": "application/json",
        }
        self.client = httpx.Client(timeout=httpx.Timeout(90, connect=10), headers=self.headers)

    def _request(self, method: str, path: str, **kwargs) -> dict:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = self.client.request(method, f"{self.base_url}{path}", **kwargs)
                try:
                    payload = response.json()
                except ValueError:
                    response.raise_for_status()
                    raise TextInError("TextIn 服务返回了无法解析的响应")
                code = payload.get("code")
                if code != 200:
                    message = ERROR_MESSAGES.get(code)
                    detail = str(payload.get("msg") or "").strip()
                    if not message:
                        message = f"TextIn 请求失败（错误码 {code}）"
                        if detail:
                            message = f"{message}：{detail}"
                    # TextIn returns 10306 while a previous comparison is still
                    # being admitted. No task id is returned, so retrying the
                    # create request after a short delay is safe and expected.
                    if code == 10306 and detail == "比对任务处理中" and attempt < 2:
                        time.sleep(2 * (attempt + 1))
                        continue
                    raise TextInError(message)
                response.raise_for_status()
                return payload
            except TextInError:
                raise
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.4 * (attempt + 1))
            except (httpx.HTTPStatusError, ValueError) as exc:
                raise TextInError("TextIn 服务响应异常") from exc
        raise TextInError("无法连接 TextIn 服务") from last_error

    @staticmethod
    def _encoded_file(path: Path, filename: str) -> dict[str, str]:
        return {"filedata": base64.b64encode(path.read_bytes()).decode("ascii"), "filename": filename}

    def create(self, word_path: Path, pdf_path: Path, word_filename: str | None = None, pdf_filename: str | None = None, ignore_options: dict[str, bool] | None = None) -> CreatedComparison:
        options = {**DEFAULT_IGNORE_OPTIONS, **(ignore_options or {})}
        payload = {
            "creator": "合同对比与金额审核系统",
            # This account uses TextIn's token-auth integration. Although this
            # field is omitted from the create endpoint table, the access guide
            # and the service itself require it for this authentication mode.
            "token_mode": 1,
            "convert_arg": {
                "remove_stamp": int(options["stamp"]),
                "remove_comments": int(options["comments"]),
                "remove_headerfooter": int(options["headerfooter"]),
                "remove_symbol": int(options["symbols"]),
                "merge_diff": 1,
            },
            "config": {"use_pdf_parser": "false", "remove_watermark": str(options["watermark"]).lower()},
            "standard_doc": [self._encoded_file(word_path, word_filename or word_path.name)],
            "compare_doc": [self._encoded_file(pdf_path, pdf_filename or pdf_path.name)],
        }
        data = self._request("POST", "/api/contracts/v3/comparison/external/create", json=payload).get("data") or {}
        if not data.get("task_id") or not data.get("preview_url"):
            raise TextInError("TextIn 创建任务响应缺少任务编号或预览地址")
        return CreatedComparison(str(data["task_id"]), str(data["preview_url"]))

    def status(self, task_id: str) -> ComparisonStatus:
        data = self._request("GET", "/api/contracts/v3/comparison/external/diff_info", params={"task_id": task_id}).get("data") or {}
        remote_status = data.get("status")
        if remote_status == 2:
            return ComparisonStatus("processing")
        if remote_status == -1:
            return ComparisonStatus("failed", error_message="TextIn 解析或比对失败")
        if remote_status != 1:
            return ComparisonStatus("processing")
        results = data.get("result") or []
        result = results[0] if results else {}
        diff_info = result.get("diff_info") or {}
        return ComparisonStatus("completed", float(result.get("similarity", 0)), int(diff_info.get("total", 0)))

    def delete(self, task_id: str) -> None:
        self._request("POST", "/api/contracts/v3/comparison/external/delete", json={"task_id": task_id})

    def preview(self, task_id: str, preview_url: str, base_url: str) -> str:
        data = self._request("POST", "/api/contracts/v3/comparison/external/token/apply", json={"task_ids": [task_id]}).get("data") or {}
        token = data.get("token")
        if not token:
            raise TextInError("TextIn 未返回预览令牌")
        parts = urlsplit(preview_url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query["token"] = token
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

    def health(self) -> tuple[bool, str]:
        self._request("GET", "/api/contracts/v3/comparison/external/list", params={"page": 1, "pagesize": 10, "by_app_id": "true"})
        return True, "TextIn 凭证有效且服务可访问"


def get_adapter():
    from .config import get_settings

    return MockTextInAdapter() if get_settings().comparison_adapter == "mock" else TextInAdapter()
