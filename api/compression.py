"""响应压缩中间件：能协商时择优（br > gzip），协商不到时按显式策略兜底。

为什么需要「兜底策略」而不是只靠协商
------------------------------------
实测 CloudBase 网关会把客户端真实的 ``Accept-Encoding`` **改写为 ``identity``**
再转发给云函数，而网关自己并不压缩。也就是说在这条链路上：

* 云函数永远看不到浏览器的真实编码偏好（拿到的永远是 ``identity``）；
* 「仅按 Accept-Encoding 协商」的结果 = **永远不压缩**。

这类失效是静默的（HTTP 200、内容正确、只是体积没降），因此本模块把兜底策略做成
**显式配置**，并保证默认值在线上是可用的：

``RESPONSE_COMPRESSION_FORCE``
    * ``gzip``（默认）：客户端未声明可用编码时用 gzip。gzip 被浏览器、小程序、
      Node 等几乎所有 HTTP 客户端支持，是「稳妥」选项。
    * ``br``：同上但用 brotli，体积更小（实测 /api/projects/ gzip 102.8KB vs
      br 55.2KB），代价是依赖客户端支持 brotli，需确认后再启用。
    * ``auto``：严格按 Accept-Encoding 协商（RFC 语义最正确，但在当前网关下
      等于关闭压缩）。
    * ``off``：完全关闭压缩。

协商优先级（只要客户端**真的**声明了支持，就一定按声明来，此时才会用到 brotli）：
    客户端声明 br → br；声明 gzip → gzip；声明 identity/未声明 → 走兜底策略。

安全约束（改动时务必保持）
--------------------------
* 只压缩超过阈值的 JSON / 文本响应：``/api/poll/`` 只有百字节，压缩反而更大。
* brotli 是可选依赖，缺失或压缩失败时回落 gzip；再失败就退回原响应。
* 任何异常都退回原响应（压缩是纯优化），但**必须留日志**——静默失效会让排查
  变成猜谜（本项目就经历过一次）。
"""

from __future__ import annotations

import gzip
import logging
import os
import re

logger = logging.getLogger(__name__)

# brotli 为可选依赖：优先官方 `brotli`，其次 `brotlicffi`，都没有就只用 gzip。
_brotli = None
for _module_name in ("brotli", "brotlicffi"):
    try:
        _brotli = __import__(_module_name)
        break
    except Exception:  # pragma: no cover - 依赖缺失时回落 gzip
        _brotli = None

BROTLI_AVAILABLE = _brotli is not None

# 小于此体积不压缩：省下的字节还不如压缩头开销。
MIN_COMPRESS_BYTES = 1024
# brotli 质量 5：压缩率与耗时的平衡点，适合云函数这种短生命周期进程。
BROTLI_QUALITY = 5
# gzip 级别 6（默认档）：与 brotli 对比时保持较快的压缩速度。
GZIP_LEVEL = 6

_FORCE_RAW = (os.getenv("RESPONSE_COMPRESSION_FORCE") or "gzip").strip().lower()
if _FORCE_RAW not in {"gzip", "br", "auto", "off"}:
    logger.warning("RESPONSE_COMPRESSION_FORCE=%r 不是合法取值，回退 gzip", _FORCE_RAW)
    _FORCE_RAW = "gzip"
FORCE_ENCODING = _FORCE_RAW

# 只压缩文本类内容；二进制（图片等）压缩收益低且可能已被压缩过。
_COMPRESSIBLE_TYPE = re.compile(
    r"^(application/json|application/javascript|application/xml"
    r"|text/|image/svg\+xml)",
    re.IGNORECASE,
)


def _parse_accept_encoding(header: str) -> dict[str, float]:
    """解析 ``Accept-Encoding`` 为 ``{编码: q值}``；q=0 视为明确拒绝。"""
    accepted: dict[str, float] = {}
    for raw in header.split(","):
        token = raw.strip()
        if not token:
            continue
        name, _, params = token.partition(";")
        name = name.strip().lower()
        if not name:
            continue
        quality = 1.0
        for param in params.split(";"):
            key, _, value = param.partition("=")
            if key.strip().lower() == "q":
                try:
                    quality = float(value.strip())
                except ValueError:
                    quality = 0.0
        if quality > 0:
            accepted[name] = quality
    return accepted


def _by_quality(accepted: dict[str, float]) -> str | None:
    """按 q 值择优；q 相同时优先 brotli（压缩率更高）。未声明返回 None。"""
    candidates: list[tuple[float, int, str]] = []
    if BROTLI_AVAILABLE:
        quality = accepted.get("br", 0.0)
        if quality > 0:
            candidates.append((quality, 1, "br"))
    for name in ("gzip", "x-gzip"):
        quality = accepted.get(name, 0.0)
        if quality > 0:
            candidates.append((quality, 0, "gzip"))
            break
    if not candidates and accepted.get("*", 0.0) > 0:
        candidates.append((accepted["*"], 0, "gzip"))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][2]


def pick_encoding(header: str) -> str | None:
    """选择响应编码。

    客户端真的声明了支持的编码时按声明择优（这才轮到 brotli）；
    只声明 ``identity``（含网关强制改写的情况）或未声明时，走兜底策略。
    返回 ``None`` 表示不压缩。
    """
    if FORCE_ENCODING == "off":
        return None
    accepted = _parse_accept_encoding(header)
    chosen = _by_quality(accepted)
    if chosen:
        return chosen
    # 到这里说明客户端（或中间网关）没有声明任何可用编码。
    if FORCE_ENCODING == "auto":
        return None
    if FORCE_ENCODING == "br":
        return "br" if BROTLI_AVAILABLE else "gzip"
    return "gzip"


def compress_body(body: bytes, encoding: str) -> bytes | None:
    """压缩响应体；失败返回 ``None`` 交由调用方退回原响应。"""
    try:
        if encoding == "br":
            if _brotli is None:
                return None
            return _brotli.compress(body, quality=BROTLI_QUALITY)
        if encoding == "gzip":
            # mtime=0 让输出可复现（不写入当前时间戳）。
            return gzip.compress(body, compresslevel=GZIP_LEVEL, mtime=0)
    except Exception:  # pragma: no cover - 压缩异常不阻断业务
        return None
    return None


def _append_vary(response, value: str) -> None:
    """向 ``Vary`` 追加字段，避免重复；缺少 ``Vary`` 会让中间缓存返回错误变体。"""
    existing = response.get("Vary")
    if not existing:
        response["Vary"] = value
        return
    parts = [part.strip() for part in existing.split(",") if part.strip()]
    if value not in parts:
        response["Vary"] = ", ".join([*parts, value])


class ResponseCompressionMiddleware:
    """压缩 JSON / 文本响应；任何异常都退回原响应，绝不阻断业务。"""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        try:
            return self._maybe_compress(request, response)
        except Exception:  # pragma: no cover - 压缩绝不阻断业务
            # 压缩是纯优化：任何异常都退回原响应，但**必须留下痕迹**。
            # 早期版本此处静默吞异常，导致「压缩整体失效」在线上毫无线索。
            logger.exception("响应压缩失败，已退回未压缩响应")
            return response

    def _maybe_compress(self, request, response):
        # 流式响应无法整体压缩；HEAD 只回头部，保持原样更简单可靠。
        if getattr(response, "streaming", False) or request.method == "HEAD":
            return response
        # 已经编码过（例如视图自己压缩）不重复处理。
        if response.get("Content-Encoding"):
            return response
        if not _COMPRESSIBLE_TYPE.match(response.get("Content-Type", "")):
            return response
        body = response.content
        if len(body) < MIN_COMPRESS_BYTES:
            return response
        encoding = pick_encoding(request.META.get("HTTP_ACCEPT_ENCODING", ""))
        if encoding is None:
            return response
        compressed = compress_body(body, encoding)
        if compressed is None and encoding != "gzip":
            # 首选的 brotli 不可用或失败时回落 gzip：宁可少压一点，
            # 也不要因为一个可选编码的问题而完全不压缩。
            logger.warning("brotli 压缩失败，已回落 gzip")
            encoding = "gzip"
            compressed = compress_body(body, encoding)
        # 压缩没变小就保留原响应（小响应或已高度可压缩的内容可能出现）。
        if compressed is None or len(compressed) >= len(body):
            return response
        response.content = compressed
        response["Content-Encoding"] = encoding
        response["Content-Length"] = str(len(compressed))
        _append_vary(response, "Accept-Encoding")
        return response
