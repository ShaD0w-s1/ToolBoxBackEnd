"""ToolBox 的 HTTP API 视图。

视图只处理输入校验和响应映射；CloudBase 协议细节集中在 cloudbase_nosql，
轮询修订计算集中在 polling，避免业务入口承担过多职责。
"""

import base64
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone
from uuid import uuid4

from django.http import HttpRequest, JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

from .cloudbase_nosql import (
    CloudBaseAPIError,
    CloudBaseConfigError,
    CloudBaseNoSQLClient,
)
from .cloudbase_storage import CloudBaseStorageClient
from .jsonio import CompactJsonResponse
from .polling import PollingPayloadError
from .sync_domains import (
    DOMAIN_ANNOUNCEMENT,
    DOMAIN_CART,
    DOMAIN_CONTROL,
    DOMAIN_PROJECTS,
    DOMAIN_STDLIBS,
    DOMAIN_SYNC_TEMPLATES,
    DOMAIN_TEMPLATES,
    SYNC_DOMAINS,
)
from .workcard_filter import (
    apply_material_filter,
    apply_tool_filter,
    apply_work_card_list,
    collect_apu_workcard_names,
    collect_keyword_names,
    collect_workcard_names,
)


# 本地开发可通过前缀使用独立测试集合；生产环境保持空前缀。
COLLECTION_PREFIX = os.getenv("CLOUDBASE_COLLECTION_PREFIX", "")
PROJECTS = f"{COLLECTION_PREFIX}work_projects"
TEMPLATES = f"{COLLECTION_PREFIX}aircraft_templates"
MATERIAL_TEMPLATES = f"{COLLECTION_PREFIX}aircraft_material_templates"
TOOL_CART = f"{COLLECTION_PREFIX}tool_cart"
AIRCRAFT_INFO = f"{COLLECTION_PREFIX}aircraft_info"
WORKCARD_320 = f"{COLLECTION_PREFIX}workcard_lib_320"
ANNOUNCEMENT = f"{COLLECTION_PREFIX}announcement"
# 变更日志：单计数器文档，作为 poll 的轻量 revision 来源（替代全量集合哈希）。
CHANGE_LOG = f"{COLLECTION_PREFIX}work_change_log"
REVISION_DOC_ID = "revision"
# 应用运行时配置（远端下发）：watch 实时推送开关与阈值。
APP_CONFIG = f"{COLLECTION_PREFIX}app_config"
# 现场管控单：按项目类型组织的云存储文件元数据（fileid 存这里，文件本体在云存储 COS）。
CONTROL_DOCS = f"{COLLECTION_PREFIX}control_docs"
# 登录过的账号目录（无密码身份标识）：每账号一文档，doc_id = 姓名（2-5 字符）。
ACCOUNTS = f"{COLLECTION_PREFIX}work_accounts"
# 编辑会话（字段/输入框软锁）：与账号目录共用集合，doc_id = "editing:" + session_id，doc_type="editing"，
# 避免新增集合（CloudBase NoSQL 集合需显式建表）；accounts / online-count 必须按 doc_type 过滤。
# 字段 = { doc_type, name, session_id, project_id, key, at }；TTL 由查询端懒过滤（at 距今 > 60s 视为已释放）。
EDIT_SESSION_TTL_SECONDS = 60
# 编辑会话（单输入框软锁）：与账号目录同集合，doc_id = "editing:" + session_id，doc_type="editing"，
# 避免新增集合（CloudBase NoSQL 需显式建表）；accounts/online_count 需按 doc_type 过滤避免污染目录。
# 字段 = { doc_type, name, project_id, key, at }，key 为空串视为已释放。
# 换发/APU 模板库：每模板一文档，_id 用 uuid，字段 = { id, name, savedAt, state }（state 即 GanttPrep 全量）。
ENG_TEMPLATES = f"{COLLECTION_PREFIX}eng_templates"
# 单项工作模板库（单独项目）：每模板一文档，字段 = { id, name, savedAt, state }（state 即 StandalonePrepSheet，不含 base）。
STANDALONE_TEMPLATES = f"{COLLECTION_PREFIX}standalone_templates"
AIRCRAFT_TYPES = {"A320", "B787"}

# AIRNAV 短期授权 token 有效期（秒）。
AIRNAV_TOKEN_TTL = 30 * 60
# 暴力破解限流：失败次数与冷却时间（内存态，单实例内有效，作为第一道防线）。
_AIRNAV_RATE: dict[str, dict] = {}

# 工作项目可选类型；空字符串表示历史遗留项目（仅有工具清单）。
PROJECT_TYPES = {"A检", "零散", "单独项目", "换发/APU"}

# 一级页面新增的三类标准库：键 -> (集合名, 文档ID, 行字段顺序)。
# 标准库以「整文档保存 rows 数组」的方式存储，导入即整体替换，导出即整体读取。
STANDARD_LIBRARIES = {
    "aircraft_info": {
        "collection": AIRCRAFT_INFO,
        "doc_id": "default",
        "row_keys": ["飞机号", "MSN", "FSN", "机型", "发动机", "ETOPS", "ELT-DT"],
    },
    "workcard_320": {
        "collection": WORKCARD_320,
        "doc_id": "default",
        "row_keys": ["工卡号", "工卡名", "MP项目号", "部位", "分级"],
    },
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_body(request: HttpRequest) -> dict:
    try:
        value = json.loads(request.body or b"{}")
    except json.JSONDecodeError as exc:
        raise ValueError("请求体不是有效的 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("请求体必须是 JSON 对象")
    return value


def _error(message: str, status: int, details=None) -> JsonResponse:
    payload = {"ok": False, "error": message}
    if details is not None:
        payload["details"] = details
    return CompactJsonResponse(payload, status=status)


def _handle_cloudbase_error(exc: Exception) -> JsonResponse:
    """把基础设施异常转换成稳定的 HTTP 错误响应。"""
    if isinstance(exc, CloudBaseConfigError):
        return _error(str(exc), 503)
    if isinstance(exc, CloudBaseAPIError):
        status = exc.status if 400 <= exc.status < 600 else 502
        return _error(str(exc), status, exc.details)
    raise exc


def get_nosql_client() -> CloudBaseNoSQLClient:
    return CloudBaseNoSQLClient()


def _bump_revision(client: CloudBaseNoSQLClient, domain: str) -> None:
    """递增计数器文档：全局 ``seq`` + 指定域的域计数。

    * ``seq``：全局单调递增，保留给旧客户端与 watch 推送（只需判断「有变化」）。
    * ``domains.<domain>``：供分域客户端判断「哪个域变了」，从而只重拉该域的
      端点，而不是每次变化都重拉全部 12 个端点。

    ⚠️ 域写错不会报错、只会导致该域永不触发同步（**静默漏同步**）。此处刻意
    不抛异常 —— 调用点发生在写入成功之后，抛错会把 200 变成 500 并让客户端
    误判为保存失败；改为退化成「仅全局递增」，由
    ``scripts/check_sync_domains.py`` 在提交前拦截这类错误。

    计数器文档尚不存在时自动创建。变更日志失败不影响主写入（最坏情况 poll
    检测不到该次变更，退化为手动刷新）。
    """
    increments: dict[str, int] = {"seq": 1}
    seed_domains: dict[str, int] = {}
    if domain in SYNC_DOMAINS:
        increments[f"domains.{domain}"] = 1
        seed_domains[domain] = 1
    try:
        result = client.update_document(
            CHANGE_LOG, REVISION_DOC_ID, {"$inc": increments}, upsert=False
        )
        if isinstance(result, dict) and result.get("matched") == 0:
            client.insert_document(
                CHANGE_LOG,
                {"_id": REVISION_DOC_ID, "seq": 1, "domains": seed_domains},
            )
    except (CloudBaseAPIError, CloudBaseConfigError):
        pass


def _read_revision(client: CloudBaseNoSQLClient) -> tuple[str, dict[str, str]]:
    """读取计数器文档，返回 ``(全局 seq, 各域 seq)``；文档不存在视为 0。

    各域**始终全量返回**（缺失的域补 "0"），这样客户端可以直接逐域比较，
    不必区分「域不存在」与「域为 0」两种情况。
    """
    try:
        doc = client.get_document(CHANGE_LOG, REVISION_DOC_ID)
    except CloudBaseAPIError as exc:
        if exc.status == 404:
            return "0", {name: "0" for name in SYNC_DOMAINS}
        raise
    if not isinstance(doc, dict):
        return "0", {name: "0" for name in SYNC_DOMAINS}
    seq = str(int(doc.get("seq", 0)))
    raw_domains = doc.get("domains")
    domains: dict[str, str] = {}
    for name in SYNC_DOMAINS:
        value = raw_domains.get(name, 0) if isinstance(raw_domains, dict) else 0
        try:
            domains[name] = str(int(value))
        except (TypeError, ValueError):
            domains[name] = "0"
    return seq, domains


def _normalize_list_payload(result: object) -> dict:
    """把 CloudBase 列表响应归一为「只有一个数组键」。

    早期实现用 ``{**result, "data": result[key]}`` 保留原键，导致同一个数组在
    响应里出现两次 —— 实测 ``/api/projects/?limit=100`` 因此多传 433 KB（占
    35%），且客户端只会读 ``data``。这里改为「取到数组后删除其余数组键」。
    """
    if not isinstance(result, dict):
        return {"data": result}
    array_keys = ("list", "documents", "items")
    for key in array_keys:
        value = result.get(key)
        if isinstance(value, list):
            payload = dict(result)
            for other in array_keys:
                payload.pop(other, None)
            payload["data"] = value
            return payload
    return dict(result)


# 列表响应允许返回的轻量字段；其余（sections / prep_sheet / workcard_assignment /
# standalone_prep_sheet / material_list / gantt_prep）由 /api/projects/<id>/ 按需返回。
PROJECT_LIST_FIELDS = (
    "_id",
    "name",
    "aircraft_type",
    "type",
    "team",
    "execute_date",
    "created_at",
    "updated_at",
    "version",
)


def _project_max_item_id(document: dict) -> int:
    """项目内物品的最大本地编号。

    客户端把 sections / material_list 读回内存时按 1..N 顺序编号（见前端
    ``stateFromSections``），所以「物品条数」就是该项目的最大编号。客户端拿它
    计算新增行的不冲突编号，这样列表不必返回重字段也能维持原有不变式
    （新增物品的编号大于所有项目的既有编号）。

    注意：持久化结构里物品只有 ``uid``，没有本地数字 id，因此服务端只能按
    条数推算 —— 这与客户端重新载入时的编号结果完全一致。
    """
    total = 0
    for key in ("sections", "material_list"):
        groups = document.get(key)
        if not isinstance(groups, list):
            continue
        for group in groups:
            works = group.get("works") if isinstance(group, dict) else None
            if not isinstance(works, list):
                continue
            for work in works:
                items = work.get("items") if isinstance(work, dict) else None
                if isinstance(items, list):
                    total += len(items)
    return total


def _project_summary(document: object) -> dict:
    """把项目文档裁剪为列表用的轻量元数据（不含重字段）。"""
    if not isinstance(document, dict):
        return {}
    summary = {key: document.get(key) for key in PROJECT_LIST_FIELDS if key in document}
    summary["_id"] = str(document.get("_id", ""))
    summary["max_item_id"] = _project_max_item_id(document)
    return summary


def index(request):
    return CompactJsonResponse(
        {
            "message": "Django API is running",
            "path": request.path,
            "timestamp": _now(),
        }
    )


@ensure_csrf_cookie
@require_http_methods(["GET"])
def csrf(request):
    return CompactJsonResponse({"ok": True, "csrf_token": get_token(request)})


def _client_ip(request: HttpRequest) -> str:
    """尽量还原真实客户端 IP（经 CloudBase 网关时可能带 X-Forwarded-For）。"""
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "unknown")


def _airnav_secret() -> str:
    """token 签名密钥：优先 Django 生产/本地密钥，兜底 AIRNAV 密码。"""
    return (
        os.getenv("DJANGO_PRODUCTION_SECRET_KEY")
        or os.getenv("DJANGO_SECRET_KEY")
        or os.getenv("AIRNAV_PASSWORD")
        or ""
    )


def _issue_airnav_token() -> tuple[str, int]:
    """签发短期 token（expiry.signature），供飞机信息读取鉴权使用。"""
    secret = _airnav_secret()
    expiry = int(time.time()) + AIRNAV_TOKEN_TTL
    sig = hmac.new(secret.encode("utf-8"), str(expiry).encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{expiry}.{sig}", expiry


def _verify_airnav_token(token: str) -> bool:
    """校验 token 的签名与有效期（防篡改、防过期）。"""
    secret = _airnav_secret()
    if not secret or not token:
        return False
    try:
        expiry_str, sig = token.split(".", 1)
        expiry = int(expiry_str)
    except (ValueError, AttributeError):
        return False
    if int(time.time()) > expiry:
        return False
    expected = hmac.new(secret.encode("utf-8"), expiry_str.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


def _airnav_throttled(ip: str) -> bool:
    entry = _AIRNAV_RATE.get(ip)
    return bool(entry) and time.time() < entry["until"]


def _airnav_fail(ip: str) -> None:
    entry = _AIRNAV_RATE.setdefault(ip, {"fail": 0, "until": 0.0})
    entry["fail"] += 1
    # 指数退避：5s、10s、20s…上限 300s
    entry["until"] = time.time() + min(5 * (2 ** (entry["fail"] - 1)), 300)


def _airnav_ok(ip: str) -> None:
    _AIRNAV_RATE.pop(ip, None)


@require_http_methods(["POST"])
def airnav_verify(request):
    """飞机信息标准库读取/编辑前的 AIRNAV 密码校验。

    密码只由环境变量 AIRNAV_PASSWORD 提供；未配置时直接 503，绝不复用任何
    硬编码默认值（历史默认 73409 已移除）。校验成功后签发短期 token，
    供后续读取飞机信息（GET /api/standard-libraries/aircraft_info/）使用。
    """
    expected = os.getenv("AIRNAV_PASSWORD")
    if not expected:
        return _error("服务端未配置 AIRNAV_PASSWORD，无法校验", 503)
    ip = _client_ip(request)
    if _airnav_throttled(ip):
        return _error("尝试过于频繁，请稍后再试", 429)
    try:
        body = _json_body(request)
        password = str(body.get("password", ""))
    except ValueError:
        password = ""
    if password and hmac.compare_digest(password.encode("utf-8"), expected.encode("utf-8")):
        _airnav_ok(ip)
        token, _ = _issue_airnav_token()
        return CompactJsonResponse({"ok": True, "verified": True, "token": token, "expires_in": AIRNAV_TOKEN_TTL})
    _airnav_fail(ip)
    return CompactJsonResponse({"ok": False, "verified": False, "error": "AIRNAV 密码错误"}, status=403)


@require_http_methods(["GET"])
def cloudbase_status(request):
    # 只返回是否完成配置，绝不把 API Key 内容发送给客户端。
    api_key = os.getenv("CLOUDBASE_API_KEY", "")
    return CompactJsonResponse(
        {
            "ok": True,
            "env_id": os.getenv("CLOUDBASE_ENV_ID", ""),
        "configured": bool(api_key and not api_key.startswith("replace-")),
        "collections": [
            PROJECTS,
            TEMPLATES,
            MATERIAL_TEMPLATES,
            TOOL_CART,
            AIRCRAFT_INFO,
            WORKCARD_320,
        ],
        }
    )


@require_http_methods(["GET"])
def poll(request):
    """返回业务数据的修订值：全局 ``revision`` + 各域修订值（成本 O(1) 读）。

    客户端据此**只重拉真正变化的域**对应的端点，而不是每次变化都重拉全部
    12 个端点（实测约 1.4 MB）。``scope`` 是能力标记，让新旧客户端都能工作：

    * 旧客户端：只读 ``revision`` / ``changed``，行为与以前一致（全量重拉）。
    * 新客户端：认 ``scope == "domains"``，逐域比较后只拉变化域。
    """
    try:
        revision, domains = _read_revision(get_nosql_client())
        # 首次不带 revision 只建立基线；之后仅在值不同时报告 changed。
        previous = request.GET.get("revision", "").strip().strip('"')
        changed = bool(previous and previous != revision)
        payload: dict[str, object] = {
            "ok": True,
            "revision": revision,
            "changed": changed,
            "scope": "domains",
            "poll_after_ms": 5000,
        }
        # 仅在「有变化」或「客户端尚无基线」时附带域映射：空闲时把响应压在
        # 百字节级，避免 2 秒一次的空轮询把省下的流量又吃回去。
        if changed or not previous:
            payload["domains"] = domains
        response = CompactJsonResponse(payload)
        # 禁止中间缓存复用旧结果；ETag 供支持条件请求的客户端扩展使用。
        response["ETag"] = f'"{revision}"'
        response["Cache-Control"] = "no-store"
        return response
    except (CloudBaseConfigError, CloudBaseAPIError) as exc:
        return _handle_cloudbase_error(exc)


@require_http_methods(["GET", "POST"])
def projects(request):
    try:
        client = get_nosql_client()
        if request.method == "GET":
            limit = min(max(int(request.GET.get("limit", 20)), 1), 100)
            offset = max(int(request.GET.get("offset", 0)), 0)
            query = {}
            if request.GET.get("team"):
                query["team"] = request.GET["team"]
            result = client.list_documents(
                PROJECTS,
                query=query,
                limit=limit,
                offset=offset,
                order=[{"field": "created_at", "direction": "desc"}],
            )
            # 归一为单一 data 数组：早期写法保留原键会让同一数组在响应里出现
            # 两次（实测多传 433 KB，占该响应 35%），而客户端只读 data。
            normalized = _normalize_list_payload(result)
            # 列表只回**轻量元数据**，重字段由 /api/projects/<id>/ 按需返回。
            # 实测把该响应从 1227.7 KB 降到约 3 KB：列表页本就只显示名称/类型/
            # 班组/执行日期，把 15 个项目的完整内容全传一遍纯属浪费。
            documents = normalized.get("data")
            if isinstance(documents, list):
                normalized = {**normalized, "data": [_project_summary(doc) for doc in documents]}
            return CompactJsonResponse({"ok": True, **normalized})

        body = _json_body(request)
        name = str(body.get("name", "")).strip()
        if not name:
            return _error("name 不能为空", 400)
        aircraft_type = str(body.get("aircraft_type", "A320")).upper()
        if aircraft_type not in AIRCRAFT_TYPES:
            return _error("aircraft_type 只支持 A320 或 B787", 400)

        project_type = str(body.get("type", "")).strip()
        if project_type and project_type not in PROJECT_TYPES:
            return _error("type 只支持 A检/零散/单独项目/换发/APU", 400)

        now = _now()
        document = {
            # 客户端生成 ID 会让失败重试更复杂，因此由可信后端统一生成。
            "_id": uuid4().hex,
            "name": name,
            "aircraft_type": aircraft_type,
            "type": project_type,
            "team": str(body.get("team", "")).strip(),
            # 执行日期（YYYYMMDD 字符串，如 20260817），可选。
            "execute_date": str(body.get("execute_date", "")).strip(),
            "sections": body.get("sections", []),
            "use_tool_cart": bool(body.get("use_tool_cart", False)),
            # A检项目的两个子结构；非 A检项目这两个字段保持为空。
            "prep_sheet": body.get("prep_sheet", {}),
            "workcard_assignment": body.get("workcard_assignment", {}),
            # 「单独项目」的两个子结构；非单独项目保持为空。
            "standalone_prep_sheet": body.get("standalone_prep_sheet", {}),
            "material_list": body.get("material_list", []),
            # 「换发/APU」的甘特工作准备单；非换发/APU 项目保持为空结构。
            "gantt_prep": body.get("gantt_prep", {}),
            "created_at": now,
            "updated_at": now,
            "version": 1,
        }
        if not isinstance(document["sections"], list):
            return _error("sections 必须是数组", 400)
        client.insert_document(PROJECTS, document)
        _bump_revision(client, DOMAIN_PROJECTS)
        return CompactJsonResponse({"ok": True, "data": document}, status=201)
    except ValueError as exc:
        return _error(str(exc), 400)
    except (CloudBaseConfigError, CloudBaseAPIError) as exc:
        return _handle_cloudbase_error(exc)


@require_http_methods(["GET", "PATCH", "DELETE"])
def project_detail(request, project_id):
    try:
        client = get_nosql_client()
        if request.method == "GET":
            return CompactJsonResponse(
                {"ok": True, "data": client.get_document(PROJECTS, project_id)}
            )
        if request.method == "DELETE":
            result = client.delete_document(PROJECTS, project_id)
            _bump_revision(client, DOMAIN_PROJECTS)
            return CompactJsonResponse({"ok": True, "result": result})

        body = _json_body(request)
        # 乐观锁：客户端带上 expected_version，用原子条件写实现（version 放进 query）。
        expected_version = body.get("expected_version")
        allowed = {
            "name",
            "aircraft_type",
            "type",
            "team",
            "execute_date",
            "sections",
            "use_tool_cart",
            "prep_sheet",
            "workcard_assignment",
            "standalone_prep_sheet",
            "material_list",
            "gantt_prep",
        }
        updates = {key: value for key, value in body.items() if key in allowed}
        if not updates:
            return _error("没有可更新字段", 400)
        if "aircraft_type" in updates:
            updates["aircraft_type"] = str(updates["aircraft_type"]).upper()
            if updates["aircraft_type"] not in AIRCRAFT_TYPES:
                return _error("aircraft_type 只支持 A320 或 B787", 400)
        if "type" in updates:
            updates["type"] = str(updates["type"]).strip()
            if updates["type"] and updates["type"] not in PROJECT_TYPES:
                return _error("type 只支持 A检/零散/单独项目/换发/APU", 400)
        if "sections" in updates and not isinstance(updates["sections"], list):
            return _error("sections 必须是数组", 400)
        if "prep_sheet" in updates and not isinstance(updates["prep_sheet"], dict):
            return _error("prep_sheet 必须是对象", 400)
        if "workcard_assignment" in updates and not isinstance(
            updates["workcard_assignment"], dict
        ):
            return _error("workcard_assignment 必须是对象", 400)
        if "standalone_prep_sheet" in updates and not isinstance(
            updates["standalone_prep_sheet"], dict
        ):
            return _error("standalone_prep_sheet 必须是对象", 400)
        if "material_list" in updates and not isinstance(updates["material_list"], list):
            return _error("material_list 必须是数组", 400)
        if "gantt_prep" in updates and not isinstance(updates["gantt_prep"], dict):
            return _error("gantt_prep 必须是对象", 400)
        updates["updated_at"] = _now()
        if expected_version is not None:
            # 原子乐观锁：单次请求内「版本匹配 + 更新」同时完成，消除读改写竞态。
            result = client.update_documents_where(
                PROJECTS,
                {"_id": project_id, "version": int(expected_version)},
                {"$set": updates, "$inc": {"version": 1}},
            )
            if not isinstance(result, dict) or result.get("matched", 0) == 0:
                # matched=0 有两种成因，必须区分：
                # ① 文档存在但版本不匹配 → 真并发冲突（409，前端按冲突流程处理）；
                # ② 文档根本不存在（已被他人删除 / id 过期）→ 404。
                # 若不加区分，已删除的项目会被一直报成「并发修改」，前端会带着
                # 新版本号反复重试，永远保存不上却始终显示"正在重试"。
                current_version = None
                exists = True
                try:
                    current = client.get_document(PROJECTS, project_id)
                    if isinstance(current, dict):
                        current_version = int(current.get("version", 0))
                except CloudBaseAPIError as exc:
                    if exc.status == 404:
                        exists = False
                if not exists:
                    return _error("项目不存在或已被删除", 404)
                return CompactJsonResponse(
                    {
                        "ok": False,
                        "error": "数据已被他人修改，请刷新后重试",
                        "current_version": current_version,
                        "expected_version": expected_version,
                    },
                    status=409,
                )
        else:
            result = client.update_document(
                PROJECTS,
                project_id,
                # version 每次写入都原子递增，供冲突检测和审计使用。
                {"$set": updates, "$inc": {"version": 1}},
            )
            # ⚠️ 必须检查 matched：CloudBase 对不存在的文档返回 matched=0 且**不报错**，
            # 若直接返回 200，则「保存到已删除/错误 id」会表现为成功 —— 数据实际
            # 没有写入却无人察觉（静默数据丢失，调用方与日志都不会发现）。
            if not isinstance(result, dict) or result.get("matched", 0) == 0:
                return _error("项目不存在或已被删除", 404)
        _bump_revision(client, DOMAIN_PROJECTS)
        return CompactJsonResponse({"ok": True, "result": result})
    except ValueError as exc:
        return _error(str(exc), 400)
    except (CloudBaseConfigError, CloudBaseAPIError) as exc:
        return _handle_cloudbase_error(exc)


@require_http_methods(["GET", "PUT"])
def aircraft_template(request, aircraft_type):
    aircraft_type = aircraft_type.upper()
    if aircraft_type not in AIRCRAFT_TYPES:
        return _error("机型只支持 A320 或 B787", 404)
    try:
        client = get_nosql_client()
        if request.method == "GET":
            return CompactJsonResponse(
                {"ok": True, "data": client.get_document(TEMPLATES, aircraft_type)}
            )
        body = _json_body(request)
        sections = body.get("sections", [])
        if not isinstance(sections, list):
            return _error("sections 必须是数组", 400)
        result = client.update_document(
            TEMPLATES,
            aircraft_type,
            {
                "$set": {
                    "aircraft_type": aircraft_type,
                    "sections": sections,
                    "updated_at": _now(),
                }
            },
            # 标准库首次保存时可能尚不存在，因此允许原子创建或更新。
            upsert=True,
        )
        _bump_revision(client, DOMAIN_TEMPLATES)
        return CompactJsonResponse({"ok": True, "result": result})
    except ValueError as exc:
        return _error(str(exc), 400)
    except (CloudBaseConfigError, CloudBaseAPIError) as exc:
        return _handle_cloudbase_error(exc)


@require_http_methods(["GET", "PUT"])
def material_template(request, aircraft_type):
    """航材标准库（A320 / B787）：结构与工具标准库一致（sections），物品 item 含 partNo。"""
    aircraft_type = aircraft_type.upper()
    if aircraft_type not in AIRCRAFT_TYPES:
        return _error("机型只支持 A320 或 B787", 404)
    try:
        client = get_nosql_client()
        if request.method == "GET":
            return CompactJsonResponse(
                {"ok": True, "data": client.get_document(MATERIAL_TEMPLATES, aircraft_type)}
            )
        body = _json_body(request)
        sections = body.get("sections", [])
        if not isinstance(sections, list):
            return _error("sections 必须是数组", 400)
        result = client.update_document(
            MATERIAL_TEMPLATES,
            aircraft_type,
            {
                "$set": {
                    "aircraft_type": aircraft_type,
                    "sections": sections,
                    "updated_at": _now(),
                }
            },
            upsert=True,
        )
        _bump_revision(client, DOMAIN_TEMPLATES)
        return CompactJsonResponse({"ok": True, "result": result})
    except ValueError as exc:
        return _error(str(exc), 400)
    except (CloudBaseConfigError, CloudBaseAPIError) as exc:
        return _handle_cloudbase_error(exc)


@require_http_methods(["GET", "PUT"])
def tool_cart(request):
    try:
        client = get_nosql_client()
        if request.method == "GET":
            return CompactJsonResponse(
                {"ok": True, "data": client.get_document(TOOL_CART, "default")}
            )
        body = _json_body(request)
        items = body.get("items", [])
        if not isinstance(items, list):
            return _error("items 必须是数组", 400)
        result = client.update_document(
            TOOL_CART,
            "default",
            {"$set": {"items": items, "updated_at": _now()}},
            # 工具车使用固定文档 ID，首次保存时允许直接创建。
            upsert=True,
        )
        _bump_revision(client, DOMAIN_CART)
        return CompactJsonResponse({"ok": True, "result": result})
    except ValueError as exc:
        return _error(str(exc), 400)
    except (CloudBaseConfigError, CloudBaseAPIError) as exc:
        return _handle_cloudbase_error(exc)


@require_http_methods(["GET", "PUT"])
def announcement(request):
    """一级页面公告栏：单文档存储，GET 读取，PUT 整体替换 content 字段。"""
    try:
        client = get_nosql_client()
        if request.method == "GET":
            return CompactJsonResponse(
                {"ok": True, "data": client.get_document(ANNOUNCEMENT, "default")}
            )
        body = _json_body(request)
        content = body.get("content", "")
        if not isinstance(content, str):
            return _error("content 必须是字符串", 400)
        result = client.update_document(
            ANNOUNCEMENT,
            "default",
            {"$set": {"content": content, "updated_at": _now()}},
            upsert=True,
        )
        _bump_revision(client, DOMAIN_ANNOUNCEMENT)
        return CompactJsonResponse({"ok": True, "result": result})
    except ValueError as exc:
        return _error(str(exc), 400)
    except (CloudBaseConfigError, CloudBaseAPIError) as exc:
        return _handle_cloudbase_error(exc)


@require_http_methods(["GET", "PUT"])
def standard_library(request, lib_key):
    """三类标准库的读写：飞机信息 / 320 工卡分配 / 787 工卡分配。

    标准库整体以 rows 数组存储，GET 返回整库（导出/编辑），PUT 整体替换
    rows（导入）。单行的「新增/删除」由前端修改本地 rows 数组后再 PUT 完成。
    """
    if lib_key not in STANDARD_LIBRARIES:
        return _error("未知的标准库", 404)
    meta = STANDARD_LIBRARIES[lib_key]
    collection, doc_id = meta["collection"], meta["doc_id"]
    try:
        client = get_nosql_client()
        if request.method == "GET":
            # 飞机信息读取自 2026-09-06 起放开为公开（工作准备单机号回填需全量常驻本地，
            # 输入框响应不依赖逐机号网络单查；与公开的 aircraft-numbers / aircraft-info 旁路对齐）。
            # 写入（下方 PUT）仍保留 AIRNAV token 保护，防止外部整体篡改。
            return CompactJsonResponse(
                {"ok": True, "data": client.get_document(collection, doc_id)}
            )
        body = _json_body(request)
        rows = body.get("rows", [])
        if not isinstance(rows, list):
            return _error("rows 必须是数组", 400)
        # 飞机信息的写入同样需要 AIRNAV 授权（编辑前已过密码门，此处校验 token）。
        if lib_key == "aircraft_info":
            token = request.META.get("HTTP_X_AIRNAV_TOKEN", "")
            if not _verify_airnav_token(token):
                return _error("需要 AIRNAV 授权才能修改飞机信息", 403)
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                return _error(f"第 {index + 1} 行必须是对象", 400)
        result = client.update_document(
            collection,
            doc_id,
            {"$set": {"rows": rows, "updated_at": _now()}},
            upsert=True,
        )
        _bump_revision(client, DOMAIN_STDLIBS)
        return CompactJsonResponse({"ok": True, "result": result})
    except ValueError as exc:
        return _error(str(exc), 400)
    except (CloudBaseConfigError, CloudBaseAPIError) as exc:
        return _handle_cloudbase_error(exc)


@require_http_methods(["GET"])
def app_config(request):
    """应用运行时配置（远端下发）：watch 实时推送开关与阈值。

    前端 loadRemote 时读取，据此决定启用 watch 实时推送还是退回轮询；
    管理员通过 CloudBase 控制台 / MCP 修改 app_config 集合的 default 文档即可
    动态切换，无需重新部署前后端。
    """
    try:
        client = get_nosql_client()
        doc = client.get_document(APP_CONFIG, "default")
    except CloudBaseAPIError as exc:
        if exc.status == 404:
            doc = {}
        else:
            return _handle_cloudbase_error(exc)
    data = doc if isinstance(doc, dict) else {}
    return CompactJsonResponse(
        {
            "ok": True,
            "data": {
                "watch_enabled": bool(data.get("watch_enabled", False)),
                "watch_max_users": int(data.get("watch_max_users", 10)),
            },
        }
    )


def _read_std_rows(client: CloudBaseNoSQLClient, collection: str) -> list:
    """读标准库文档（rows 数组，doc_id="default"）。"""
    try:
        doc = client.get_document(collection, "default")
    except CloudBaseAPIError as exc:
        if exc.status == 404:
            return []
        raise
    rows = doc.get("rows") if isinstance(doc, dict) else None
    return rows if isinstance(rows, list) else []


def _read_std_sections(client: CloudBaseNoSQLClient, collection: str, doc_id: str) -> list:
    """读标准库文档（sections 数组）。"""
    try:
        doc = client.get_document(collection, doc_id)
    except CloudBaseAPIError as exc:
        if exc.status == 404:
            return []
        raise
    sections = doc.get("sections") if isinstance(doc, dict) else None
    return sections if isinstance(sections, list) else []


def _infer_aircraft_type(aircraft_rows: list, reg_no: str) -> str:
    """从飞机信息标准库按机号推断机型：含 787 → B787，含 320/321 → A320，无 → ""。"""
    target = (reg_no or "").strip()
    if not target:
        return ""
    for row in aircraft_rows:
        if (row.get("飞机号") or "").strip() == target:
            model = str(row.get("机型") or "").upper()
            if "787" in model:
                return "B787"
            if "320" in model or "321" in model:
                return "A320"
            break
    return ""


@require_http_methods(["POST"])
def apply_workcard(request, project_id):
    """依据工卡清单：后端计算工卡分配 + 工具/航材清单自动筛选，直接写入云端。

    两种模式（同一端点）：
    - 完整模式：body 带 cards（xlsx 解析结果）→ 工卡分配 + 工具/航材筛选；
    - 筛选模式：body 不带 cards → 仅用项目已有 workcard_assignment 做工具/航材筛选
      （对应前端手动「按卡筛选」按钮）。

    写入后 _bump_revision 触发其它端同步，前端 loadRemote 显示权威结果。
    """
    try:
        client = get_nosql_client()
        body = _json_body(request)

        project_doc = client.get_document(PROJECTS, project_id)
        if not isinstance(project_doc, dict):
            return _error("项目不存在", 404)

        cards = body.get("cards")
        full_mode = isinstance(cards, list) and len(cards) > 0
        aircraft_rows: list = []
        if full_mode:
            aircraft_rows = _read_std_rows(client, AIRCRAFT_INFO)

        # 确定机型：body 优先 → 机号推断（查飞机信息标准库）→ 项目字段 → A320。
        # 机号推断必须在标准库比对之前完成，否则导入工卡（787 机号）会用默认 A320 比对。
        aircraft_type = str(body.get("aircraft_type") or "").upper()
        if aircraft_type not in AIRCRAFT_TYPES:
            aircraft_type = _infer_aircraft_type(aircraft_rows, str(body.get("机号") or ""))
        if aircraft_type not in AIRCRAFT_TYPES:
            aircraft_type = str(project_doc.get("aircraft_type") or "A320").upper()
        if aircraft_type not in AIRCRAFT_TYPES:
            aircraft_type = "A320"

        if full_mode:
            workcard_rows = _read_std_rows(client, WORKCARD_320)
            # 1) 工卡分配（复用 aircraft_rows）
            prep_sheet, assignment, written = apply_work_card_list(
                project_doc, workcard_rows, aircraft_rows, body
            )
        else:
            written = 0
            prep_sheet = project_doc.get("prep_sheet") if isinstance(project_doc.get("prep_sheet"), dict) else {}
            assignment = project_doc.get("workcard_assignment") if isinstance(project_doc.get("workcard_assignment"), dict) else {}

        tool_lib = _read_std_sections(client, TEMPLATES, aircraft_type)
        material_lib = _read_std_sections(client, MATERIAL_TEMPLATES, aircraft_type)
        names = collect_workcard_names(assignment)
        apu_names = collect_apu_workcard_names(assignment)
        lube_names = collect_keyword_names(assignment, "润滑")
        clean_names = collect_keyword_names(assignment, "清洁")
        engine = str((prep_sheet.get("base") or {}).get("发动机", ""))

        # 2) 工具清单筛选（data → sections）
        project_sections = project_doc.get("sections") if isinstance(project_doc.get("sections"), list) else []
        tool_sections, tool_deleted, tool_added = apply_tool_filter(
            project_sections, tool_lib, names, apu_names, lube_names, clean_names, engine
        )

        # 3) 航材清单筛选（material_list → sections）
        material_sections = project_doc.get("material_list") if isinstance(project_doc.get("material_list"), list) else []
        material_sections, mat_deleted, mat_added = apply_material_filter(
            material_sections, material_lib, names, apu_names, lube_names, clean_names, engine
        )

        updates = {
            "sections": tool_sections,
            "material_list": material_sections,
            "updated_at": _now(),
        }
        if full_mode:
            updates["prep_sheet"] = prep_sheet
            updates["workcard_assignment"] = assignment
        client.update_document(PROJECTS, project_id, {"$set": updates, "$inc": {"version": 1}})
        _bump_revision(client, DOMAIN_PROJECTS)

        return CompactJsonResponse(
            {
                "ok": True,
                "data": {
                    "written": written,
                    "aircraft_type": aircraft_type,
                    "tool_deleted": tool_deleted,
                    "tool_added": tool_added,
                    "material_deleted": mat_deleted,
                    "material_added": mat_added,
                },
            }
        )
    except ValueError as exc:
        return _error(str(exc), 400)
    except (CloudBaseConfigError, CloudBaseAPIError) as exc:
        return _handle_cloudbase_error(exc)


@require_http_methods(["GET"])
def aircraft_numbers(request):
    """公开返回飞机信息标准库的机号列表（仅机号，不含 FSN/MSN/发动机等敏感字段）。

    机号（飞机注册号）是公开信息，供工作准备单/单项准备单的机号下拉模糊搜索使用；
    敏感字段仍由 AIRNAV token 保护（standard_library 的 aircraft_info 读取鉴权不变）。
    """
    try:
        client = get_nosql_client()
        rows = _read_std_rows(client, AIRCRAFT_INFO)
        numbers = sorted(
            {
                str(row.get("飞机号") or "").strip()
                for row in rows
                if str(row.get("飞机号") or "").strip()
            }
        )
        return CompactJsonResponse({"ok": True, "data": numbers})
    except (CloudBaseConfigError, CloudBaseAPIError) as exc:
        return _handle_cloudbase_error(exc)


@require_http_methods(["GET", "POST"])
def aircraft_info(request):
    """公开的飞机信息接口（无需 AIRNAV 授权）。

    GET  ?reg=机号  → 按机号查询单架飞机信息（FSN/MSN/机型/发动机/ETOPS/ELT-DT），
                      供工作准备单/单项准备单机号回填展示。
    POST body       → 更新/补充单机机型数据（机号+FSN+MSN+发动机+机型+ETOPS+ELT-DT，全字段必填）。
                      机号索引不到则插入，已存在则更新该行；由二级页「更新机型标准库」弹窗确认后调用。
    """
    if request.method == "GET":
        reg = str(request.GET.get("reg", "") or "").strip().upper()
        if not reg:
            return CompactJsonResponse({"ok": True, "data": None})
        try:
            client = get_nosql_client()
            rows = _read_std_rows(client, AIRCRAFT_INFO)
            for row in rows:
                if str(row.get("飞机号") or "").strip().upper() == reg:
                    return CompactJsonResponse({"ok": True, "data": row})
            return CompactJsonResponse({"ok": True, "data": None})
        except (CloudBaseConfigError, CloudBaseAPIError) as exc:
            return _handle_cloudbase_error(exc)

    # POST：更新/补充单机机型数据（upsert），全字段必填
    try:
        body = _json_body(request)
    except ValueError as exc:
        return _error(str(exc), 400)
    reg = _normalize_aircraft_reg(str(body.get("飞机号") or ""))
    if not reg:
        return _error("飞机号格式应为 B-XXXX（或 XXXX）", 400)
    row_keys = STANDARD_LIBRARIES["aircraft_info"]["row_keys"]
    new_row = {k: str(body.get(k) or "").strip() for k in row_keys}
    new_row["飞机号"] = reg
    for k in row_keys:
        if not new_row[k]:
            return _error(f"{k} 不能为空", 400)
    try:
        client = get_nosql_client()
        rows = _read_std_rows(client, AIRCRAFT_INFO)
        updated = False
        for row in rows:
            if str(row.get("飞机号") or "").strip().upper() == reg:
                for k in row_keys:
                    row[k] = new_row[k]
                updated = True
                break
        if not updated:
            rows.append(new_row)
        client.update_document(
            AIRCRAFT_INFO,
            "default",
            {"$set": {"rows": rows, "updated_at": _now()}},
            upsert=True,
        )
        _bump_revision(client, DOMAIN_STDLIBS)
        return CompactJsonResponse({"ok": True, "data": new_row, "updated": updated})
    except (CloudBaseConfigError, CloudBaseAPIError) as exc:
        return _handle_cloudbase_error(exc)


def _normalize_aircraft_reg(value: str) -> str:
    """机号规范化：支持 B-XXXX（6 字符）或 XXXX（4 字符，自动补 B- 前缀）。非法返回空串。"""
    s = (value or "").strip().upper()
    if len(s) == 6 and s[:2] == "B-" and all(c.isalnum() and c.isascii() for c in s[2:]):
        return s
    if len(s) == 4 and all(c.isalnum() and c.isascii() for c in s):
        return "B-" + s
    return ""


def _get_storage_client() -> CloudBaseStorageClient:
    return CloudBaseStorageClient()


@require_http_methods(["GET", "POST"])
def control_docs(request):
    """现场管控单列表 / 上传。

    GET：返回全部现场管控单元数据（按项目类型组织）。
    POST：上传现场管控单文件（body 含 base64 content），上传到云存储并记录 fileid。
    """
    try:
        client = get_nosql_client()
        if request.method == "GET":
            result = client.list_documents(CONTROL_DOCS, limit=200)
            docs = None
            if isinstance(result, dict):
                for key in ("list", "data", "documents", "items"):
                    if isinstance(result.get(key), list):
                        docs = result[key]
                        break
            return CompactJsonResponse({"ok": True, "data": docs or []})

        body = _json_body(request)
        doc_type = str(body.get("type", "")).strip()
        if not doc_type:
            return _error("type 不能为空", 400)
        if doc_type not in PROJECT_TYPES:
            return _error("type 只支持 A检/零散/单独项目/换发/APU", 400)
        file_name = str(body.get("fileName", "")).strip()
        content_b64 = str(body.get("content", "") or "")
        if not file_name:
            return _error("fileName 不能为空", 400)
        if not content_b64:
            return _error("content 不能为空", 400)
        try:
            file_bytes = base64.b64decode(content_b64)
        except Exception:
            return _error("content 不是合法的 base64", 400)

        # 云存储路径：control-doc/<type>/<文件名>，同名覆盖（同一类型只保留最新一份）。
        safe_name = file_name.replace("/", "_").replace("\\", "_")
        object_id = f"control-doc/{doc_type}/{safe_name}"
        storage = _get_storage_client()
        cloud_object_id = storage.upload_bytes(object_id, file_bytes)

        doc_id = uuid4().hex
        document = {
            "_id": doc_id,
            "type": doc_type,
            "fileName": file_name,
            "cloudObjectId": cloud_object_id,
            "uploadedAt": _now(),
        }
        client.insert_document(CONTROL_DOCS, document)
        _bump_revision(client, DOMAIN_CONTROL)
        return CompactJsonResponse({"ok": True, "data": document}, status=201)
    except ValueError as exc:
        return _error(str(exc), 400)
    except (CloudBaseConfigError, CloudBaseAPIError) as exc:
        return _handle_cloudbase_error(exc)


@require_http_methods(["GET", "DELETE"])
def control_doc_detail(request, doc_id):
    """现场管控单详情：GET 返回下载链接，DELETE 删除文件与元数据。"""
    try:
        client = get_nosql_client()
        doc = client.get_document(CONTROL_DOCS, doc_id)
        if not isinstance(doc, dict) or not doc.get("_id"):
            return _error("现场管控单不存在", 404)
        if request.method == "DELETE":
            cloud_id = doc.get("cloudObjectId")
            if cloud_id:
                try:
                    _get_storage_client().delete_object(str(cloud_id))
                except (CloudBaseConfigError, CloudBaseAPIError):
                    pass  # 文件删除失败不阻断元数据删除
            client.delete_document(CONTROL_DOCS, doc_id)
            _bump_revision(client, DOMAIN_CONTROL)
            return CompactJsonResponse({"ok": True})
        cloud_id = doc.get("cloudObjectId")
        if not cloud_id:
            return _error("该记录缺少 fileid", 400)
        url = _get_storage_client().get_download_url(str(cloud_id))
        return CompactJsonResponse({"ok": True, "data": {"downloadUrl": url, "fileName": doc.get("fileName", "")}})
    except (CloudBaseConfigError, CloudBaseAPIError) as exc:
        return _handle_cloudbase_error(exc)


# ============ 准备单附件（换发 / 单独项目「附件卡片」）============
# 文件实体存云存储（object key = uuid hex，无斜杠）；元数据（名称/日期/fileKey）由前端写入项目/模板的
# attachments 引用列表并随保存同步（模板保存/调取全量透传）。删除附件 = 前端移除引用（懒清理，对象共享）。
PREP_ATTACH_MAX_B64 = 8 * 1024 * 1024  # base64 中转上限（SCF/网关约束）→ 单个文件建议 ≤5-6MB


@require_http_methods(["GET", "POST", "DELETE"])
def prep_attachment_file(request):
    """准备单附件对象（POST=上传 / GET=下载 URL / DELETE=物理删除）。

    上传 body { fileName, content(base64) } → 云存储对象，返回 {fileKey(cloudObjectId),name,size,uploadedAt}；
    引用归属由前端写入项目/模板 attachments 随保存同步。
    GET/DELETE 用 query `file_key`（cloudObjectId 含 ://与 /，path 传参会被网关还原为段，故用 query）。
    DELETE 为懒清理/脚本用；前端「删除」仅移除引用、不调用。
    """
    storage = _get_storage_client()
    if request.method == "POST":
        try:
            body = _json_body(request)
        except ValueError as exc:
            return _error(str(exc), 400)
        file_name = str(body.get("fileName", "")).strip()
        content_b64 = str(body.get("content", "") or "")
        if not file_name:
            return _error("fileName 不能为空", 400)
        if not content_b64:
            return _error("content 不能为空", 400)
        if len(content_b64) > PREP_ATTACH_MAX_B64:
            return _error("附件过大（base64 中转上限 8MB，单个文件约 ≤5-6MB）", 400)
        try:
            file_bytes = base64.b64decode(content_b64)
        except Exception:
            return _error("content 不是合法的 base64", 400)
        if not file_bytes:
            return _error("content 为空", 400)
        file_key = uuid4().hex
        try:
            cloud_object_id = storage.upload_bytes(file_key, file_bytes, content_type="application/octet-stream")
        except (CloudBaseConfigError, CloudBaseAPIError) as exc:
            return _handle_cloudbase_error(exc)
        return CompactJsonResponse(
            {"ok": True, "data": {"fileKey": cloud_object_id, "name": file_name, "size": len(file_bytes), "uploadedAt": _now()}},
            status=201,
        )
    file_key = request.GET.get("file_key", "").strip()
    if not file_key:
        return _error("file_key 缺失", 400)
    try:
        if request.method == "DELETE":
            storage.delete_object(file_key)
            return CompactJsonResponse({"ok": True})
        url = storage.get_download_url(file_key)
        return CompactJsonResponse({"ok": True, "data": {"downloadUrl": url}})
    except (CloudBaseConfigError, CloudBaseAPIError) as exc:
        return _handle_cloudbase_error(exc)


@require_http_methods(["POST"])
def identity(request):
    """记录一次无密码身份登录（姓名 2-5 字符）。

    本质是「标识」而非「鉴权」：只回答"谁在操作"，不验证身份真伪。
    每账号一文档（doc_id = 姓名），upsert 维护首次登录时间、最近登录时间、登录次数。
    """
    try:
        body = _json_body(request)
        name = str(body.get("name", "")).strip()
    except ValueError:
        return _error("请求体不是有效的 JSON", 400)
    if not (2 <= len(name) <= 5):
        return _error("姓名需为 2-5 个字符", 400)

    client = get_nosql_client()
    now = _now()
    first_seen = now
    login_count = 1
    try:
        existing = client.get_document(ACCOUNTS, name)
        if isinstance(existing, dict):
            first_seen = str(existing.get("first_seen") or now)
            login_count = int(existing.get("login_count") or 0) + 1
    except CloudBaseAPIError:
        pass  # 账号首次登录，按首次处理

    try:
        client.update_document(
            ACCOUNTS,
            name,
            {
                "$set": {
                    "name": name,
                    "first_seen": first_seen,
                    "last_seen": now,
                    "login_count": login_count,
                }
            },
            upsert=True,
        )
    except (CloudBaseConfigError, CloudBaseAPIError) as exc:
        return _handle_cloudbase_error(exc)
    # 刻意**不**调用 _bump_revision：身份心跳只更新账号目录的 last_seen /
    # login_count，属于「在线状态」而非业务数据，而客户端重拉的端点里根本不含
    # 账号数据 —— 广播它等于 100% 无效重拉。早期版本在此广播，导致每个在线用户
    # 每 60 秒让其他所有人重拉约 1.4 MB（N ×（N−1）× 1.4 MB / 分钟）。
    # 账号列表由站点管理面板打开时显式拉取，不依赖 revision 变化。
    return CompactJsonResponse({"ok": True, "data": {"name": name, "last_seen": now, "login_count": login_count}})


@require_http_methods(["GET"])
def accounts(request):
    """读取登录过的账号目录（需 X-Airnav-Token 鉴权，复用 AIRNAV 密码门）。"""
    token = request.headers.get("X-Airnav-Token", "")
    if not _verify_airnav_token(token):
        return _error("需要 AIRNAV 授权", 401)
    client = get_nosql_client()
    try:
        result = client.list_documents(ACCOUNTS, limit=500)
        docs = result.get("data") if isinstance(result, dict) else None
        accounts: list[dict] = []
        if isinstance(docs, list):
            for d in docs:
                if isinstance(d, dict) and d.get("name") and d.get("doc_type") != "editing":
                    accounts.append(
                        {
                            "name": str(d.get("name")),
                            "first_seen": str(d.get("first_seen") or ""),
                            "last_seen": str(d.get("last_seen") or ""),
                            "login_count": int(d.get("login_count") or 0),
                        }
                    )
        accounts.sort(key=lambda a: a.get("last_seen", ""), reverse=True)
        return CompactJsonResponse({"ok": True, "data": accounts})
    except (CloudBaseConfigError, CloudBaseAPIError) as exc:
        return _handle_cloudbase_error(exc)


@require_http_methods(["GET"])
def online_count(request):
    """统计网站同时在线人数：work_accounts 中 last_seen 距今 ≤ 5 分钟的账号数（免鉴权，供页面徽标轮询）。

    前端每 60 秒 POST /api/identity/ 心跳保活 last_seen，本端点按 5 分钟窗口聚合。
    """
    client = get_nosql_client()
    try:
        result = client.list_documents(ACCOUNTS, limit=500)
        docs = result.get("data") if isinstance(result, dict) else None
        now = datetime.now(timezone.utc)
        count = 0
        if isinstance(docs, list):
            for d in docs:
                if not isinstance(d, dict) or not d.get("name") or d.get("doc_type") == "editing":
                    continue
                last_seen = str(d.get("last_seen") or "")
                try:
                    ts = datetime.fromisoformat(last_seen)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if (now - ts).total_seconds() <= 300:  # 5 分钟在线窗口
                        count += 1
                except ValueError:
                    continue
        return CompactJsonResponse({"ok": True, "data": {"count": count}})
    except (CloudBaseConfigError, CloudBaseAPIError) as exc:
        return _handle_cloudbase_error(exc)


@require_http_methods(["GET", "POST"])
def editing(request):
    """字段/输入框级编辑会话（软锁，协作约束非安全层）。

    POST 上报当前正在编辑的字段：{ session_id, name, project_id, key }
      - key 非空：心跳保活（每 ~15s 上报一次），upsert doc_id="editing:"+session_id；
      - key 为空串：主动释放（更新 at 为过去时间，等价删除语义）。
    GET ?project_id=xxx 返回最近 EDIT_SESSION_TTL_SECONDS 秒内活跃的其他用户编辑会话
      [{ session_id, name, key }]（排除自己的 session_id），供对方渲染「某人正在编辑」黄锁。

    免登录无鉴权：锁是协作约束，不是安全层；不校验身份真伪。
    """
    client = get_nosql_client()
    if request.method == "POST":
        try:
            body = _json_body(request)
        except ValueError as exc:
            return _error(str(exc), 400)
        session_id = str(body.get("session_id", "")).strip()
        name = str(body.get("name", "")).strip()
        project_id = str(body.get("project_id", "")).strip()
        key = str(body.get("key", "")).strip()
        if not session_id or len(session_id) > 64:
            return _error("session_id 缺失或过长", 400)
        if key:
            if not (2 <= len(name) <= 5):
                return _error("姓名需为 2-5 个字符", 400)
            if not project_id or len(project_id) > 64:
                return _error("project_id 缺失或过长", 400)
            if len(key) > 200:
                return _error("key 过长", 400)
            doc_id = f"editing:{session_id}"
            try:
                client.update_document(
                    ACCOUNTS,
                    doc_id,
                    {
                        "$set": {
                            "doc_type": "editing",
                            "session_id": session_id,
                            "name": name,
                            "project_id": project_id,
                            "key": key,
                            "at": _now(),
                        }
                    },
                    upsert=True,
                )
            except (CloudBaseConfigError, CloudBaseAPIError) as exc:
                return _handle_cloudbase_error(exc)
        else:
            # 主动释放：把 at 推到过去，避免残留（查询端按 TTL 懒过滤兜底）。
            try:
                client.update_document(
                    ACCOUNTS,
                    f"editing:{session_id}",
                    {"$set": {"key": "", "at": "1970-01-01T00:00:00+00:00"}},
                    upsert=False,
                )
            except (CloudBaseConfigError, CloudBaseAPIError):
                pass  # 文档不存在视为已释放
        return CompactJsonResponse({"ok": True})

    project_id = request.GET.get("project_id", "").strip()
    if not project_id:
        return _error("project_id 缺失", 400)
    exclude_session = request.GET.get("session_id", "").strip()
    try:
        result = client.list_documents(ACCOUNTS, limit=1000)
    except (CloudBaseConfigError, CloudBaseAPIError) as exc:
        return _handle_cloudbase_error(exc)
    docs = None
    if isinstance(result, dict):
        for key in ("list", "data", "documents", "items"):
            if isinstance(result.get(key), list):
                docs = result[key]
                break
    now = datetime.now(timezone.utc)
    active: list[dict] = []
    if isinstance(docs, list):
        for d in docs:
            if not isinstance(d, dict) or d.get("doc_type") != "editing":
                continue
            sid = str(d.get("session_id") or "")
            key = str(d.get("key") or "").strip()
            if not sid or not key or sid == exclude_session:
                continue
            if str(d.get("project_id") or "") != project_id:
                continue
            at = str(d.get("at") or "")
            try:
                ts = datetime.fromisoformat(at)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if (now - ts).total_seconds() > EDIT_SESSION_TTL_SECONDS:
                    continue
            except ValueError:
                continue
            active.append(
                {
                    "session_id": sid,
                    "name": str(d.get("name") or ""),
                    "key": key,
                }
            )
    return CompactJsonResponse({"ok": True, "data": active})


@require_http_methods(["GET", "POST"])
def eng_templates(request):
    """换发/APU 模板库：模板列表（公开）/ 新建模板。

    每模板一文档（_id = uuid），字段 = { id, name, savedAt, state }，
    state 即 GanttPrep 全量（charts/manualParticipants/docs/airParts/toolParts/meta/currentTemplateName）。
    """
    client = get_nosql_client()
    if request.method == "GET":
        try:
            result = client.list_documents(ENG_TEMPLATES, limit=200)
            docs = None
            if isinstance(result, dict):
                for key in ("list", "data", "documents", "items"):
                    if isinstance(result.get(key), list):
                        docs = result[key]
                        break
            return CompactJsonResponse({"ok": True, "data": docs or []})
        except (CloudBaseConfigError, CloudBaseAPIError) as exc:
            return _handle_cloudbase_error(exc)

    try:
        body = _json_body(request)
    except ValueError as exc:
        return _error(str(exc), 400)
    name = str(body.get("name", "")).strip()
    if not name:
        return _error("name 不能为空", 400)
    state = body.get("state")
    if not isinstance(state, dict):
        return _error("state 必须是对象", 400)
    now = _now()
    doc_id = uuid4().hex
    document = {"_id": doc_id, "id": doc_id, "name": name, "savedAt": now, "state": state}
    try:
        client.insert_document(ENG_TEMPLATES, document)
        _bump_revision(client, DOMAIN_SYNC_TEMPLATES)
        return CompactJsonResponse({"ok": True, "data": document}, status=201)
    except (CloudBaseConfigError, CloudBaseAPIError) as exc:
        return _handle_cloudbase_error(exc)


@require_http_methods(["GET", "PUT", "DELETE"])
def eng_template_detail(request, template_id):
    """模板详情 / 整体替换（name + state）/ 删除。"""
    client = get_nosql_client()
    try:
        if request.method == "GET":
            return CompactJsonResponse({"ok": True, "data": client.get_document(ENG_TEMPLATES, template_id)})
        if request.method == "DELETE":
            client.delete_document(ENG_TEMPLATES, template_id)
            _bump_revision(client, DOMAIN_SYNC_TEMPLATES)
            return CompactJsonResponse({"ok": True})
        body = _json_body(request)
        name = str(body.get("name", "")).strip()
        state = body.get("state")
        if not name:
            return _error("name 不能为空", 400)
        if not isinstance(state, dict):
            return _error("state 必须是对象", 400)
        client.update_document(
            ENG_TEMPLATES,
            template_id,
            {"$set": {"name": name, "state": state, "savedAt": _now()}},
            upsert=True,
        )
        _bump_revision(client, DOMAIN_SYNC_TEMPLATES)
        return CompactJsonResponse({"ok": True})
    except ValueError as exc:
        return _error(str(exc), 400)
    except (CloudBaseConfigError, CloudBaseAPIError) as exc:
        return _handle_cloudbase_error(exc)


@require_http_methods(["POST"])
def eng_template_duplicate(request, template_id):
    """复制模板：新 uuid + 名称后缀「副本」。"""
    client = get_nosql_client()
    try:
        doc = client.get_document(ENG_TEMPLATES, template_id)
        if not isinstance(doc, dict) or not doc.get("name"):
            return _error("模板不存在", 404)
        new_id = uuid4().hex
        new_doc = {
            "_id": new_id,
            "id": new_id,
            "name": f"{doc.get('name')} 副本",
            "savedAt": _now(),
            "state": doc.get("state", {}),
        }
        client.insert_document(ENG_TEMPLATES, new_doc)
        _bump_revision(client, DOMAIN_SYNC_TEMPLATES)
        return CompactJsonResponse({"ok": True, "data": new_doc}, status=201)
    except (CloudBaseConfigError, CloudBaseAPIError) as exc:
        return _handle_cloudbase_error(exc)


def standalone_templates(request):
    """单项工作模板库：模板列表（公开）/ 新建模板。

    每模板一文档（_id = uuid），字段 = { id, name, savedAt, state }，
    state 即 StandalonePrepSheet（不含 base，base 属项目特有信息）。
    """
    client = get_nosql_client()
    if request.method == "GET":
        try:
            result = client.list_documents(STANDALONE_TEMPLATES, limit=200)
            docs = None
            if isinstance(result, dict):
                for key in ("list", "data", "documents", "items"):
                    if isinstance(result.get(key), list):
                        docs = result[key]
                        break
            return CompactJsonResponse({"ok": True, "data": docs or []})
        except (CloudBaseConfigError, CloudBaseAPIError) as exc:
            return _handle_cloudbase_error(exc)

    try:
        body = _json_body(request)
    except ValueError as exc:
        return _error(str(exc), 400)
    name = str(body.get("name", "")).strip()
    if not name:
        return _error("name 不能为空", 400)
    state = body.get("state")
    if not isinstance(state, dict):
        return _error("state 必须是对象", 400)
    now = _now()
    doc_id = uuid4().hex
    document = {"_id": doc_id, "id": doc_id, "name": name, "savedAt": now, "state": state}
    try:
        client.insert_document(STANDALONE_TEMPLATES, document)
        _bump_revision(client, DOMAIN_SYNC_TEMPLATES)
        return CompactJsonResponse({"ok": True, "data": document}, status=201)
    except (CloudBaseConfigError, CloudBaseAPIError) as exc:
        return _handle_cloudbase_error(exc)


@require_http_methods(["GET", "PUT", "DELETE"])
def standalone_template_detail(request, template_id):
    """模板详情 / 整体替换（name + state）/ 删除。"""
    client = get_nosql_client()
    try:
        if request.method == "GET":
            return CompactJsonResponse({"ok": True, "data": client.get_document(STANDALONE_TEMPLATES, template_id)})
        if request.method == "DELETE":
            client.delete_document(STANDALONE_TEMPLATES, template_id)
            _bump_revision(client, DOMAIN_SYNC_TEMPLATES)
            return CompactJsonResponse({"ok": True})
        body = _json_body(request)
        name = str(body.get("name", "")).strip()
        state = body.get("state")
        if not name:
            return _error("name 不能为空", 400)
        if not isinstance(state, dict):
            return _error("state 必须是对象", 400)
        client.update_document(
            STANDALONE_TEMPLATES,
            template_id,
            {"$set": {"name": name, "state": state, "savedAt": _now()}},
            upsert=True,
        )
        _bump_revision(client, DOMAIN_SYNC_TEMPLATES)
        return CompactJsonResponse({"ok": True})
    except ValueError as exc:
        return _error(str(exc), 400)
    except (CloudBaseConfigError, CloudBaseAPIError) as exc:
        return _handle_cloudbase_error(exc)


@require_http_methods(["POST"])
def standalone_template_duplicate(request, template_id):
    """复制模板：新 uuid + 名称后缀「副本」。"""
    client = get_nosql_client()
    try:
        doc = client.get_document(STANDALONE_TEMPLATES, template_id)
        if not isinstance(doc, dict) or not doc.get("name"):
            return _error("模板不存在", 404)
        new_id = uuid4().hex
        new_doc = {
            "_id": new_id,
            "id": new_id,
            "name": f"{doc.get('name')} 副本",
            "savedAt": _now(),
            "state": doc.get("state", {}),
        }
        client.insert_document(STANDALONE_TEMPLATES, new_doc)
        _bump_revision(client, DOMAIN_SYNC_TEMPLATES)
        return CompactJsonResponse({"ok": True, "data": new_doc}, status=201)
    except (CloudBaseConfigError, CloudBaseAPIError) as exc:
        return _handle_cloudbase_error(exc)
