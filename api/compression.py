"""响应压缩中间件：优先 brotli，回落 gzip。

设计取舍（改动时务必保持）
--------------------------
* **只压缩超过阈值的响应**。``/api/poll/`` 只有 74 字节，压缩后反而更大，
  还会白耗云函数 CPU；阈值设为 1 KB。
* **尊重 ``Accept-Encoding``**（含 q 值）。客户端没声明就不压缩，避免给不
  支持的客户端发去无法解码的响应。
* **brotli 是可选依赖**：导入失败时静默回落 gzip。缺一个可选包绝不能让整个
   API 起不来（云函数起不来会返回网关 443，全部端点不可用）。
* **任何压缩异常都退回原响应**：最坏情况只是「没压缩」。

实测收益（``/api/projects/?limit=100``，1227.7 KB 原始）：
brotli 57.2 KB（−95.3%）/ gzip 222.2 KB（−81.9%）。
"""

from __future__ import annotations

import gzip
import logging
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


def pick_encoding(header: str) -> str | None:
    """按 q 值选出编码；q 相同时优先 brotli（压缩率更高）。

    ``Accept-Encoding: *`` 视作接受 gzip（brotli 不在通配范围内，因为并非所有
    客户端都能解码 br）。
    """
    accepted = _parse_accept_encoding(header)
    candidates: list[tuple[float, int, str]] = []
    if BROTLI_AVAILABLE:
        quality = accepted.get("br", 0.0)
        if quality > 0:
            # 第二项是「同 q 值时的偏好序」，越大越优先。
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
    """压缩 JSON / 文本响应，支持 brotli 与 gzip，可安全回落到不压缩。"""

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
