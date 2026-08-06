"""Django API 使用的轻量 CloudBase NoSQL HTTP 客户端。

CloudBase 文档数据库不是 Django ORM 后端。把 HTTP 调用集中封装在这一层，
可以避免视图直接依赖 CloudBase 的请求格式，同时保留 Django 自带 SQLite，
供本地会话、认证和迁移等框架功能使用。
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


class CloudBaseConfigError(RuntimeError):
    pass


class CloudBaseAPIError(RuntimeError):
    def __init__(self, status: int, message: str, details: Any = None):
        super().__init__(message)
        self.status = status
        self.details = details


def decode_ejson(value: Any) -> Any:
    """把 CloudBase 返回的常见 Strict EJSON 包装转换成普通 JSON 值。"""
    if isinstance(value, list):
        return [decode_ejson(item) for item in value]
    if not isinstance(value, dict):
        return value

    if set(value) == {"$oid"}:
        return value["$oid"]
    if set(value) == {"$numberInt"} or set(value) == {"$numberLong"}:
        return int(next(iter(value.values())))
    if set(value) in ({"$numberDouble"}, {"$numberDecimal"}):
        return float(next(iter(value.values())))
    if set(value) == {"$date"}:
        raw = decode_ejson(value["$date"])
        return raw

    return {key: decode_ejson(item) for key, item in value.items()}


class CloudBaseNoSQLClient:
    def __init__(
        self,
        env_id: str | None = None,
        api_key: str | None = None,
        instance: str | None = None,
        database: str | None = None,
        timeout: float = 10.0,
    ):
        self.env_id = env_id or os.getenv("CLOUDBASE_ENV_ID", "")
        self.api_key = api_key or os.getenv("CLOUDBASE_API_KEY", "")
        self.instance = instance or os.getenv(
            "CLOUDBASE_NOSQL_INSTANCE", "(default)"
        )
        self.database = database or os.getenv(
            "CLOUDBASE_NOSQL_DATABASE", "(default)"
        )
        self.timeout = timeout

        if not self.env_id:
            raise CloudBaseConfigError("CLOUDBASE_ENV_ID is not configured")
        if not self.api_key or self.api_key.startswith("replace-"):
            raise CloudBaseConfigError("CLOUDBASE_API_KEY is not configured")

        # API Key 只存在后端环境变量中。前端永远不应直接访问这个网关地址。
        self.base_url = (
            f"https://{self.env_id}.api.tcloudbasegateway.com/v1/database/"
            f"instances/{quote(self.instance, safe='()')}/"
            f"databases/{quote(self.database, safe='()')}"
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        if query:
            # CloudBase HTTP API 要求对象和数组查询参数先编码为紧凑 JSON。
            encoded = {
                key: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                if isinstance(value, (dict, list))
                else str(value).lower()
                if isinstance(value, bool)
                else value
                for key, value in query.items()
                if value is not None
            }
            url = f"{url}?{urlencode(encoded)}"

        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = response.read()
        except HTTPError as exc:
            # 尽量保留云端原始错误详情，便于无人值守时从日志定位问题。
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                details = json.loads(raw)
            except json.JSONDecodeError:
                details = raw
            message = (
                details.get("message", "CloudBase request failed")
                if isinstance(details, dict)
                else "CloudBase request failed"
            )
            raise CloudBaseAPIError(exc.code, message, details) from exc
        except URLError as exc:
            raise CloudBaseAPIError(502, f"CloudBase is unreachable: {exc.reason}") from exc

        if not payload:
            return None
        return decode_ejson(json.loads(payload.decode("utf-8")))

    @staticmethod
    def _collection_path(collection: str) -> str:
        return f"/collections/{quote(collection, safe='')}/documents"

    def list_documents(
        self,
        collection: str,
        *,
        query: dict[str, Any] | None = None,
        offset: int = 0,
        limit: int = 20,
        order: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            self._collection_path(collection),
            query={
                "query": query or {},
                "offset": offset,
                "limit": limit,
                "order": order,
            },
        )

    def insert_document(self, collection: str, document: dict[str, Any]) -> Any:
        return self._request(
            "POST", self._collection_path(collection), body={"data": [document]}
        )

    def get_document(self, collection: str, document_id: str) -> dict[str, Any]:
        path = f"{self._collection_path(collection)}/{quote(document_id, safe='')}"
        return self._request("GET", path)

    def update_document(
        self,
        collection: str,
        document_id: str,
        data: dict[str, Any],
        *,
        upsert: bool = False,
    ) -> Any:
        path = f"{self._collection_path(collection)}/{quote(document_id, safe='')}"
        return self._request(
            "PATCH",
            path,
            body={
                "data": data,
                "replaceMode": False,
                "upsert": upsert,
                "returnDoc": True,
            },
        )

    def delete_document(self, collection: str, document_id: str) -> Any:
        path = f"{self._collection_path(collection)}/{quote(document_id, safe='')}"
        return self._request("DELETE", path)
