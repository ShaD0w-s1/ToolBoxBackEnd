"""Small CloudBase NoSQL HTTP client used by the Django API.

CloudBase's document database is not a Django ORM backend. Keeping it behind
this adapter avoids coupling views to HTTP details and leaves Django's SQLite
database available for sessions and authentication.
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
    """Convert common Strict EJSON wrappers into JSON-friendly values."""
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
