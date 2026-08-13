"""ToolBox 的 HTTP API 视图。

视图只处理输入校验和响应映射；CloudBase 协议细节集中在 cloudbase_nosql，
轮询修订计算集中在 polling，避免业务入口承担过多职责。
"""

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
from .polling import PollingPayloadError
from .workcard_filter import (
    apply_material_filter,
    apply_tool_filter,
    apply_work_card_list,
    collect_apu_workcard_names,
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
AIRCRAFT_TYPES = {"A320", "B787"}

# AIRNAV 短期授权 token 有效期（秒）。
AIRNAV_TOKEN_TTL = 30 * 60
# 暴力破解限流：失败次数与冷却时间（内存态，单实例内有效，作为第一道防线）。
_AIRNAV_RATE: dict[str, dict] = {}

# 工作项目可选类型；空字符串表示历史遗留项目（仅有工具清单）。
PROJECT_TYPES = {"A检", "零散", "换发", "换APU", "单独项目"}

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
            return _error("type 只支持 A检/零散/换发/换APU/单独项目", 400)

        now = _now()
        document = {
            # 客户端生成 ID 会让失败重试更复杂，因此由可信后端统一生成。
            "_id": uuid4().hex,
            "name": name,
            "aircraft_type": aircraft_type,
            "type": project_type,
            "team": str(body.get("team", "")).strip(),
            "sections": body.get("sections", []),
            "use_tool_cart": bool(body.get("use_tool_cart", False)),
            # A检项目的两个子结构；非 A检项目这两个字段保持为空。
            "prep_sheet": body.get("prep_sheet", {}),
            "workcard_assignment": body.get("workcard_assignment", {}),
            # 「单独项目」的两个子结构；非单独项目保持为空。
            "standalone_prep_sheet": body.get("standalone_prep_sheet", {}),
            "material_list": body.get("material_list", []),
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
            "sections",
            "use_tool_cart",
            "prep_sheet",
            "workcard_assignment",
            "standalone_prep_sheet",
            "material_list",
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
                return _error("type 只支持 A检/零散/换发/换APU/单独项目", 400)
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
                return _error(
                    "数据已被他人修改，请刷新后重试",
                    409,
                    {"current_version": current_version, "expected_version": expected_version},
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

        aircraft_type = str(body.get("aircraft_type") or project_doc.get("aircraft_type") or "A320").upper()
        if aircraft_type not in AIRCRAFT_TYPES:
            aircraft_type = "A320"

        cards = body.get("cards")
        full_mode = isinstance(cards, list) and len(cards) > 0
        if full_mode:
            workcard_rows = _read_std_rows(client, WORKCARD_320)
            aircraft_rows = _read_std_rows(client, AIRCRAFT_INFO)
            # 1) 工卡分配
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
        engine = str((prep_sheet.get("base") or {}).get("发动机", ""))

        # 2) 工具清单筛选（data → sections）
        project_sections = project_doc.get("sections") if isinstance(project_doc.get("sections"), list) else []
        tool_sections, tool_deleted, tool_added = apply_tool_filter(
            project_sections, tool_lib, names, apu_names, engine
        )

        # 3) 航材清单筛选（material_list → sections）
        material_sections = project_doc.get("material_list") if isinstance(project_doc.get("material_list"), list) else []
        material_sections, mat_deleted, mat_added = apply_material_filter(
            material_sections, material_lib, names, apu_names, engine
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
