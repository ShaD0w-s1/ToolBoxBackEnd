"""分域同步：把「业务写操作」与「客户端需要重拉的端点」显式绑定。

背景
----
早期所有写操作都递增同一个全局计数器，客户端只能判断「有东西变了」，于是
改一条 0.3 KB 的公告也会让所有人重拉 12 个端点（实测约 1.4 MB）。更糟的是
60 秒身份心跳同样递增该计数器，形成 N ×（N−1）× 1.4 MB / 分钟的无效放大。

引入分域计数后，``/api/poll/`` 会返回各域各自的修订值，客户端只重拉「真正
变化的域」对应的端点。

维护约定（重要）
----------------
1. 任何新增的写端点都必须归入一个域。否则它的变更对分域客户端不可见 ——
   这属于**静默漏同步**，不会报错。``scripts/check_sync_domains.py`` 会做
   完整性检查，务必在提交前运行。
2. ``DOMAIN_ENDPOINTS`` 是后端与前端共享的「域 → 端点」契约，前端
   ``src/composables/useToolbox.ts`` 的 ``DOMAIN_ENDPOINTS`` 必须与本表一致。
3. 值本身是字符串常量：前端原样比较，不做数值运算。
"""

from __future__ import annotations

# —— 域常量（写入方通过它们标记变更归属）——
DOMAIN_PROJECTS = "projects"
DOMAIN_TEMPLATES = "templates"
DOMAIN_STDLIBS = "stdlibs"
DOMAIN_CART = "cart"
DOMAIN_ANNOUNCEMENT = "announcement"
DOMAIN_CONTROL = "control"
DOMAIN_SYNC_TEMPLATES = "syncTemplates"

SYNC_DOMAINS: tuple[str, ...] = (
    DOMAIN_PROJECTS,
    DOMAIN_TEMPLATES,
    DOMAIN_STDLIBS,
    DOMAIN_CART,
    DOMAIN_ANNOUNCEMENT,
    DOMAIN_CONTROL,
    DOMAIN_SYNC_TEMPLATES,
)

# 域 → 客户端需要重拉的端点。
# 注意：control / syncTemplates 的端点由前端「按需拉取」（打开管控单面板、
# 打开模板库弹窗时才请求），不参与 loadRemote 的全量重拉，因此列表仍列出
# 以便文档与校验，但前端不会因为这两个域变化而重拉。
DOMAIN_ENDPOINTS: dict[str, tuple[str, ...]] = {
    DOMAIN_PROJECTS: ("/api/projects/",),
    DOMAIN_TEMPLATES: (
        "/api/templates/<aircraft_type>/",
        "/api/material-templates/<aircraft_type>/",
    ),
    DOMAIN_STDLIBS: (
        "/api/standard-libraries/<lib_key>/",
        "/api/aircraft-numbers/",
    ),
    DOMAIN_CART: ("/api/tool-cart/",),
    DOMAIN_ANNOUNCEMENT: ("/api/announcement/",),
    DOMAIN_CONTROL: ("/api/control-docs/",),
    DOMAIN_SYNC_TEMPLATES: (
        "/api/eng-templates/",
        "/api/standalone-templates/",
    ),
}

# 参与 loadRemote 全量重拉的域（前端 loadRemote 能按这些域裁剪请求）。
PULLABLE_DOMAINS: tuple[str, ...] = (
    DOMAIN_PROJECTS,
    DOMAIN_TEMPLATES,
    DOMAIN_STDLIBS,
    DOMAIN_CART,
    DOMAIN_ANNOUNCEMENT,
)


def is_valid_domain(domain: str) -> bool:
    """域名是否在受支持集合内。"""
    return domain in SYNC_DOMAINS
