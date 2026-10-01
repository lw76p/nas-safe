"""磁盘垃圾清理（junk v1）——只读扫描出报告，清理按类别白名单动作。

四类目标（均经真机探测确认可行）：
  recycle  回收站          各卷 *recycle* 目录（未启用时为空，动态探测）
  thumbs   缩略图缓存      .@__thumb / @Thumbnail 目录（QTS 自生成，可重建；
                           HMP 海报在应用目录里，与此无关，且被黑名单排除）
  docker   软件包缓存      Docker 悬空镜像 + 构建缓存（image/builder prune，不动在用容器）
  logs     日志与临时文件  /var/log、/mnt/HDA_ROOT/.logs 里 30 天前的轮转日志

安全设计（沿用重复文件模块的共识）：
  - 扫描只读（find/du/stat/docker system df），不改任何数据；
  - 清理必须 confirm=True，且逐项校验路径属于对应类别（防接口滥用删任意文件）；
  - 应用黑名单路径（HMP、MoviePilot、Docker 数据、qpkg 等）绝不进入扫描结果；
  - 日志只清 30 天前的轮转文件，活动日志一律不碰。
"""
import posixpath
import re
import shlex
import threading
from datetime import datetime

import qnap  # noqa: F401  复用远程客户端


class JunkError(Exception):
    """垃圾清理功能错误。"""


# ---------------- 状态（与 duplicates 同款后台线程模式） ----------------

_lock = threading.Lock()
_scan_state: dict = {"status": "idle"}
_report: dict = {}

SCAN_ROOTS = "/share/CACHEDEV1_DATA /share/CACHEDEV2_DATA /share/MD0_DATA"
# 应用黑名单：任何路径命中即整个剪掉，绝不报告也不清理
BLACKLIST_RE = re.compile(
    r"(HMP|MoviePilot|moviepilot|docker|Docker|Container|container|"
    r"\.qpkg|@appdata|@plugins|@tmp|nassafe|\.git)",
    re.I,
)
LOG_DIRS = "/var/log /mnt/HDA_ROOT/.logs"
LOG_MIN_AGE_DAYS = 30


def get_status() -> dict:
    with _lock:
        return {"ok": True, "clean": dict(_clean_state), **_scan_state}


def load_report() -> dict:
    with _lock:
        return _report


# ---------------- 扫描 ----------------

def _client():
    return qnap.default_client()


def _thumbs_script() -> str:
    q = shlex.quote
    return (
        f"find {SCAN_ROOTS} -xdev -type d \\( -name '.@__thumb' -o -name '@Thumbnail' \\) "
        f"-print0 2>/dev/null | xargs -0 -r du -sk 2>/dev/null"
    )


def _recycle_script() -> str:
    return (
        f"find {SCAN_ROOTS} -xdev -maxdepth 2 -type d "
        f"\\( -iname '*recycle*' -o -name '$RECYCLE.BIN' \\) "
        f"-print0 2>/dev/null | xargs -0 -r du -sk 2>/dev/null"
    )


def _logs_script() -> str:
    q = shlex.quote
    return (
        f"find {q(LOG_DIRS)} -type f "
        f"\\( -name '*.gz' -o -name '*.[0-9]' -o -name '*.log.[0-9]*' \\) -mtime +{LOG_MIN_AGE_DAYS} "
        f"-print0 2>/dev/null | xargs -0 -r stat -c '%s %Y %n' 2>/dev/null"
    )


def _parse_du(text: str) -> list[dict]:
    """du -sk 输出 → [{path, kb}]。跳过黑名单路径。"""
    items = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t", 1) if "\t" in line else line.split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        kb, path = int(parts[0]), parts[1].strip()
        if not path.startswith("/") or BLACKLIST_RE.search(path):
            continue
        items.append({"path": path, "kb": kb})
    return items


def _parse_stat(text: str) -> list[dict]:
    """stat '%s %Y %n' 输出 → [{path, size, mtime}]。跳过黑名单。"""
    items = []
    for line in text.splitlines():
        parts = line.split(None, 2)
        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        size, mtime, path = int(parts[0]), int(parts[1]), parts[2]
        if not path.startswith("/") or BLACKLIST_RE.search(path):
            continue
        items.append({"path": path, "size": size, "mtime": mtime})
    return items


def _docker_section(client) -> dict:
    """docker system df 只读采样（容器远程模式下经 SSH 回宿主机执行）。"""
    docker_bin = "/share/CE_CACHEDEV1_DATA/.qpkg/container-station/bin/docker"
    out = client.run_shell(
        f"{shlex.quote(docker_bin)} system df --format "
        f"'{{{{.Type}}}}\\t{{{{.Size}}}}\\t{{{{.Reclaimable}}}}' 2>/dev/null ; "
        f"{shlex.quote(docker_bin)} image ls -f dangling=true --format '{{{{.ID}}}} {{{{.Size}}}}' 2>/dev/null | head -20"
    )
    cats, dangling = [], []
    lines = out.splitlines()
    df_lines = [l for l in lines if "\t" in l and not re.match(r"^[0-9a-f]{12} ", l)]
    dangling_lines = [l for l in lines if re.match(r"^[0-9a-f]{12} ", l)]
    for line in df_lines:
        t = line.split("\t")
        if len(t) >= 3:
            cats.append({"type": t[0], "size": t[1], "reclaimable": t[2]})
    for line in dangling_lines:
        t = line.split(None, 1)
        dangling.append({"id": t[0], "size": t[1] if len(t) > 1 else "?"})
    return {"df": cats, "dangling_images": dangling}


def _scan_worker() -> None:
    client = None
    try:
        client = _client()

        def set_state(**kw):
            with _lock:
                _scan_state.update(kw)

        set_state(status="scanning", started_at=datetime.now().isoformat(timespec="seconds"),
                  error="", finished_at=None)

        recycle = _parse_du(client.run_shell(_recycle_script()))
        set_state(phase="thumbs")
        thumbs = _parse_du(client.run_shell(_thumbs_script()))
        set_state(phase="logs")
        logs = _parse_stat(client.run_shell(_logs_script()))
        set_state(phase="docker")
        docker = _docker_section(client)

        def cat(key, name, items, total_bytes, hint):
            return {"key": key, "name": name, "items": items,
                    "total_bytes": total_bytes, "hint": hint}

        categories = [
            cat("recycle", "回收站", recycle, sum(i["kb"] for i in recycle) * 1024,
                "网络回收站里已删除但未清空的文件"),
            cat("thumbs", "缩略图缓存", thumbs, sum(i["kb"] for i in thumbs) * 1024,
                "QTS 自动生成的缩略图，删后浏览图片时会自动重建；HMP 海报在应用目录，不受影响"),
            cat("logs", "30 天前的轮转日志", logs, sum(i["size"] for i in logs),
                "只列出 30 天前的轮转/压缩日志，活动日志一律不碰"),
            cat("docker", "Docker 可回收空间", [], 0,
                "悬空镜像与构建缓存可用 image/builder prune 回收；未用卷只提示，不自动清"),
        ]

        report = {
            "scanned_at": datetime.now().isoformat(timespec="seconds"),
            "categories": categories,
            "docker": docker,
        }
        with _lock:
            _report.clear()
            _report.update(report)
            _scan_state.update(status="done", phase="",
                               finished_at=datetime.now().isoformat(timespec="seconds"))
    except Exception as exc:  # noqa: BLE001
        with _lock:
            _scan_state.update(status="error", error=str(exc),
                               finished_at=datetime.now().isoformat(timespec="seconds"))
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


def start_scan() -> dict:
    with _lock:
        if _scan_state.get("status") == "scanning":
            return {"ok": False, "error": "已有扫描在进行中"}
    threading.Thread(target=_scan_worker, daemon=True).start()
    return {"ok": True, "started": True}


# ---------------- 清理（后台任务 + 实时进度） ----------------
#
# 为什么清理必须后台化：151GB 回收站的 rm -rf、Docker prune 都要跑几分钟，
# 同步接口会让前端干等到像死机（用户实测反馈）。现改为与扫描同款模式：
# POST /api/junk/clean 立即返回"已启动"，进度走 GET /api/junk/status 的
# clean 字段，前端 2s 轮询一次实时显示"已完成 x/y 项"。

# 每个类别允许的清理动作校验器：路径必须落在该类别报告登记过的范围内
def _validate_paths(category: str, paths: list, report: dict) -> list[dict]:
    cat = next((c for c in report.get("categories", []) if c["key"] == category), None)
    if not cat:
        raise JunkError(f"未知类别: {category}，请先扫描")
    known = {i["path"]: i for i in cat["items"]}
    validated = []
    for p in paths:
        pn = posixpath.normpath(str(p))
        item = known.get(pn)
        if not item:
            raise JunkError(f"路径不在当前{cat['name']}报告内，拒绝操作: {p}")
        if BLACKLIST_RE.search(pn):
            raise JunkError(f"路径命中应用黑名单，拒绝操作: {p}")
        validated.append(item)
    return validated


_clean_state: dict = {
    "status": "idle", "category": "", "total": 0, "done": 0,
    "error": "", "freed_bytes": 0, "detail": "",
    "started_at": None, "finished_at": None,
}


def get_clean_status() -> dict:
    with _lock:
        return dict(_clean_state)


def start_clean(category: str | None = None, confirm: bool = False,
                paths: list | None = None, categories: list | None = None) -> dict:
    """启动后台清理。支持单类别（category）或多类别（categories，一键全清）。

    校验全部在进线程前完成（fail fast），线程只负责执行。
    多类别时按传入顺序逐类清理，进度 done/total 是所有类别的合计。
    """
    with _lock:
        if _clean_state.get("status") == "running":
            return {"ok": False, "error": "已有清理任务在进行中，请等它完成"}
    if confirm is not True:
        raise JunkError("清理操作需要 confirm=true 确认参数")
    report = load_report()
    if not report:
        raise JunkError("没有扫描报告，请先执行扫描")

    cats = [str(c) for c in categories] if isinstance(categories, list) and categories else (
        [category] if category else [])
    if not cats:
        raise JunkError("未指定清理类别")
    for c in cats:
        if c not in ("recycle", "thumbs", "logs", "docker"):
            raise JunkError(f"不支持的类别: {c}")
    if len(cats) > 1 and paths:
        raise JunkError("多类别清理不支持指定 paths（逐类全清）")

    jobs: list[tuple[str, list]] = []
    total = 0
    for c in cats:
        if c == "docker":
            targets: list = []
            total += 1  # docker 清理算一步（prune 一条命令）
        else:
            targets = _validate_paths(c, paths or
                                      [i["path"] for i in
                                       next(x for x in report["categories"] if x["key"] == c)["items"]],
                                      report)
            total += len(targets)
        jobs.append((c, targets))

    with _lock:
        _clean_state.update(
            status="running", category=cats[0], total=total, done=0,
            error="", freed_bytes=0, detail="",
            started_at=datetime.now().isoformat(timespec="seconds"),
            finished_at=None)
    threading.Thread(target=_clean_worker, args=(jobs, total), daemon=True).start()
    return {"ok": True, "started": True, "total": total, "categories": cats}


def _clean_worker(jobs: list, total: int) -> None:
    client = None
    try:
        client = _client()
        # 清理是大动作：默认 60s 的 channel 读超时必炸；放宽到 30 分钟。
        client.timeout = 1800
        q = shlex.quote

        def set_state(**kw):
            with _lock:
                _clean_state.update(kw)

        cleaned = 0
        freed = 0
        detail = ""
        done = 0
        for cat, targets in jobs:
            set_state(category=cat)
            if cat == "docker":
                docker_bin = "/share/CE_CACHEDEV1_DATA/.qpkg/container-station/bin/docker"
                out = client.run_shell(
                    f"{q(docker_bin)} image prune -f 2>&1 ; "
                    f"{q(docker_bin)} builder prune -f 2>&1"
                )
                detail = (detail + "\n" + out.strip()[-200:]).strip()
                m = re.search(r"total reclaimed space:\s*([\d.]+)\s*([kMG]?B)", out, re.I)
                if m:
                    freed += _parse_size(m.group(1), m.group(2))
                done += 1
                set_state(done=done)
                continue
            # 分批执行：1464 个缩略图目录若拼成一条命令，长度会超出远端
            # ARG_MAX/sh 解析上限，表现为静默失败甚至连接被重置。
            # 每批跑完推进一次进度，前端 2s 轮询即可看到 x/y 实时变化。
            CHUNK = 100
            for i in range(0, len(targets), CHUNK):
                batch = targets[i:i + CHUNK]
                lines = []
                for item in batch:
                    p = item["path"]
                    if cat == "logs":
                        lines.append(f"rm -f -- {q(p)} 2>/dev/null ; printf 'OK\\t{p}\\n'")
                    else:
                        # recycle/thumbs：只删目录内容对应的目录本身（QTS 会重建空目录）
                        lines.append(f"rm -rf -- {q(p)} 2>/dev/null ; printf 'OK\\t{p}\\n'")
                out = client.run_shell(" ; ".join(lines))
                cleaned += sum(1 for line in out.splitlines() if line.startswith("OK"))
                done = min(done + len(batch), total)
                set_state(done=done)
            freed += sum((i.get("kb", 0) * 1024) if "kb" in i else i.get("size", 0) for i in targets)
        with _lock:
            _clean_state.update(status="done", cleaned=cleaned, freed_bytes=freed,
                                detail=detail,
                                finished_at=datetime.now().isoformat(timespec="seconds"))
    except Exception as exc:  # noqa: BLE001
        with _lock:
            _clean_state.update(status="error", error=str(exc),
                                finished_at=datetime.now().isoformat(timespec="seconds"))
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


def _parse_size(num: str, unit: str) -> int:
    try:
        v = float(num)
    except ValueError:
        return 0
    mult = {"B": 1, "kB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}
    return int(v * mult.get(unit, 1))
