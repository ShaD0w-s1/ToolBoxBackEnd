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
from .polling import PollingPayloadError
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
    return JsonResponse(payload, status=status)


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


def _bump_revision(client: CloudBaseNoSQLClient) -> None:
    """递增单计数器文档 seq，作为全局单调递增的 revision 来源。

    每次写操作成功后调用。计数器文档尚不存在时自动创建（seq=1）。
    变更日志失败不影响主写入（最坏情况 poll 检测不到该次变更，退化为手动刷新）。
    """
    try:
        result = client.update_document(
            CHANGE_LOG, REVISION_DOC_ID, {"$inc": {"seq": 1}}, upsert=False
        )
        if isinstance(result, dict) and result.get("matched") == 0:
            client.insert_document(CHANGE_LOG, {"_id": REVISION_DOC_ID, "seq": 1})
    except (CloudBaseAPIError, CloudBaseConfigError):
        pass


def _read_revision(client: CloudBaseNoSQLClient) -> str:
    """读取计数器文档的 seq，作为当前 revision。文档不存在视为 0。"""
    try:
        doc = client.get_document(CHANGE_LOG, REVISION_DOC_ID)
    except CloudBaseAPIError as exc:
        if exc.status == 404:
            return "0"
        raise
    seq = int(doc.get("seq", 0)) if isinstance(doc, dict) else 0
    return str(seq)


def index(request):
    return JsonResponse(
        {
            "message": "Django API is running",
            "path": request.path,
            "timestamp": _now(),
        }
    )


@ensure_csrf_cookie
@require_http_methods(["GET"])
def csrf(request):
    return JsonResponse({"ok": True, "csrf_token": get_token(request)})


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
        return JsonResponse({"ok": True, "verified": True, "token": token, "expires_in": AIRNAV_TOKEN_TTL})
    _airnav_fail(ip)
    return JsonResponse({"ok": False, "verified": False, "error": "AIRNAV 密码错误"}, status=403)


@require_http_methods(["GET"])
def cloudbase_status(request):
    # 只返回是否完成配置，绝不把 API Key 内容发送给客户端。
    api_key = os.getenv("CLOUDBASE_API_KEY", "")
    return JsonResponse(
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
    """返回所有用户可见业务数据的稳定修订值（来自单计数器文档，成本 O(1) 读）。"""
    try:
        revision = _read_revision(get_nosql_client())
        # 首次不带 revision 只建立基线；之后仅在值不同时报告 changed。
        previous = request.GET.get("revision", "").strip().strip('"')
        response = JsonResponse(
            {
                "ok": True,
                "revision": revision,
                "changed": bool(previous and previous != revision),
                "poll_after_ms": 5000,
            }
        )
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
            if isinstance(result, dict) and not isinstance(result.get("data"), list):
                for key in ("list", "documents", "items"):
                    if isinstance(result.get(key), list):
                        result = {**result, "data": result[key]}
                        break
            return JsonResponse({"ok": True, **result})

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
        _bump_revision(client)
        return JsonResponse({"ok": True, "data": document}, status=201)
    except ValueError as exc:
        return _error(str(exc), 400)
    except (CloudBaseConfigError, CloudBaseAPIError) as exc:
        return _handle_cloudbase_error(exc)


@require_http_methods(["GET", "PATCH", "DELETE"])
def project_detail(request, project_id):
    try:
        client = get_nosql_client()
        if request.method == "GET":
            return JsonResponse(
                {"ok": True, "data": client.get_document(PROJECTS, project_id)}
            )
        if request.method == "DELETE":
            result = client.delete_document(PROJECTS, project_id)
            _bump_revision(client)
            return JsonResponse({"ok": True, "result": result})

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
                current_version = None
                try:
                    current = client.get_document(PROJECTS, project_id)
                    if isinstance(current, dict):
                        current_version = int(current.get("version", 0))
                except CloudBaseAPIError:
                    pass
                return JsonResponse(
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
        _bump_revision(client)
        return JsonResponse({"ok": True, "result": result})
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
            return JsonResponse(
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
        _bump_revision(client)
        return JsonResponse({"ok": True, "result": result})
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
            return JsonResponse(
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
        _bump_revision(client)
        return JsonResponse({"ok": True, "result": result})
    except ValueError as exc:
        return _error(str(exc), 400)
    except (CloudBaseConfigError, CloudBaseAPIError) as exc:
        return _handle_cloudbase_error(exc)


@require_http_methods(["GET", "PUT"])
def tool_cart(request):
    try:
        client = get_nosql_client()
        if request.method == "GET":
            return JsonResponse(
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
        _bump_revision(client)
        return JsonResponse({"ok": True, "result": result})
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
            return JsonResponse(
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
        _bump_revision(client)
        return JsonResponse({"ok": True, "result": result})
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
            # 飞机信息（机号/FSN/发动机等）属敏感数据：读取需持有 AIRNAV 短期 token。
            if lib_key == "aircraft_info":
                token = request.META.get("HTTP_X_AIRNAV_TOKEN", "")
                if not _verify_airnav_token(token):
                    return _error("需要 AIRNAV 授权才能读取飞机信息", 403)
            return JsonResponse(
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
        _bump_revision(client)
        return JsonResponse({"ok": True, "result": result})
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
    return JsonResponse(
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
        _bump_revision(client)

        return JsonResponse(
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
        return JsonResponse({"ok": True, "data": numbers})
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
            return JsonResponse({"ok": True, "data": None})
        try:
            client = get_nosql_client()
            rows = _read_std_rows(client, AIRCRAFT_INFO)
            for row in rows:
                if str(row.get("飞机号") or "").strip().upper() == reg:
                    return JsonResponse({"ok": True, "data": row})
            return JsonResponse({"ok": True, "data": None})
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
        _bump_revision(client)
        return JsonResponse({"ok": True, "data": new_row, "updated": updated})
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
            return JsonResponse({"ok": True, "data": docs or []})

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
        _bump_revision(client)
        return JsonResponse({"ok": True, "data": document}, status=201)
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
            _bump_revision(client)
            return JsonResponse({"ok": True})
        cloud_id = doc.get("cloudObjectId")
        if not cloud_id:
            return _error("该记录缺少 fileid", 400)
        url = _get_storage_client().get_download_url(str(cloud_id))
        return JsonResponse({"ok": True, "data": {"downloadUrl": url, "fileName": doc.get("fileName", "")}})
    except (CloudBaseConfigError, CloudBaseAPIError) as exc:
        return _handle_cloudbase_error(exc)


# ============ 准备单附件（换发 / 单独项目「附件卡片」）============
# 文件实体存云存储（object key = uuid hex，无斜杠）；元数据（名称/日期/fileKey）由前端写入项目/模板的
# attachments 引用列表并随保存同步（模板保存/调取全量透传）。删除附件 = 前端移除引用（懒清理，对象共享）。
PREP_ATTACH_MAX_B64 = 8 * 1024 * 1024  # base64 中转上限（SCF/网关约束）→ 单个文件建议 ≤5-6MB


@require_http_methods(["POST"])
def prep_attachment_upload(request):
    """上传准备单附件：body { fileName, content(base64) } → 云存储对象，返回引用元数据。"""
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
    storage = _get_storage_client()
    cloud_object_id = storage.upload_bytes(file_key, file_bytes, content_type="application/octet-stream")
    return JsonResponse(
        {"ok": True, "data": {"fileKey": cloud_object_id, "name": file_name, "size": len(file_bytes), "uploadedAt": _now()}},
        status=201,
    )


@require_http_methods(["GET", "DELETE"])
def prep_attachment_detail(request, file_key):
    """附件对象：GET 返回临时下载链接；DELETE 物理删除（懒清理/脚本用；前端「删除」仅移除引用）。"""
    try:
        storage = _get_storage_client()
        if request.method == "DELETE":
            storage.delete_object(file_key)
            return JsonResponse({"ok": True})
        url = storage.get_download_url(file_key)
        return JsonResponse({"ok": True, "data": {"downloadUrl": url}})
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
    _bump_revision(client)
    return JsonResponse({"ok": True, "data": {"name": name, "last_seen": now, "login_count": login_count}})


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
        return JsonResponse({"ok": True, "data": accounts})
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
        return JsonResponse({"ok": True, "data": {"count": count}})
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
        return JsonResponse({"ok": True})

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
    return JsonResponse({"ok": True, "data": active})


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
            return JsonResponse({"ok": True, "data": docs or []})
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
        _bump_revision(client)
        return JsonResponse({"ok": True, "data": document}, status=201)
    except (CloudBaseConfigError, CloudBaseAPIError) as exc:
        return _handle_cloudbase_error(exc)


@require_http_methods(["GET", "PUT", "DELETE"])
def eng_template_detail(request, template_id):
    """模板详情 / 整体替换（name + state）/ 删除。"""
    client = get_nosql_client()
    try:
        if request.method == "GET":
            return JsonResponse({"ok": True, "data": client.get_document(ENG_TEMPLATES, template_id)})
        if request.method == "DELETE":
            client.delete_document(ENG_TEMPLATES, template_id)
            _bump_revision(client)
            return JsonResponse({"ok": True})
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
        _bump_revision(client)
        return JsonResponse({"ok": True})
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
        _bump_revision(client)
        return JsonResponse({"ok": True, "data": new_doc}, status=201)
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
            return JsonResponse({"ok": True, "data": docs or []})
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
        _bump_revision(client)
        return JsonResponse({"ok": True, "data": document}, status=201)
    except (CloudBaseConfigError, CloudBaseAPIError) as exc:
        return _handle_cloudbase_error(exc)


@require_http_methods(["GET", "PUT", "DELETE"])
def standalone_template_detail(request, template_id):
    """模板详情 / 整体替换（name + state）/ 删除。"""
    client = get_nosql_client()
    try:
        if request.method == "GET":
            return JsonResponse({"ok": True, "data": client.get_document(STANDALONE_TEMPLATES, template_id)})
        if request.method == "DELETE":
            client.delete_document(STANDALONE_TEMPLATES, template_id)
            _bump_revision(client)
            return JsonResponse({"ok": True})
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
        _bump_revision(client)
        return JsonResponse({"ok": True})
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
        _bump_revision(client)
        return JsonResponse({"ok": True, "data": new_doc}, status=201)
    except (CloudBaseConfigError, CloudBaseAPIError) as exc:
        return _handle_cloudbase_error(exc)
