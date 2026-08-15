"""CloudBase 云存储（COS）HTTP 客户端，与 cloudbase_nosql 一样复用环境 API Key。

封装「现场管控单」所需的三个操作：
- get_upload_info / upload_bytes：获取上传信息并直传对象到 COS
- get_download_url：批量获取对象下载链接
- delete_object：删除对象

端点：https://{env_id}.api.tcloudbasegateway.com/v1/storages/...
鉴权：Authorization: Bearer <CLOUDBASE_API_KEY>
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .cloudbase_nosql import CloudBaseAPIError, CloudBaseConfigError


class CloudBaseStorageClient:
    def __init__(self, env_id: str | None = None, api_key: str | None = None, timeout: float = 30.0):
        self.env_id = env_id or os.getenv("CLOUDBASE_ENV_ID", "")
        self.api_key = api_key or os.getenv("CLOUDBASE_API_KEY", "")
        self.timeout = timeout
        if not self.env_id:
            raise CloudBaseConfigError("CLOUDBASE_ENV_ID is not configured")
        if not self.api_key or self.api_key.startswith("replace-"):
            raise CloudBaseConfigError("CLOUDBASE_API_KEY is not configured")
        self.base_url = f"https://{self.env_id}.api.tcloudbasegateway.com/v1/storages"

    def _request(self, path: str, body: list[dict[str, Any]]) -> Any:
        url = f"{self.base_url}/{path}"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = Request(
            url,
            data=data,
            method="POST",
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
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                details = json.loads(raw)
            except json.JSONDecodeError:
                details = raw
            message = details.get("message", "CloudBase storage request failed") if isinstance(details, dict) else "CloudBase storage request failed"
            raise CloudBaseAPIError(exc.code, message, details) from exc
        except URLError as exc:
            raise CloudBaseAPIError(502, f"CloudBase storage unreachable: {exc.reason}") from exc
        if not payload:
            return None
        return json.loads(payload.decode("utf-8"))

    def get_upload_info(self, object_id: str) -> dict[str, Any]:
        """获取对象上传信息，返回含 uploadUrl/authorization/token/cloudObjectMeta/cloudObjectId 的 dict。"""
        result = self._request("get-objects-upload-info", [{"objectId": object_id}])
        item = (result or [])[0] if isinstance(result, list) else None
        if not isinstance(item, dict):
            raise CloudBaseAPIError(502, "上传信息返回异常")
        if item.get("code"):
            raise CloudBaseAPIError(502, str(item.get("message", item.get("code"))), item)
        return item

    def upload_bytes(self, object_id: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        """上传字节到云存储，返回 cloudObjectId。"""
        info = self.get_upload_info(object_id)
        upload_url = info.get("uploadUrl")
        if not upload_url:
            raise CloudBaseAPIError(502, "上传信息缺少 uploadUrl")
        headers = {
            "Authorization": info.get("authorization", ""),
            "X-Cos-Security-Token": info.get("token", ""),
            "X-Cos-Meta-Fileid": info.get("cloudObjectMeta", ""),
            "Content-Type": content_type,
        }
        req = Request(upload_url, data=data, method="PUT", headers=headers)
        try:
            with urlopen(req, timeout=self.timeout) as response:
                response.read()
        except HTTPError as exc:
            raise CloudBaseAPIError(exc.code, f"上传到 COS 失败: {exc.reason}") from exc
        except URLError as exc:
            raise CloudBaseAPIError(502, f"COS 上传不可达: {exc.reason}") from exc
        cloud_id = info.get("cloudObjectId")
        if not cloud_id:
            raise CloudBaseAPIError(502, "上传信息缺少 cloudObjectId")
        return str(cloud_id)

    def get_download_url(self, cloud_object_id: str) -> str:
        result = self._request("get-objects-download-info", [{"cloudObjectId": cloud_object_id}])
        item = (result or [])[0] if isinstance(result, list) else None
        if not isinstance(item, dict):
            raise CloudBaseAPIError(502, "下载信息返回异常")
        if item.get("code"):
            raise CloudBaseAPIError(502, str(item.get("message", item.get("code"))), item)
        url = item.get("downloadUrl") or item.get("downloadUrlEncoded")
        if not url:
            raise CloudBaseAPIError(502, "下载信息缺少 downloadUrl")
        return str(url)

    def delete_object(self, cloud_object_id: str) -> None:
        result = self._request("delete-objects", [{"cloudObjectId": cloud_object_id}])
        item = (result or [])[0] if isinstance(result, list) else None
        if isinstance(item, dict) and item.get("code"):
            raise CloudBaseAPIError(502, str(item.get("message", item.get("code"))), item)
