import json
import os
from datetime import datetime, timezone
from uuid import uuid4

from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

from .cloudbase_nosql import (
    CloudBaseAPIError,
    CloudBaseConfigError,
    CloudBaseNoSQLClient,
)


PROJECTS = "work_projects"
TEMPLATES = "aircraft_templates"
TOOL_CART = "tool_cart"
AIRCRAFT_TYPES = {"A320", "B787"}


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
    if isinstance(exc, CloudBaseConfigError):
        return _error(str(exc), 503)
    if isinstance(exc, CloudBaseAPIError):
        status = exc.status if 400 <= exc.status < 600 else 502
        return _error(str(exc), status, exc.details)
    raise exc


def get_nosql_client() -> CloudBaseNoSQLClient:
    return CloudBaseNoSQLClient()


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
    return JsonResponse({"ok": True})


@require_http_methods(["GET"])
def cloudbase_status(request):
    api_key = os.getenv("CLOUDBASE_API_KEY", "")
    return JsonResponse(
        {
            "ok": True,
            "env_id": os.getenv("CLOUDBASE_ENV_ID", ""),
            "configured": bool(api_key and not api_key.startswith("replace-")),
            "collections": [PROJECTS, TEMPLATES, TOOL_CART],
        }
    )


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
            return JsonResponse({"ok": True, **result})

        body = _json_body(request)
        name = str(body.get("name", "")).strip()
        if not name:
            return _error("name 不能为空", 400)
        aircraft_type = str(body.get("aircraft_type", "A320")).upper()
        if aircraft_type not in AIRCRAFT_TYPES:
            return _error("aircraft_type 只支持 A320 或 B787", 400)

        now = _now()
        document = {
            "_id": uuid4().hex,
            "name": name,
            "aircraft_type": aircraft_type,
            "team": str(body.get("team", "")).strip(),
            "sections": body.get("sections", []),
            "use_tool_cart": bool(body.get("use_tool_cart", False)),
            "created_at": now,
            "updated_at": now,
            "version": 1,
        }
        if not isinstance(document["sections"], list):
            return _error("sections 必须是数组", 400)
        client.insert_document(PROJECTS, document)
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
            return JsonResponse(
                {"ok": True, "result": client.delete_document(PROJECTS, project_id)}
            )

        body = _json_body(request)
        allowed = {
            "name",
            "aircraft_type",
            "team",
            "sections",
            "use_tool_cart",
        }
        updates = {key: value for key, value in body.items() if key in allowed}
        if not updates:
            return _error("没有可更新字段", 400)
        if "aircraft_type" in updates:
            updates["aircraft_type"] = str(updates["aircraft_type"]).upper()
            if updates["aircraft_type"] not in AIRCRAFT_TYPES:
                return _error("aircraft_type 只支持 A320 或 B787", 400)
        if "sections" in updates and not isinstance(updates["sections"], list):
            return _error("sections 必须是数组", 400)
        updates["updated_at"] = _now()
        result = client.update_document(
            PROJECTS,
            project_id,
            {"$set": updates, "$inc": {"version": 1}},
        )
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
            upsert=True,
        )
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
            upsert=True,
        )
        return JsonResponse({"ok": True, "result": result})
    except ValueError as exc:
        return _error(str(exc), 400)
    except (CloudBaseConfigError, CloudBaseAPIError) as exc:
        return _handle_cloudbase_error(exc)
