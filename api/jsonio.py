"""JSON 响应统一使用紧凑序列化与非 ASCII 直出。

Django ``JsonResponse`` 默认 ``separators=(", ", ": ")`` 且 ``ensure_ascii=True``，
在中文为主的业务数据上开销很大。实测 ``/api/projects/?limit=100`` 单次响应
1227.7 KB 中有：

* ``\\uXXXX`` 转义 282.6 KB（96,470 个转义符，占 23.0%）
* 分隔符空格 53.4 KB（占 4.4%）

换成紧凑 + ``ensure_ascii=False`` 后这部分全部消失，且不影响 JSON 语义
（RFC 8259 规定 JSON 文本默认按 UTF-8 解析）。
"""

from __future__ import annotations

from django.http import JsonResponse


class CompactJsonResponse(JsonResponse):
    """紧凑 JSON 响应：无冗余空白、中文直出（不转义为 ``\\uXXXX``）。"""

    def __init__(self, data, **kwargs):
        # 调用方未显式指定序列化参数时套用紧凑配置，避免覆盖个别定制需求。
        kwargs.setdefault(
            "json_dumps_params",
            {"ensure_ascii": False, "separators": (",", ":")},
        )
        super().__init__(data, **kwargs)
