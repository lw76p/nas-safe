"""
NAS Safe — AI 管家（对话式工具调用，仅标准库）

定位：用户用大白话问 NAS / 电脑 / 服务器的状态或下达安全操作，
管家让大模型决定调用哪个工具，本地执行后把结果回灌给模型再作答。
模型是"调度员"，工具是"双手"——所有敏感动作都在本地、可审计。

一期工具（5 个）：
  - create_snapshot  建一张受保护快照（真防勒索，受版本/额度约束）
  - list_volumes     列出所有存储单元（NAS/电脑/服务器）
  - list_changes     列出近期异常改动 / 防勒索告警
  - search_files     按关键字搜文件（只读，限深度与结果数）
  - health_check     汇总 CPU / 内存 / 磁盘 / SMART 健康

配额：免费版 20 次/月（与 /api/ai/ask 共用计数）；家庭版 / 专业版不限量。
未配置 AI 时所有入口返回 None，核心功能零影响。
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
import urllib.error

import storage
import ai
import editions


# ---------------------------------------------------------------------------
# 配额（免费版 20 次/月，与问答共用）
# ---------------------------------------------------------------------------

def _quota_path() -> str:
    return os.path.join(storage.state_dir(), "ai_quota.json")


def _quota_limit() -> int:
    """当前版本每月 AI 调用上限。-1 表示不限量。"""
    return editions.limits().get("ai_quota", 20)


def quota_remaining() -> int:
    """返回本月剩余次数；-1 表示不限量。"""
    lim = _quota_limit()
    if lim == -1:
        return -1
    month = time.strftime("%Y-%m")
    try:
        with open(_quota_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    used = int(data.get(month, 0))
    return max(0, lim - used)


def quota_check() -> tuple[bool, int]:
    """(是否还能用, 剩余次数)。"""
    rem = quota_remaining()
    if rem == -1:
        return True, -1
    return rem > 0, rem


def quota_incr() -> None:
    lim = _quota_limit()
    if lim == -1:
        return
    month = time.strftime("%Y-%m")
    try:
        with open(_quota_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data[month] = int(data.get(month, 0)) + 1
    # 只保留最近 3 个月，避免无限增长
    keep = {m: data[m] for m in data if m >= _month_prev(2)}
    os.makedirs(storage.state_dir(), exist_ok=True)
    with open(_quota_path(), "w", encoding="utf-8") as fh:
        json.dump(keep, fh, ensure_ascii=False, indent=2)


def _month_prev(offset: int) -> str:
    y, m = time.localtime().tm_year, time.localtime().tm_mon
    idx = (y * 12 + (m - 1)) - offset
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


# ---------------------------------------------------------------------------
# 工具实现（每个返回人类可读字符串；异常被捕获后回灌模型）
# ---------------------------------------------------------------------------

def _find_volume(volume_id: str):
    vols = storage.list_all_volumes()
    for v in vols:
        if v.mountpoint == volume_id or v.name == volume_id:
            return v
    return None


def tool_create_snapshot(volume_id: str, name: str = "") -> str:
    if not volume_id:
        return "错误：缺少 volume 参数（先用 list_volumes 看有哪些存储单元）"
    target = _find_volume(volume_id)
    if target is None:
        return f"错误：未找到存储单元 {volume_id}"
    snap_name = (name or f"ai-{int(time.time())}")[:40]
    try:
        snap = storage.create_snapshot(target, snap_name, vital=True)
        return (f"已创建受保护快照：{snap.name}（{target.name}，fs={snap.fs_type}）"
                f"——即使文件被勒索加密，也能从这张快照还原。")
    except Exception as exc:  # noqa: BLE001
        return f"创建快照失败：{exc}"


def tool_list_volumes() -> str:
    try:
        vols = storage.list_all_volumes()
    except Exception as exc:  # noqa: BLE001
        return f"读取存储单元失败：{exc}"
    if not vols:
        return "当前没有检测到任何受保护的存储单元。"
    lines = []
    for v in vols[:12]:
        lines.append(f"- {v.name}｜挂载点 {v.mountpoint}｜类型 {v.fs_type}")
    return "受保护存储单元：\n" + "\n".join(lines)


def tool_list_changes() -> str:
    try:
        alerts = storage.scan_tamper()
    except Exception as exc:  # noqa: BLE001
        return f"读取异常改动失败：{exc}"
    if not alerts:
        return "近期未检测到异常改动 / 防勒索告警，数据状态正常。"
    lines = [f"- [{a.get('level', '')}] {a.get('type', '')}：{a.get('detail', '')}"
             for a in alerts[:12]]
    return "近期异常改动 / 防勒索告警：\n" + "\n".join(lines)


def tool_search_files(keyword: str, path: str = "") -> str:
    if not keyword or len(keyword) < 2:
        return "错误：keyword 至少 2 个字符"
    kw = keyword.lower()
    roots = [path] if path else [v.mountpoint for v in storage.list_all_volumes()]
    roots = [r for r in roots if r and os.path.isdir(r)]
    if not roots:
        return "错误：没有可搜索的路径（先 list_volumes 看挂载点）"
    hits, scanned = [], 0
    max_hits, max_scan = 50, 20000
    try:
        for root in roots:
            for dirpath, dirnames, filenames in os.walk(root):
                # 跳过明显系统/缓存目录，减少噪音与耗时
                dirnames[:] = [d for d in dirnames
                               if d not in (".nassafe", ".git", "__pycache__", "node_modules")]
                for fn in filenames:
                    scanned += 1
                    if scanned > max_scan:
                        break
                    if kw in fn.lower():
                        hits.append(os.path.join(dirpath, fn))
                        if len(hits) >= max_hits:
                            break
                if len(hits) >= max_hits or scanned > max_scan:
                    break
            if len(hits) >= max_hits or scanned > max_scan:
                break
    except Exception:  # noqa: BLE001 权限/断链等单个错误不影响整体
        pass
    if not hits:
        return f"未找到包含「{keyword}」的文件（已扫描 {scanned} 个文件）。"
    head = "\n".join(f"- {h}" for h in hits[:max_hits])
    more = "" if len(hits) < max_hits else f"\n…仅显示前 {max_hits} 条"
    return f"匹配「{keyword}」的文件（已扫描 {scanned} 个）：\n{head}{more}"


def tool_health_check() -> str:
    parts = []
    try:
        import metrics
        m = metrics.collect()
        cpu = (m.get("cpu") or {})
        mem = (m.get("mem") or {})
        up = (m.get("uptime") or {})
        parts.append(
            f"【系统】{(m.get('hostname') or '本机')}，已运行 {up.get('days', 0)} 天 "
            f"{up.get('hours', 0)} 小时，CPU {cpu.get('percent', '--')}%"
            + (f"，温度 {cpu.get('temp_c')}°C" if cpu.get('temp_c') is not None else "")
            + f"，内存 {mem.get('percent', '--')}%")
        vols = m.get("volumes") or []
        if vols:
            parts.append("【存储】" + "；".join(
                f"{v['mount']} 已用 {v.get('percent', 0)}%" for v in vols[:8]))
        for t in (m.get("trends") or []):
            if t.get("days_to_full"):
                parts.append(f"【趋势】{t['mount']} 约 {t['days_to_full']} 天后存满")
    except Exception as exc:  # noqa: BLE001
        parts.append(f"【系统】指标读取失败：{exc}")
    try:
        import smartd
        sd = smartd.collect(force=False)
        disks = sd.get("disks") or sd.get("items") or []
        if disks:
            bad = [d for d in disks if (d.get("health") or "").lower() not in ("ok", "good", "正常", "")]
            parts.append(f"【硬盘】共 {len(disks)} 块，异常 {len(bad)} 块"
                         + ("" if not bad else "：" + "；".join(
                             f"{d.get('device', d.get('name', '?'))} {d.get('health')}" for d in bad[:6])))
        else:
            parts.append("【硬盘】未取到 SMART 数据")
    except Exception:  # noqa: BLE001
        parts.append("【硬盘】SMART 读取失败")
    return "\n".join(parts)


_TOOLS = {
    "create_snapshot": tool_create_snapshot,
    "list_volumes": tool_list_volumes,
    "list_changes": tool_list_changes,
    "search_files": tool_search_files,
    "health_check": tool_health_check,
}

_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "create_snapshot",
            "description": "为某个存储单元创建一张受保护快照（真防勒索，文件被加密也能还原）。需要先用 list_volumes 获取 volume 标识。",
            "parameters": {
                "type": "object",
                "properties": {
                    "volume": {"type": "string", "description": "存储单元名或挂载点，如 Data、/data"},
                    "name": {"type": "string", "description": "快照名称（可选，默认自动命名）"},
                },
                "required": ["volume"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_volumes",
            "description": "列出所有受保护的存储单元（NAS / 电脑 / 服务器），返回名称、挂载点、类型。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_changes",
            "description": "列出近期异常改动与防勒索告警。没有则说明数据状态正常。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "按文件名关键字在所有受保护存储单元里搜文件（只读）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "文件名包含的关键字，至少 2 个字符"},
                    "path": {"type": "string", "description": "限定搜索目录（可选，默认搜全部）"},
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "health_check",
            "description": "汇总系统、存储使用率、增长趋势与硬盘 SMART 健康。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


# ---------------------------------------------------------------------------
# 工具调用循环（OpenAI function-calling）
# ---------------------------------------------------------------------------

_SYSTEM = (
    "你是「全栈数据卫士」的 AI 管家，帮普通用户照看 NAS、电脑、服务器的数据安全。"
    "你可以调用工具查看存储单元、搜文件、看健康状况、查异常改动，也能创建受保护快照。"
    "规则：① 只用工具返回的真实数据作答，不要编造；② 用户想建快照但没说清哪个存储单元时，"
    "先调用 list_volumes 再建；③ 用通俗中文，给 2-3 条可执行建议；④ 控制在 300 字以内。"
)


def _chat_with_tools(messages: list, cfg: dict, max_rounds: int = 4) -> (str, str):
    prov = cfg.get("provider", "deepseek")
    info = ai.PROVIDERS.get(prov, ai.PROVIDERS["deepseek"])
    base = (cfg.get("base_url") or info["base_url"]).rstrip("/")
    if prov == "ollama" and "localhost" in base:
        ip = os.environ.get("NASSAFE_QNAP_HOST") or os.environ.get("NASSAFE_HOST")
        if ip and ip not in ("127.0.0.1", "localhost"):
            base = f"http://{ip}:11434/v1"
    model = cfg.get("model") or info["model"]
    url = f"{base}/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 1200,
        "tools": _TOOL_SCHEMAS,
        "tool_choice": "auto",
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_content = ""
    for _ in range(max_rounds):
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        api_key = cfg.get("api_key", "")
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.URLError as exc:
            return "", f"网络错误: {exc}"
        except Exception as exc:  # noqa: BLE001
            return "", f"调用失败: {exc}"
        msg = body.get("choices", [{}])[0].get("message", {})
        last_content = (msg.get("content") or "").strip()
        calls = msg.get("tool_calls")
        if not calls:
            return last_content, ""
        messages.append(msg)  # 保留带 tool_calls 的 assistant 消息
        for tc in calls:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:  # noqa: BLE001
                args = {}
            func = _TOOLS.get(name)
            if func is None:
                res = f"未知工具: {name}"
            else:
                try:
                    res = func(**args)
                except Exception as exc:  # noqa: BLE001
                    res = f"工具执行出错: {exc}"
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "content": str(res)[:3000],
            })
    return last_content, ""


def run(question: str, history: list | None = None) -> (str, str):
    """对话式管家主入口。返回 (文本, 错误)。未配置 AI 返回 (None, '')。"""
    cfg = ai.load_config()
    if not ai.is_ready():
        return None, ""
    q = (question or "").strip()
    if not q:
        return None, "请先输入问题"
    messages = [{"role": "system", "content": _SYSTEM}]
    for m in (history or [])[-12:]:
        if isinstance(m, dict) and m.get("role") in ("user", "assistant") \
                and isinstance(m.get("content"), str) and m["content"].strip():
            messages.append({"role": m["role"], "content": m["content"][:4000]})
    messages.append({"role": "user", "content": q})
    return _chat_with_tools(messages, cfg)
