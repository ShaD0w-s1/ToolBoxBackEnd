"""同步/压缩改动的本地自测（无需联网、无需 CloudBase 凭据）。

覆盖本次「同步协议瘦身」的全部关键路径，目的是在部署前把逻辑问题挡下来：

* 压缩：编码协商（q 值 / 通配 / 拒绝）、阈值、幂等、失败回落
* 响应体：紧凑序列化（无空白、中文不转义）、列表响应去重
* 分域：域映射完整性、非法域降级、各域缺失时补 0

用法（需要 django；建议用项目隔离 env）::

    python scripts/selftest_sync.py
"""

from __future__ import annotations

import gzip
import io
import json
import os
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cloudrun.settings_scf")

import django  # noqa: E402

django.setup()

from django.http import HttpResponse  # noqa: E402
from django.test import RequestFactory  # noqa: E402

from api import compression as comp  # noqa: E402
from api import views  # noqa: E402
from api.jsonio import CompactJsonResponse  # noqa: E402
from api.sync_domains import SYNC_DOMAINS  # noqa: E402

PASSED = 0
FAILED: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  ✓ {label}")
    else:
        FAILED.append(f"{label} {detail}".strip())
        print(f"  ✗ {label} {detail}".strip())


# ————————————————————— 1. 编码协商 —————————————————————
print("\n[1] 编码协商 pick_encoding")
# 先测「严格协商」模式（auto）下的 RFC 语义。
comp.FORCE_ENCODING = "auto"
cases = [
    ("", None, "未声明且不兜底 → 不压缩"),
    ("identity", None, "仅 identity 且不兜底 → 不压缩"),
    ("gzip", "gzip", "仅 gzip"),
    ("br", "br", "仅 br"),
    ("gzip, deflate, br", "br", "同 q 值 → 优先 brotli（体积更小）"),
    ("gzip;q=1.0, br;q=0.5", "gzip", "gzip 的 q 更高 → 选 gzip"),
    ("br;q=1.0, gzip;q=0.1", "br", "br 的 q 更高 → 选 br"),
    ("br;q=0, gzip", "gzip", "明确拒绝 br → 回落 gzip"),
    ("*", "gzip", "通配 → gzip（br 不在通配范围）"),
    ("deflate", None, "只有 deflate → 不支持则不压缩"),
]
for header, expected, note in cases:
    actual = comp.pick_encoding(header)
    check(f"AE={header!r} → {actual!r}（{note}）", actual == expected, f"期望 {expected!r}")

# brotli 缺失时必须回落 gzip，而不是抛错。
saved_available = comp.BROTLI_AVAILABLE
saved_brotli = comp._brotli
comp.BROTLI_AVAILABLE = False
comp._brotli = None
check(
    "brotli 不可用时 'br, gzip' 回落 gzip",
    comp.pick_encoding("br, gzip") == "gzip",
)
check("brotli 不可用时 'br' 返回 None", comp.pick_encoding("br") is None)
comp.BROTLI_AVAILABLE = saved_available
comp._brotli = saved_brotli

print("\n[1b] 兜底策略（网关把 Accept-Encoding 改写为 identity 的场景）")
comp.FORCE_ENCODING = "gzip"
check("identity → gzip（云端网关场景）", comp.pick_encoding("identity") == "gzip")
check("未声明 → gzip", comp.pick_encoding("") == "gzip")
check("客户端声明 br 时仍优先 br（真的能协商到就选更小的）", comp.pick_encoding("gzip, deflate, br") == "br")
comp.FORCE_ENCODING = "br"
check("兜底=br 时 identity → br", comp.pick_encoding("identity") == "br")
comp.BROTLI_AVAILABLE = False
check("兜底=br 但 brotli 缺失 → 仍回落 gzip", comp.pick_encoding("identity") == "gzip")
comp.BROTLI_AVAILABLE = saved_available
comp.FORCE_ENCODING = "off"
check("兜底=off 时即使声明 br 也不压缩", comp.pick_encoding("gzip, deflate, br") is None)
comp.FORCE_ENCODING = "gzip"

# ————————————————————— 2. 压缩往返 —————————————————————
print("\n[2] 压缩往返 compress_body")
sample = json.dumps({"rows": [{"工卡号": f"49-{i:04d}", "工卡名": "检查" * 20} for i in range(200)]}, ensure_ascii=False).encode("utf-8")
for enc, decode in (("gzip", gzip.decompress), ("br", __import__("brotli").decompress)):
    body = comp.compress_body(sample, enc)
    check(f"{enc} 压缩成功", body is not None)
    if body:
        check(f"{enc} 往返一致", decode(body) == sample)
        check(
            f"{enc} 确实变小（{len(sample)}→{len(body)} 字节）",
            len(body) < len(sample),
        )
check("未知编码返回 None（调用方回退）", comp.compress_body(sample, "deflate") is None)

# ————————————————————— 3. 中间件端到端 —————————————————————
print("\n[3] 中间件端到端响应压缩")
factory = RequestFactory()
big_payload = {"ok": True, "data": [{"name": f"项目{i}", "note": "备注" * 30} for i in range(60)]}


def make_view(status=200, content_type=None, content=None, streaming=False):
    def view(request):
        if streaming:
            return HttpResponse(iter([b"x"] * 10), content_type="application/json")
        if content is not None:
            return HttpResponse(content, content_type=content_type or "text/plain")
        response = CompactJsonResponse(big_payload, status=status)
        if content_type:
            response["Content-Type"] = content_type
        return response

    return view


def run(view, accept_encoding=None, method="GET"):
    request = factory.generic(method, "/api/projects/")
    if accept_encoding is not None:
        request.META["HTTP_ACCEPT_ENCODING"] = accept_encoding
    return comp.ResponseCompressionMiddleware(view)(request)


def has_vary(response, token="Accept-Encoding"):
    return token in [p.strip() for p in (response.get("Vary") or "").split(",")]


response = run(make_view(), "gzip, br")
check("大 JSON 被压缩", response.get("Content-Encoding") in {"br", "gzip"}, f"实际 {response.get('Content-Encoding')!r}")
check("设置了 Vary: Accept-Encoding", has_vary(response))
check(
    "Content-Length 与压缩后字节一致",
    int(response["Content-Length"]) == len(response.content),
)
decoded = json.loads(
    (__import__("brotli").decompress if response["Content-Encoding"] == "br" else gzip.decompress)(response.content).decode("utf-8")
)
check("压缩后仍是合法 JSON 且内容一致", decoded == big_payload)

plain = run(make_view())
check("未声明 Accept-Encoding → 按兜底策略压缩(gzip)", plain.get("Content-Encoding") == "gzip", f"实际 {plain.get('Content-Encoding')!r}")

identity = run(make_view(), "identity")
check("网关改写为 identity → 仍按兜底策略压缩(gzip)", identity.get("Content-Encoding") == "gzip", f"实际 {identity.get('Content-Encoding')!r}")

comp.FORCE_ENCODING = "auto"
strict = run(make_view(), "identity")
check("兜底=auto 时 identity → 不压缩（严格 RFC 语义）", strict.get("Content-Encoding") is None)
comp.FORCE_ENCODING = "gzip"

tiny = run(make_view(content=b'{"ok":true,"revision":"1"}', content_type="application/json"), "gzip, br")
check("小于阈值 → 不压缩（poll 场景）", tiny.get("Content-Encoding") is None)

binary = run(make_view(content=b"\x89PNG" + b"\x00" * 3000, content_type="image/png"), "gzip, br")
check("非文本类型 → 不压缩", binary.get("Content-Encoding") is None)

already = HttpResponse(b"x" * 3000, content_type="application/json")
already["Content-Encoding"] = "gzip"
already_wrapped = comp.ResponseCompressionMiddleware(lambda r: already)(factory.get("/"))
check("已编码响应 → 幂等不重复压缩", already_wrapped["Content-Encoding"] == "gzip")

streaming = run(make_view(streaming=True), "gzip, br")
check("流式响应 → 跳过压缩", streaming.get("Content-Encoding") is None)

head_resp = run(make_view(), "gzip, br", method="HEAD")
check("HEAD → 跳过压缩", head_resp.get("Content-Encoding") is None)

# 压缩函数异常时必须回退原响应（而不是 500）。
saved_compress = comp.compress_body
comp.compress_body = lambda body, encoding: None
fallback = run(make_view(), "gzip, br")
check("压缩失败 → 回退未压缩响应", fallback.get("Content-Encoding") is None)
comp.compress_body = saved_compress


def boom(body, encoding):
    raise RuntimeError("模拟压缩崩溃")


comp.compress_body = boom
try:
    crashed = run(make_view(), "gzip, br")
    check("压缩抛异常 → 中间件仍返回响应（不 500）", crashed.status_code == 200)
except Exception as exc:  # pragma: no cover
    check("压缩抛异常 → 中间件仍返回响应（不 500）", False, f"抛出 {exc!r}")
comp.compress_body = saved_compress

# 4xx 小响应不压缩；大 4xx 也不应因压缩而损坏语义。
err = run(make_view(status=400), "gzip, br")
check("4xx 大响应可压缩且状态码不变", err.status_code == 400)

# ————————————————————— 4. 紧凑序列化 —————————————————————
print("\n[4] 紧凑 JSON 序列化")
raw = CompactJsonResponse({"ok": True, "名称": "常州C转A", "n": 1}).content.decode("utf-8")
check("中文不转义为 \\uXXXX", "\\u" not in raw, f"实际 {raw[:60]}")
check("无分隔符空格", '": ' not in raw and '", ' not in raw, f"实际 {raw[:60]}")
check("仍是合法 JSON", json.loads(raw) == {"ok": True, "名称": "常州C转A", "n": 1})

sample_cn = {"rows": [{"工卡号": "49-0001", "工卡名称": "发动机区域检查"}] * 20}
escaped_len = len(json.dumps(sample_cn, ensure_ascii=True).encode("utf-8"))
compact_len = len(CompactJsonResponse(sample_cn).content)
check(
    f"对照：默认 ensure_ascii 更膨胀（{escaped_len} → {compact_len} 字节）",
    escaped_len > compact_len,
    f"转义 {escaped_len} 未解析 {compact_len}",
)

# ————————————————————— 5. 列表响应去重 —————————————————————
print("\n[5] 列表响应去重 _normalize_list_payload")
rows = [{"_id": "a", "name": "x"}, {"_id": "b", "name": "y"}]
cloudbase_style = {"offset": 0, "limit": 100, "list": rows}
normalized = views._normalize_list_payload(cloudbase_style)
check("保留 data 数组", normalized.get("data") == rows)
check("删除重复的 list 键", "list" not in normalized)
check("保留分页元信息", normalized.get("offset") == 0 and normalized.get("limit") == 100)

already_data = {"data": rows, "total": 2}
check("已有 data 且无其他数组键 → 原样", views._normalize_list_payload(already_data) == already_data)

both = {"data": rows, "list": rows}
both_norm = views._normalize_list_payload(both)
check("data 与 list 同时存在 → 只留 data", "list" not in both_norm and both_norm["data"] == rows)

empty = {}
check("空字典 → 原样返回", views._normalize_list_payload(empty) == {})
check("非字典 → 包成 data", views._normalize_list_payload(rows) == {"data": rows})

# ————————————————————— 6. 分域修订 —————————————————————
print("\n[6] 分域修订 _bump_revision / _read_revision")
calls: list[tuple] = []


class FakeClient:
    """记录写入参数、模拟计数器文档已存在。"""

    def __init__(self, doc=None, matched=1):
        self.doc = doc
        self.matched = matched

    def update_document(self, collection, doc_id, patch, upsert=False):
        calls.append((collection, doc_id, patch, upsert))
        return {"matched": self.matched}

    def insert_document(self, collection, document):
        calls.append(("insert", collection, document))
        return {}

    def get_document(self, collection, doc_id):
        if self.doc is None:
            # 注意构造签名是 (status, message, details=None)。
            raise views.CloudBaseAPIError(404, "missing")
        return self.doc


views._bump_revision(FakeClient(), "projects")
patch = calls[-1][2]
check("$inc 同时递增全局 seq", patch["$inc"].get("seq") == 1)
check("$inc 递增对应域计数", patch["$inc"].get("domains.projects") == 1)

calls.clear()
views._bump_revision(FakeClient(), "control")
check("control 域也被正确记录", calls[-1][2]["$inc"].get("domains.control") == 1)

calls.clear()
try:
    views._bump_revision(FakeClient(), "not_a_domain")
    check("非法域不抛异常（避免写入成功后变 500）", True)
except Exception as exc:
    check("非法域不抛异常（避免写入成功后变 500）", False, f"抛出 {exc!r}")
check("非法域仍递增全局 seq（退化为旧行为，不漏同步）", calls[-1][2]["$inc"].get("seq") == 1)
check("非法域不写入 domains 计数", not any(k.startswith("domains.") for k in calls[-1][2]["$inc"]))

calls.clear()
views._bump_revision(FakeClient(matched=0), "cart")
check("计数器文档不存在时自动创建", calls[-1][0] == "insert")
check("创建的文档包含该域种子值", calls[-1][2].get("domains") == {"cart": 1})

full_doc = {"seq": 42, "domains": {"projects": 7, "cart": 3}}
seq, domains = views._read_revision(FakeClient(doc=full_doc))
check("读取全局 seq", seq == "42")
check("返回全部已知域（缺失补 0）", set(domains) == set(SYNC_DOMAINS), f"实际 {sorted(domains)}")
check("已有域值原样返回", domains["projects"] == "7" and domains["cart"] == "3")
check("缺失域补 0", domains["templates"] == "0")

seq_missing, domains_missing = views._read_revision(FakeClient(doc=None))
check("文档不存在 → seq=0 且所有域为 0", seq_missing == "0" and all(v == "0" for v in domains_missing.values()))
check("文档不存在不抛异常", True)

bad_doc = {"seq": 5, "domains": "不是字典"}
check("domains 字段类型异常 → 全 0 兜底", all(v == "0" for v in views._read_revision(FakeClient(doc=bad_doc))[1].values()))

# ————————————————————— 7. 列表轻量投影（档 2′） —————————————————————
print("\n[7] 列表轻量投影 _project_summary / _project_max_item_id")
sample_doc = {
    "_id": "abc123",
    "name": "6572 常州C转A",
    "aircraft_type": "A320",
    "type": "A检",
    "team": "三车间",
    "execute_date": "20260901",
    "created_at": "2026-09-01T00:00:00Z",
    "updated_at": "2026-09-02T00:00:00Z",
    "version": 163,
    "sections": [
        {"name": "通用工具", "works": [
            {"name": "零散包工作", "items": [{"name": "注油枪", "quantity": 1}, {"name": "堵盖", "quantity": 1}]},
            {"name": "其他", "items": [{"name": "扳手", "quantity": 2}]},
        ]},
        {"name": "发动机", "works": [{"name": "检查", "items": [{"name": "内窥镜", "quantity": 1}]}]},
    ],
    "material_list": [
        {"name": "通用航材", "works": [{"name": "检查", "items": [{"name": "开口销", "quantity": 4}]}]},
    ],
    "prep_sheet": {"groups": [{"title": "x"}]},
    "workcard_assignment": {"sections": [{"n": 1}]},
    "standalone_prep_sheet": {"base": {"reg": "B-1234"}},
    "gantt_prep": {"charts": [{"cards": [{"id": "c1"}]}]},
}
summary = views._project_summary(sample_doc)
heavy_keys = ["sections", "prep_sheet", "workcard_assignment", "standalone_prep_sheet", "material_list", "gantt_prep"]
check("轻量投影不含任何重字段", not any(k in summary for k in heavy_keys), f"实际键 {sorted(summary)}")
for light in ("_id", "name", "aircraft_type", "type", "team", "execute_date", "updated_at", "version"):
    check(f"保留轻量字段 {light}", light in summary)
check(
    "max_item_id = 物品总条数（sections 4 + material_list 1）",
    summary["max_item_id"] == 5,
    f"实际 {summary.get('max_item_id')}",
)
# 用贴近真实的规模核对压缩比（真实项目平均 28.9 KB，重字段占 98%）。
big_doc = dict(sample_doc)
big_doc["sections"] = [
    {"name": f"部位{i}", "notes": "备注" * 10,
     "works": [{"name": "工作", "items": [{"name": f"工具{j}", "quantity": 1, "uid": f"u{i}-{j}", "partNo": "PN-123456"} for j in range(20)]}]}
    for i in range(10)
]
light_len = len(json.dumps(views._project_summary(big_doc), ensure_ascii=False))
full_len = len(json.dumps(big_doc, ensure_ascii=False))
check(
    f"真实规模下轻量投影占比 <5%（{full_len} → {light_len} 字节）",
    light_len < full_len * 0.05,
)

check("缺少 works 时不报错", views._project_max_item_id({"sections": [{"name": "x"}]}) == 0)
check("字段类型异常时不报错", views._project_max_item_id({"sections": "不是数组", "material_list": None}) == 0)
check("非字典文档返回空元数据", views._project_summary("字符串") == {})
check("无 _id 时补空串（避免前端拿到 undefined）", views._project_summary({"name": "x"})["_id"] == "")

# ————————————————————— 汇总 —————————————————————
print("\n" + "=" * 60)
if FAILED:
    print(f"✗ 失败 {len(FAILED)} 项（通过 {PASSED} 项）：")
    for item in FAILED:
        print("   -", item)
    sys.exit(1)
print(f"✓ 全部通过（{PASSED} 项）")
