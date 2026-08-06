"""供客户端轻量轮询使用的无状态变更检测模块。"""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable

from .cloudbase_nosql import CloudBaseNoSQLClient


PAGE_SIZE = 100
Document = dict[str, object]


class PollingPayloadError(RuntimeError):
    """CloudBase 列表响应无法安全解释时抛出。"""


def _document(item: object, index: int) -> Document:
    """校验并收窄单个文档的类型。"""
    if not isinstance(item, dict):
        raise PollingPayloadError(
            f"CloudBase 文档列表第 {index} 项不是对象"
        )
    if not all(isinstance(key, str) for key in item):
        raise PollingPayloadError(
            f"CloudBase 文档列表第 {index} 项包含非字符串键"
        )
    # 重新构造后，类型检查器可以确认所有键都是 str。
    return {key: value for key, value in item.items() if isinstance(key, str)}


def _document_list(payload: list[object]) -> list[Document]:
    """校验列表中的每一项都是文档，避免静默丢弃异常数据。"""
    return [_document(item, index) for index, item in enumerate(payload)]


def _documents(payload: object) -> list[Document]:
    """兼容 CloudBase HTTP API 不同版本使用过的列表响应结构。"""
    if isinstance(payload, list):
        return _document_list(payload)
    if not isinstance(payload, dict):
        raise PollingPayloadError(
            f"CloudBase 文档列表响应类型异常：{type(payload).__name__}"
        )

    for key in ("list", "documents", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return _document_list(value)

    data = payload.get("data")
    if data is not None and data is not payload:
        return _documents(data)
    raise PollingPayloadError("CloudBase 文档列表响应缺少可识别的数据字段")


def _all_documents(
    client: CloudBaseNoSQLClient, collection: str
) -> list[Document]:
    """稳定分页读取一个集合的全部文档。"""
    documents: list[Document] = []
    offset = 0
    while True:
        page = _documents(
            client.list_documents(
                collection,
                offset=offset,
                limit=PAGE_SIZE,
                order=[{"field": "_id", "direction": "asc"}],
            )
        )
        documents.extend(page)
        if len(page) < PAGE_SIZE:
            break
        offset += len(page)
    return sorted(
        documents,
        key=lambda item: json.dumps(
            item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
    )


def calculate_revision(
    client: CloudBaseNoSQLClient, collections: Iterable[str]
) -> str:
    """对完整当前状态计算哈希，因此更新和删除都能被检测到。

    这里有意用更多只读查询换取正确性。算法不依赖进程内存，所以云函数冷启动、
    实例替换或缩容到零都不会丢失通知。
    """
    collection_names = sorted(collections)
    state: dict[str, list[Document]] = {}
    if collection_names:
        with ThreadPoolExecutor(max_workers=min(4, len(collection_names))) as executor:
            futures = {
                collection: executor.submit(_all_documents, client, collection)
                for collection in collection_names
            }
            # 按集合名顺序取结果，保证规范化输入稳定。任意一路读取失败都会向上
            # 抛出，绝不把不完整快照误报成有效状态。
            state = {
                collection: futures[collection].result()
                for collection in collection_names
            }
    canonical = json.dumps(
        state,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
