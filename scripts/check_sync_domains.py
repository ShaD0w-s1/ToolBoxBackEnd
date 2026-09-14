"""同步域完整性检查（提交前必跑）。

为什么需要这个脚本
------------------
分域同步最大的风险是**静默漏同步**：如果某个写端点忘了归入域，它的变更对
分域客户端就永远不可见 —— 不报错、不提示，只是别人的改动看不到。这类问题
在功能测试里几乎不可能被发现，所以用静态检查把它挡在提交前。

检查项
------
1. ``_bump_revision`` 的域参数必须是 ``sync_domains.SYNC_DOMAINS`` 中的已知域。
2. 每个已知域都必须至少被一个调用点使用（避免定义了却没人用的孤儿域）。
3. 每个**可写端点**（含 POST / PUT / PATCH / DELETE 的视图）都必须有
   ``_bump_revision`` 调用，否则客户端收不到它的变更通知。
4. 后端 ``SYNC_DOMAINS`` 与前端 ``DOMAIN_ENDPOINTS`` 的域名集合必须一致
   （两侧契约失配同样会造成漏同步）。

用法
----
    python scripts/check_sync_domains.py

退出码 0 = 通过；1 = 发现问题（输出具体行号）。
"""

from __future__ import annotations

import io
import re
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
VIEWS_PATH = BACKEND_ROOT / "api" / "views.py"
DOMAINS_PATH = BACKEND_ROOT / "api" / "sync_domains.py"
FRONTEND_STORE = (
    BACKEND_ROOT.parent / "ToolBoxWebFrontEnd" / "src" / "composables" / "useToolbox.ts"
)

MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# 只读端点不受「必须有 bump」约束（GET/HEAD）。
_DECORATOR_RE = re.compile(r"@require_http_methods\(\[(.*?)\]\)")
_DEF_RE = re.compile(r"^def (\w+)\(")
_BUMP_RE = re.compile(r"_bump_revision\(\s*client\s*,\s*([A-Z_]+)\s*\)")
_DOMAIN_CONST_RE = re.compile(r"^(DOMAIN_\w+)\s*=\s*\"([^\"]+)\"")

# 有意「不广播」的可写端点：各自有独立的同步途径，或根本不属于同步数据。
# 这份清单必须显式列出，避免「忘了加 bump」被误当成有意为之。
NO_BROADCAST_ENDPOINTS = {
    "airnav_verify": "仅签发授权 token，不改变任何同步数据",
    "identity": "身份心跳：loadRemote 不含账号数据，广播等于 100% 无效重拉（档 0.3 已移除）",
    "editing": "编辑会话软锁由 /api/editing/ 逐秒轮询，不参与 revision 广播",
    "prep_attachment_file": "附件由页面按需拉取，不随主数据同步",
}


def _read(path: Path) -> str:
    if not path.exists():
        return ""
    return io.open(path, encoding="utf-8").read()


def _known_domains() -> dict[str, str]:
    """读取 sync_domains.py 的 ``DOMAIN_X = "value"`` 映射（常量名 → 域值）。"""
    domains: dict[str, str] = {}
    for line in _read(DOMAINS_PATH).splitlines():
        match = _DOMAIN_CONST_RE.match(line.strip())
        if match:
            domains[match.group(1)] = match.group(2)
    return domains


def _scan_views(domain_consts: dict[str, str]) -> tuple[list[str], list[str], list[str]]:
    """返回 (错误列表, 已使用的域常量, 无 bump 的可写端点)。"""
    lines = _read(VIEWS_PATH).splitlines()
    errors: list[str] = []
    used: list[str] = []
    writable_without_bump: list[str] = []

    # 只统计模块级函数：缩进的嵌套函数不会匹配 ``^def``，天然被排除。
    # 装饰器只作用于紧随其后的那个 def：``pending_methods`` 在 def 处「移交」
    # 给 current_methods 后立即清空。否则装饰器会泄漏到前/后一个函数上，
    # 把只读辅助函数误判成可写端点。
    pending_methods: list[str] = []
    current_func: str | None = None
    current_methods: list[str] = []
    current_line = 0
    bump_seen = False

    def flush() -> None:
        """结算上一个函数：可写但无 bump，且不在豁免清单中 → 记为问题。"""
        if not current_func or not any(m in MUTATING_METHODS for m in current_methods):
            return
        if bump_seen or current_func in NO_BROADCAST_ENDPOINTS:
            return
        writable_without_bump.append(f"{current_func}(L{current_line})")

    for index, line in enumerate(lines, start=1):
        decorator = _DECORATOR_RE.search(line)
        if decorator:
            pending_methods = re.findall(r"\"([A-Z]+)\"", decorator.group(1))
            continue

        definition = _DEF_RE.match(line)
        if definition:
            # 先用上一个函数自己的方法结算，再接收本次装饰器。
            flush()
            current_func = definition.group(1)
            current_methods = pending_methods
            pending_methods = []
            current_line = index
            bump_seen = False
            continue

        bump = _BUMP_RE.search(line)
        if bump:
            const = bump.group(1)
            if const not in domain_consts:
                errors.append(
                    f"L{index}: 未定义的域常量 {const}（应为 sync_domains 中的 DOMAIN_*）"
                )
            else:
                used.append(const)
                bump_seen = True

    flush()
    return errors, used, writable_without_bump


def _frontend_domains() -> set[str]:
    """从 useToolbox.ts 的 ``DOMAIN_ENDPOINTS`` 抽取域名字面量。"""
    source = _read(FRONTEND_STORE)
    start = source.find("DOMAIN_ENDPOINTS")
    if start < 0:
        return set()
    block = source[start : start + 2000]
    return set(re.findall(r"^\s*([A-Za-z][\w]*)\s*:\s*\[", block, flags=re.MULTILINE))


def main() -> int:
    domain_consts = _known_domains()
    if not domain_consts:
        print("✗ 无法从 api/sync_domains.py 解析出域常量")
        return 1

    errors, used, writable_without_bump = _scan_views(domain_consts)

    # 检查 2：孤儿域（定义了却没有任何写入方使用）。
    orphans = sorted(set(domain_consts) - set(used))

    # 检查 4：前后端域名集合一致。
    backend_values = {value for value in domain_consts.values() if value != "SYNC_DOMAINS"}
    frontend_values = _frontend_domains()
    mismatch: list[str] = []
    if frontend_values:
        only_backend = sorted(backend_values - frontend_values)
        only_frontend = sorted(frontend_values - backend_values)
        if only_backend:
            mismatch.append(f"仅后端有：{', '.join(only_backend)}")
        if only_frontend:
            mismatch.append(f"仅前端有：{', '.join(only_frontend)}")
    else:
        mismatch.append("未能解析前端 DOMAIN_ENDPOINTS（跳过该检查）")

    failed = False

    print(f"已知域 {len(domain_consts)} 个：{', '.join(sorted(domain_consts.values()))}")
    print(f"写入点使用域 {len(used)} 处，覆盖 {len(set(used))} 个域")

    if errors:
        failed = True
        print("\n✗ 域常量错误：")
        for item in errors:
            print("   ", item)

    if orphans:
        failed = True
        print("\n✗ 孤儿域（定义了但无写入方使用）：")
        for name in orphans:
            print(f"    {name} = {domain_consts[name]!r}")

    if writable_without_bump:
        failed = True
        print("\n✗ 可写端点缺少 _bump_revision（该端点的变更对客户端不可见）：")
        for item in writable_without_bump:
            print("   ", item)

    if mismatch:
        # 前端缺失只作为警告：前端可能尚未升级。
        print("\n⚠ 前后端域名契约差异（需确认是有意为之）：")
        for item in mismatch:
            print("   ", item)

    if failed:
        print("\n检查未通过。")
        return 1

    print("\n✓ 同步域完整性检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
