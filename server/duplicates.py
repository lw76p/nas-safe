"""
TS Safe — 重复文件清理（v4 功能区）

产品定位：给用户一个「只出报告、软删除、全程可恢复」的重复文件清理工具。

设计原则（2026-09-30 防误删共识，不可违背）：
  1. 扫描 = 只读。绝不在扫描阶段修改任何数据。
  2. 删除 = 软删除。被移除的文件先移入扫描根目录下的 .nassafe-quarantine/
     隔离目录（同卷 rename，瞬时完成），随时可恢复，绝不直接 unlink。
  3. 判定 = 内容哈希。先按大小分组（零读盘），仅对「同大小」候选算 MD5，
     内容完全一致才算重复 —— 零误报。
  4. 护栏：
     - 每组至少保留一份：一次操作不允许把某组内容清空。
     - 只允许隔离「当前报告里存在的路径」，防止接口被滥用删除任意文件。
     - 跳过隐藏目录、QNAP 特殊目录（#@ 开头）、回收站、快照挂载点、
       隔离目录自身，以及太新（默认 7 天内改过）的文件。
  5. 报告持久化到 state/duplicates_report.json，重启不丢。

执行通道：复用 qnap.default_client().run_shell()（容器 SSH 远程 / 主机本地通吃），
与 metrics.py 同一条链路，零新增依赖。
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import shlex
import sys
import threading
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime

import storage  # 自包含模块（qnap 懒加载），无循环依赖

# 单次扫描最多收录的文件数（防超大库拖垮内存/SSH 通道）
MAX_FILES = 200_000
# md5sum 单批的字节上限（约 2 块 4K 电影），防单条 SSH 命令跑太久
MAX_BATCH_BYTES = 8 * 1024 * 1024 * 1024
# md5sum 单批的文件数上限
MAX_BATCH_FILES = 40

# find 需要剪掉的目录名：隐藏目录、QNAP 特殊目录、回收站、系统目录
_PRUNE_NAMES = (
    "-name '.*' -o -name '#*' -o -name 'System Volume Information' "
    "-o -name '$RECYCLE.BIN' -o -name 'lost+found'"
)


class DuplicateError(storage.StorageError):
    """重复文件功能错误（继承 StorageError，走 app.py 统一 400 通道）。"""


# ---------------------------------------------------------------------------
# 状态（内存）与持久化
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_scan_state: dict = {
    "status": "idle",          # idle / scanning / done / error
    "phase": "",               # inventory / hash
    "root": "",
    "started_at": None,
    "finished_at": None,
    "error": "",
    "files_seen": 0,
    "candidates": 0,           # 参与哈希比对的文件数
    "candidate_bytes": 0,
    "hashed_bytes": 0,
    "groups": 0,
    "wasted_bytes": 0,
    "truncated": False,
}


def state_dir() -> str:
    return storage.state_dir()


def _report_path() -> str:
    return os.path.join(state_dir(), "duplicates_report.json")


def _quarantine_log_path() -> str:
    return os.path.join(state_dir(), "quarantine.json")


def load_report() -> dict:
    try:
        with open(_report_path(), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_report(report: dict) -> None:
    d = state_dir()
    try:
        os.makedirs(d, exist_ok=True)
        with open(_report_path(), "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False)
    except OSError:
        pass  # 状态目录只读时跳过持久化，报告仍在内存可用


def load_quarantine_log() -> dict:
    try:
        with open(_quarantine_log_path(), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {"entries": []}


def _save_quarantine_log(data: dict) -> None:
    d = state_dir()
    os.makedirs(d, exist_ok=True)
    with open(_quarantine_log_path(), "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def get_status() -> dict:
    with _lock:
        return dict(_scan_state)


# ---------------------------------------------------------------------------
# 执行通道
# ---------------------------------------------------------------------------

def _client():
    """构造执行通道：SSH 远程（容器部署）或本地。超时放宽到 1 小时（大库哈希）。"""
    from qnap import default_client

    c = default_client()
    c.timeout = 3600
    return c


def _probe_printf(client) -> bool:
    """探测远端 find 是否支持 -printf（GNU findutils）。"""
    out = client.run_shell("find / -maxdepth 0 -printf 'ok\\n' 2>/dev/null")
    return "ok" in out


def _find_script(root: str, min_size: int, min_age_days: int, use_printf: bool) -> str:
    """构造只读清点脚本。

    剪掉隐藏/特殊/回收站目录（整个不进入）；只输出普通文件，绝不修改数据。
    QTS 实测坑（BusyBox find 1.24）：无 -printf；`-exec ... {} +` 包在 \\( \\)
    里会静默空输出 —— 兜底走 `-print0 | xargs -0 stat` 管道（xargs 自动分批，
    天然防 ARG_MAX）。GNU find（fnOS/TrueNAS 等）直接用 -printf。
    """
    q = shlex.quote
    # 注意：find -mtime +N 是「超过 N 个 24 小时」，+0 也意味着要超过 1 天。
    # min_age_days=0 表示不过滤时间（今天新建的文件也参与比对）。
    age = int(min_age_days)
    age_cond = f" -mtime +{age}" if age > 0 else ""
    prune = f"\\( -type d \\( {_PRUNE_NAMES} \\) -prune \\)"
    if use_printf:
        return (
            f"find {q(root)} {prune} -o "
            f"\\( -type f -size +{int(min_size)}c{age_cond} "
            r"-printf '%s\t%T@\t%p\n' \) 2>/dev/null"
        )
    return (
        f"find {q(root)} {prune} -o "
        f"\\( -type f -size +{int(min_size)}c{age_cond} -print0 \\) 2>/dev/null"
        r" | xargs -0 stat -c '%s %Y %n' 2>/dev/null"
    )


def _parse_inventory(text: str) -> list[dict]:
    """解析 find 输出：每行 size<TAB>mtime<TAB>path（printf）或 size mtime path（stat 兜底）。

    路径可含空格：先按制表符切，切不开再按空白切前两段（split(None, 2)）。
    """
    files: list[dict] = []
    for line in text.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            parts = line.split(None, 2)
        if len(parts) != 3:
            continue
        size_s, mtime_s, path = parts
        if not size_s.strip().isdigit() or not path.startswith("/"):
            continue
        try:
            mtime = int(float(mtime_s))
        except ValueError:
            continue  # 非法行（路径含换行等被截断），防御性跳过
        files.append({
            "path": path,
            "size": int(size_s),
            "mtime": mtime,
        })
    return files


# ---------------------------------------------------------------------------
# 哈希阶段
# ---------------------------------------------------------------------------

def _hash_batches(client, candidates: list[dict], progress_cb) -> dict:
    """对候选文件分批 md5sum，返回 {path: md5}。"""
    hashes: dict[str, str] = {}
    batches: list[list[dict]] = []
    cur: list[dict] = []
    cur_bytes = 0
    for f in sorted(candidates, key=lambda x: x["size"]):
        cur.append(f)
        cur_bytes += f["size"]
        if len(cur) >= MAX_BATCH_FILES or cur_bytes >= MAX_BATCH_BYTES:
            batches.append(cur)
            cur, cur_bytes = [], 0
    if cur:
        batches.append(cur)

    done_bytes = 0
    for batch in batches:
        args = " ".join(shlex.quote(f["path"]) for f in batch)
        out = client.run_shell(f"md5sum -- {args} 2>/dev/null")
        for line in out.splitlines():
            # md5sum 行格式：<32位hex><两个空格><path>；异常名会有 \ 前缀行
            line = line.lstrip("\\")
            if len(line) < 34 or line[32:34] != "  ":
                continue
            digest, path = line[:32], line[34:]
            if path.startswith("/"):
                hashes[path] = digest
        done_bytes += sum(f["size"] for f in batch)
        progress_cb(done_bytes)
    return hashes


# ---------------------------------------------------------------------------
# 扫描主流程（后台线程）
# ---------------------------------------------------------------------------

def _scan_worker(root: str, min_size: int, min_age_days: int) -> None:
    client = None
    try:
        client = _client()

        def set_state(**kw):
            with _lock:
                _scan_state.update(kw)

        set_state(status="scanning", phase="inventory", root=root,
                  started_at=datetime.now().isoformat(timespec="seconds"),
                  error="", files_seen=0, candidates=0, candidate_bytes=0,
                  hashed_bytes=0, groups=0, wasted_bytes=0,
                  finished_at=None, truncated=False)

        use_printf = _probe_printf(client)
        script = _find_script(root, min_size, min_age_days, use_printf)
        out = client.run_shell(script)
        files = _parse_inventory(out)

        truncated = False
        if len(files) > MAX_FILES:
            files = files[:MAX_FILES]
            truncated = True

        with _lock:
            _scan_state["files_seen"] = len(files)
            _scan_state["truncated"] = truncated

        # 按大小分组：只有同大小才可能是同内容（零误报的第一道筛）
        by_size: dict[int, list[dict]] = defaultdict(list)
        for f in files:
            by_size[f["size"]].append(f)

        candidates: list[dict] = []
        for size, group in by_size.items():
            if size >= min_size and len(group) >= 2:
                candidates.extend(group)

        candidate_bytes = sum(f["size"] for f in candidates)
        with _lock:
            _scan_state["candidates"] = len(candidates)
            _scan_state["candidate_bytes"] = candidate_bytes
            _scan_state["phase"] = "hash"

        # 按总字节数降序哈希（大组先出结果，进度条体感更好）
        size_groups = sorted(
            (g for g in by_size.values() if len(g) >= 2 and g[0]["size"] >= min_size),
            key=lambda g: g[0]["size"] * len(g), reverse=True,
        )

        path2hash: dict[str, str] = {}
        hashed = 0

        def on_progress(done: int):
            nonlocal hashed
            hashed = done
            with _lock:
                _scan_state["hashed_bytes"] = done

        # 把候选按「所属大小组连续」重排，md5sum 结果逐组聚合
        flat: list[dict] = []
        for g in size_groups:
            flat.extend(g)
        if flat:
            path2hash = _hash_batches(client, flat, on_progress)

        # 聚合成内容重复组
        by_hash: dict[str, list[dict]] = defaultdict(list)
        for g in size_groups:
            digests: dict[str, list[dict]] = defaultdict(list)
            for f in g:
                d = path2hash.get(f["path"])
                if d:
                    digests[d].append(f)
            for d, fs in digests.items():
                if len(fs) >= 2:
                    by_hash[d].extend(fs)

        groups = []
        wasted = 0
        for d, fs in by_hash.items():
            fs_sorted = sorted(fs, key=lambda x: x["path"])
            waste = fs_sorted[0]["size"] * (len(fs_sorted) - 1)
            wasted += waste
            groups.append({
                "hash": d,
                "size": fs_sorted[0]["size"],
                "wasted": waste,
                "files": fs_sorted,
            })
        groups.sort(key=lambda g: g["wasted"], reverse=True)

        report = {
            "scanned_at": datetime.now().isoformat(timespec="seconds"),
            "root": root,
            "min_size": min_size,
            "min_age_days": min_age_days,
            "files_seen": len(files),
            "truncated": truncated,
            "groups": groups,
            "wasted_bytes": wasted,
            "group_count": len(groups),
        }
        _save_report(report)

        with _lock:
            _scan_state.update(
                status="done", phase="", groups=len(groups),
                wasted_bytes=wasted,
                finished_at=datetime.now().isoformat(timespec="seconds"),
            )
    except Exception as exc:  # noqa: BLE001
        with _lock:
            _scan_state.update(
                status="error", error=str(exc),
                finished_at=datetime.now().isoformat(timespec="seconds"),
            )
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


def _hash_windows(candidates: list[dict], progress_cb) -> dict:
    """Windows 本地 MD5 哈希（分块读取，避免大文件内存爆炸）。"""
    hashes: dict[str, str] = {}
    done = 0
    for f in candidates:
        path = f["path"]
        h = hashlib.md5()  # noqa: S324  仅用于重复文件内容比对，非安全场景
        try:
            with open(path, "rb") as fh:
                while True:
                    chunk = fh.read(8 * 1024 * 1024)
                    if not chunk:
                        break
                    h.update(chunk)
            hashes[path] = h.hexdigest()
        except OSError:
            pass
        done += f["size"]
        progress_cb(done)
    return hashes


def _scan_worker_windows(root: str, min_size: int, min_age_days: int) -> None:
    """Windows 本地重复文件扫描（标准库实现）。"""

    def set_state(**kw):
        with _lock:
            _scan_state.update(kw)

    set_state(
        status="scanning", phase="inventory", root=root,
        started_at=datetime.now().isoformat(timespec="seconds"),
        error="", files_seen=0, candidates=0, candidate_bytes=0,
        hashed_bytes=0, groups=0, wasted_bytes=0,
        finished_at=None, truncated=False,
    )

    skip_dir_names = {
        ".nassafe-quarantine", "$RECYCLE.BIN",
        "System Volume Information", "lost+found",
    }
    cutoff = (time.time() - min_age_days * 86400) if min_age_days > 0 else 0
    files: list[dict] = []
    truncated = False

    for dirpath, dirnames, filenames in os.walk(root):
        # 原地修改 dirnames，跳过隐藏/系统目录
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".") and d not in skip_dir_names
        ]
        for fn in filenames:
            if fn.startswith("."):
                continue
            path = os.path.join(dirpath, fn)
            try:
                st = os.stat(path)
            except OSError:
                continue
            if st.st_size < min_size:
                continue
            if min_age_days > 0 and st.st_mtime > cutoff:
                continue
            files.append({
                "path": path,
                "size": st.st_size,
                "mtime": int(st.st_mtime),
            })
            if len(files) >= MAX_FILES:
                files = files[:MAX_FILES]
                truncated = True
                break
        if truncated:
            break

    with _lock:
        _scan_state["files_seen"] = len(files)
        _scan_state["truncated"] = truncated

    # 按大小分组
    by_size: dict[int, list[dict]] = defaultdict(list)
    for f in files:
        by_size[f["size"]].append(f)

    size_groups = sorted(
        (g for g in by_size.values() if len(g) >= 2 and g[0]["size"] >= min_size),
        key=lambda g: g[0]["size"] * len(g), reverse=True,
    )

    candidates = [f for g in size_groups for f in g]
    candidate_bytes = sum(f["size"] for f in candidates)
    with _lock:
        _scan_state["candidates"] = len(candidates)
        _scan_state["candidate_bytes"] = candidate_bytes
        _scan_state["phase"] = "hash"

    path2hash: dict[str, str] = {}

    def on_progress(done: int):
        with _lock:
            _scan_state["hashed_bytes"] = done

    if candidates:
        path2hash = _hash_windows(candidates, on_progress)

    # 聚合成重复组
    by_hash: dict[str, list[dict]] = defaultdict(list)
    for g in size_groups:
        digests: dict[str, list[dict]] = defaultdict(list)
        for f in g:
            d = path2hash.get(f["path"])
            if d:
                digests[d].append(f)
        for d, fs in digests.items():
            if len(fs) >= 2:
                by_hash[d].extend(fs)

    groups = []
    wasted = 0
    for d, fs in by_hash.items():
        fs_sorted = sorted(fs, key=lambda x: x["path"])
        waste = fs_sorted[0]["size"] * (len(fs_sorted) - 1)
        wasted += waste
        groups.append({
            "hash": d,
            "size": fs_sorted[0]["size"],
            "wasted": waste,
            "files": fs_sorted,
        })
    groups.sort(key=lambda g: g["wasted"], reverse=True)

    report = {
        "scanned_at": datetime.now().isoformat(timespec="seconds"),
        "root": root,
        "min_size": min_size,
        "min_age_days": min_age_days,
        "files_seen": len(files),
        "truncated": truncated,
        "groups": groups,
        "wasted_bytes": wasted,
        "group_count": len(groups),
    }
    _save_report(report)

    with _lock:
        _scan_state.update(
            status="done", phase="", groups=len(groups),
            wasted_bytes=wasted,
            finished_at=datetime.now().isoformat(timespec="seconds"),
        )


def start_scan(root: str, min_mb: int = 1, min_age_days: int = 7) -> dict:
    """启动后台扫描。root 合法性（绝对路径/卷内）由 app.py 调用方校验。"""
    root = (root or "").strip()
    norm = os.path.normpath(root)
    if not os.path.isabs(root) or ".." in norm.split(os.sep):
        raise DuplicateError("扫描路径必须是绝对路径且不允许包含 ..")
    with _lock:
        if _scan_state["status"] == "scanning":
            raise DuplicateError("已有扫描正在进行，请等它结束")
        _scan_state["status"] = "scanning"
    worker = _scan_worker_windows if sys.platform == "win32" else _scan_worker
    t = threading.Thread(
        target=worker,
        args=(root, max(0, int(min_mb)) * 1024 * 1024, max(0, int(min_age_days))),
        daemon=True,
    )
    t.start()
    return {"ok": True, "started": True, "root": root}


# ---------------------------------------------------------------------------
# 隔离（软删除）与恢复
# ---------------------------------------------------------------------------

def _norm(p: str) -> str:
    if sys.platform == "win32":
        return os.path.normpath(p)
    return posixpath.normpath(p)


def _windows_noop(op: str) -> None:
    raise DuplicateError(
        f"Windows 平台暂不支持「{op}」操作。重复文件扫描可在 Windows 使用，"
        "清理隔离请先在 NAS/Linux 控制台扫描后操作，或等待后续版本适配。"
    )


def quarantine_files(paths: list[str], confirm: bool) -> dict:
    """把用户勾选的重复文件移入隔离目录（同卷 rename，瞬时、可恢复）。

    护栏：
      - confirm 必须 True；
      - 每个路径必须出现在当前报告里（防止接口滥用删任意文件）；
      - 同一组（同哈希）至少保留一份，不允许一次全清。
    """
    if confirm is not True:
        raise DuplicateError("隔离操作需要 confirm=true 确认参数")
    if not isinstance(paths, list) or not paths:
        raise DuplicateError("缺少要隔离的文件列表")
    if sys.platform == "win32":
        _windows_noop("隔离")

    report = load_report()
    if not report or not report.get("groups"):
        raise DuplicateError("没有可用的重复文件报告，请先执行扫描")

    root = _norm(report["root"])
    known: dict[str, dict] = {}
    for g in report["groups"]:
        for f in g["files"]:
            known[_norm(f["path"])] = g

    selected: list[tuple[str, dict]] = []
    seen = set()
    for p in paths:
        pn = _norm(str(p))
        if pn in seen:
            continue
        seen.add(pn)
        if pn not in known:
            raise DuplicateError(f"文件不在当前报告内，拒绝操作: {p}")
        if not pn.startswith(root + "/"):
            raise DuplicateError(f"路径越权（不在扫描根内）: {p}")
        selected.append((pn, known[pn]))

    # 护栏：每组至少保留一份
    group_total: Counter = Counter()
    group_selected: Counter = Counter()
    for g in report["groups"]:
        for f in g["files"]:
            group_total[g["hash"]] += 1
    for pn, g in selected:
        group_selected[g["hash"]] += 1
    for h, cnt in group_selected.items():
        if cnt >= group_total[h]:
            raise DuplicateError(
                "同一组重复内容必须至少保留一份，请取消勾选组内任意一个文件后重试"
            )

    batch = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    qdir = posixpath.join(root, ".nassafe-quarantine", batch)
    q = shlex.quote
    client = _client()
    moved: list[dict] = []
    try:
        lines = [f"mkdir -p {q(qdir)}"]
        for i, (pn, g) in enumerate(selected):
            dst = posixpath.join(qdir, f"{i:04d}__{posixpath.basename(pn)}")
            lines.append(
                f"if mv -f -- {q(pn)} {q(dst)} 2>/dev/null; then "
                f"printf 'OK\\t%s\\t%s\\t%s\\t%s\\n' {q(pn)} {q(dst)} "
                f"{g['hash']} {g['size']}; "
                f"else printf 'FAIL\\t%s\\n' {q(pn)}; fi"
            )
        out = client.run_shell(" ; ".join(lines))
        for line in out.splitlines():
            parts = line.split("\t")
            if parts and parts[0] == "OK" and len(parts) == 5:
                moved.append({
                    "id": uuid.uuid4().hex[:12],
                    "original": parts[1],
                    "quarantined": parts[2],
                    "hash": parts[3],
                    "size": int(parts[4]) if parts[4].isdigit() else None,
                    "batch": batch,
                    "time": datetime.now().isoformat(timespec="seconds"),
                })
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass

    if not moved:
        raise DuplicateError("隔离失败：没有任何文件移动成功（文件可能已被移动或删除）")

    log = load_quarantine_log()
    log.setdefault("entries", []).extend(moved)
    _save_quarantine_log(log)

    # 报告里同步摘掉已隔离的文件，空组删除
    moved_set = {m["original"] for m in moved}
    for g in report["groups"]:
        g["files"] = [f for f in g["files"] if f["path"] not in moved_set]
    report["groups"] = [g for g in report["groups"] if len(g["files"]) >= 2]
    report["group_count"] = len(report["groups"])
    _save_report(report)

    failed = [p for p in paths if _norm(str(p)) not in moved_set]
    return {
        "ok": True,
        "quarantined": len(moved),
        "failed": failed,
        "batch": batch,
        "recoverable": "隔离目录保留全部原文件，可随时恢复",
    }


def restore_files(ids: list[str], confirm: bool) -> dict:
    """把隔离区里的文件恢复到原位置。"""
    if confirm is not True:
        raise DuplicateError("恢复操作需要 confirm=true 确认参数")
    if not isinstance(ids, list) or not ids:
        raise DuplicateError("缺少要恢复的条目 ID")
    if sys.platform == "win32":
        _windows_noop("恢复")

    log = load_quarantine_log()
    entries = {e["id"]: e for e in log.get("entries", [])}
    targets = []
    for i in ids:
        e = entries.get(str(i))
        if not e:
            raise DuplicateError(f"找不到隔离记录: {i}")
        targets.append(e)

    q = shlex.quote
    client = _client()
    restored: list[str] = []
    try:
        for e in targets:
            parent = posixpath.dirname(e["original"])
            script = (
                f"mkdir -p {q(parent)} ; "
                f"if [ -e {q(e['original'])} ]; then "
                f"DST={q(e['original'])}.$(date +%s) ; "
                f"else DST={q(e['original'])} ; fi ; "
                f"if mv -f -- {q(e['quarantined'])} \"$DST\" 2>/dev/null; then "
                f"printf 'OK\\t%s\\n' \"$DST\"; else printf 'FAIL\\n'; fi"
            )
            out = client.run_shell(script)
            if "OK" in out:
                restored.append(e["id"])
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass

    if restored:
        log["entries"] = [e for e in log.get("entries", []) if e["id"] not in set(restored)]
        _save_quarantine_log(log)
    failed = [i for i in ids if str(i) not in set(restored)]
    return {"ok": True, "restored": len(restored), "failed": failed}


def quarantine_list() -> dict:
    log = load_quarantine_log()
    entries = log.get("entries", [])
    total = sum(e.get("size") or 0 for e in entries)
    return {"ok": True, "entries": entries, "count": len(entries),
            "total_bytes": total}


def purge_quarantine(ids: list, confirm: bool) -> dict:
    """彻底删除隔离区里的文件（不可恢复）。

    护栏：
      - confirm 必须 True；
      - 只允许删除隔离日志里登记的条目；
      - 磁盘路径必须位于「.nassafe-quarantine」目录内，且文件名必须是
        「4位序号__原文件名」的隔离格式（双保险，防接口滥用删任意文件）。
    """
    if confirm is not True:
        raise DuplicateError("彻底删除需要 confirm=true 确认参数")
    if not isinstance(ids, list) or not ids:
        raise DuplicateError("缺少要删除的条目 ID")
    if sys.platform == "win32":
        _windows_noop("彻底删除")

    log = load_quarantine_log()
    entries = {e["id"]: e for e in log.get("entries", [])}
    targets = []
    for i in ids:
        e = entries.get(str(i))
        if not e:
            raise DuplicateError(f"找不到隔离记录: {i}")
        qp = _norm(e["quarantined"])
        # 双保险：路径必须落在 .nassafe-quarantine 内，且为「0000__原名」格式
        if "/.nassafe-quarantine/" not in qp or ".." in qp.split("/"):
            raise DuplicateError(f"路径不在隔离目录内，拒绝删除: {e['quarantined']}")
        fname = posixpath.basename(qp)
        orig_name = posixpath.basename(_norm(e["original"]))
        if not (len(fname) > 5 and fname[:4].isdigit() and fname[4:6] == "__"
                and fname[6:] == orig_name):
            raise DuplicateError(f"隔离文件名格式异常，拒绝删除: {e['quarantined']}")
        targets.append(e)

    q = shlex.quote
    client = _client()
    purged: list[str] = []
    freed = 0
    try:
        lines = []
        for e in targets:
            lines.append(
                f"if rm -f -- {q(_norm(e['quarantined']))} 2>/dev/null; then "
                f"printf 'OK\\t%s\\n' {q(e['id'])}; else printf 'FAIL\\t%s\\n' {q(e['id'])}; fi"
            )
        out = client.run_shell(" ; ".join(lines))
        ok_ids = set()
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) == 2 and parts[0] == "OK":
                ok_ids.add(parts[1])
        purged = [e["id"] for e in targets if e["id"] in ok_ids]
        freed = sum(e.get("size") or 0 for e in targets if e["id"] in ok_ids)

        # 清掉空批次目录（只删空目录，误删风险为零）
        if purged:
            qroots = set()
            for e in targets:
                qp = _norm(e["quarantined"])
                idx = qp.find("/.nassafe-quarantine/")
                if idx > 0:
                    qroots.add(qp[:idx + len("/.nassafe-quarantine")])
            for qr in qroots:
                client.run_shell(
                    f"find {q(qr)} -mindepth 1 -type d -empty -delete 2>/dev/null ; true")
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass

    if purged:
        log["entries"] = [e for e in log.get("entries", []) if e["id"] not in set(purged)]
        _save_quarantine_log(log)
    failed = [str(i) for i in ids if str(i) not in set(purged)]
    return {"ok": True, "purged": len(purged), "failed": failed,
            "freed_bytes": freed}
