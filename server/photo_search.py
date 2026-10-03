"""三期：照片语义搜索（专业版专属）——视觉模型描述 + 文本 embedding + 余弦检索。

原理：
  1) 扫描照片目录（jpg/jpeg/png/webp，浅递归）
  2) 每张照片用当前 AI 供应商的**视觉模型**生成一句中文描述（glm-4v-flash / qwen-vl-plus / gpt-4o-mini）
  3) 描述文本走既有 ai.embed 向量化，与照片路径、修改时间一起存 SQLite
  4) 搜索时把查询文本同样向量化，余弦相似度排序返回
批量：每次 index 调用处理一小批（默认 10 张），前端循环调用显示进度，避免单请求超时。
缩略图：GET /api/ai/photo/thumb?path=... 只放行「已在索引里登记过」的文件。
"""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import time
import urllib.request

from storage import StorageError
import ai
from knowledge import _vec_serialize, _vec_deserialize

STATUS = "live"
SUPPORTED_EXT = (".jpg", ".jpeg", ".png", ".webp")
_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
_MAX_FILE_BYTES = 6 * 1024 * 1024      # 单张超过 6MB 跳过（节省视觉模型流量）
_MAX_PHOTOS = 2000                     # 索引总量护栏
_MAX_DEPTH = 4

# 各供应商的视觉模型（deepseek/qiniu 无视觉模型，ollama 取决于用户装的模型不承诺）
VISION_MODELS = {
    "zhipu": "glm-4v-flash",
    "qwen": "qwen-vl-plus",
    "openai": "gpt-4o-mini",
    "deepseek": "",
    "qiniu": "",
    "ollama": "",
}

_DESC_PROMPT = ("用一句中文描述这张照片：主体、场景、颜色、氛围，不超过60字，"
                "不要出现'这张照片'四个字，直接描述内容。")


# ---------------------------------------------------------------------------
# 存储层
# ---------------------------------------------------------------------------

def _db_path() -> str:
    from storage import state_dir
    return os.path.join(state_dir(), "photo_index.db")


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_db_path()), exist_ok=True)
    c = sqlite3.connect(_db_path())
    c.execute("""CREATE TABLE IF NOT EXISTS photos(
        path TEXT PRIMARY KEY, root TEXT, mtime INTEGER,
        desc TEXT, emb BLOB, dim INTEGER, created TEXT)""")
    return c


def status() -> dict:
    c = _conn()
    n = c.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
    roots = [r[0] for r in c.execute("SELECT DISTINCT root FROM photos")]
    c.close()
    return {"ok": True, "status": STATUS, "indexed": n, "roots": roots}


def scan_library(root: str) -> list:
    """枚举目录下的图片（浅递归 + 总量护栏），返回绝对路径列表。"""
    if not root or not os.path.isdir(root):
        raise StorageError("目录不存在：%s" % root)
    files = []
    root = os.path.abspath(root)
    for dirpath, dirs, names in os.walk(root):
        depth = dirpath[len(root):].count(os.sep)
        if depth >= _MAX_DEPTH:
            dirs[:] = []
        for n in names:
            if n.lower().endswith(SUPPORTED_EXT):
                files.append(os.path.join(dirpath, n))
                if len(files) >= _MAX_PHOTOS:
                    dirs[:] = []
                    break
        if len(files) >= _MAX_PHOTOS:
            break
    files.sort()
    return files


# ---------------------------------------------------------------------------
# 视觉描述 + 向量化
# ---------------------------------------------------------------------------

def _vision_ready() -> tuple:
    """返回 (provider, base_url, api_key, vision_model)；不支持时给出中文错误。"""
    cfg = ai.load_config()
    if not ai.is_ready():
        raise StorageError("AI 未配置，请先到「设置 → AI 配置」启用")
    prov = cfg.get("provider", "deepseek")
    model = VISION_MODELS.get(prov, "")
    if not model:
        raise StorageError(
            "当前 AI 供应商（%s）不支持看图。请在「设置 → AI 配置」切换到智谱 / 通义千问 / OpenAI 后重试" % prov)
    info = ai.PROVIDERS.get(prov, {})
    base = (cfg.get("base_url") or info.get("base_url") or "").rstrip("/")
    return prov, base, cfg.get("api_key", ""), model


def _describe_image(path: str) -> str:
    prov, base, api_key, model = _vision_ready()
    ext = os.path.splitext(path)[1].lower()
    mime = _MIME.get(ext, "image/jpeg")
    sz = os.path.getsize(path)
    if sz > _MAX_FILE_BYTES:
        raise StorageError("图片超过 6MB，已跳过")
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    body = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": _DESC_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ],
        }],
        "max_tokens": 120,
        "temperature": 0.2,
    }
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"})
    if api_key:
        req.add_header("Authorization", "Bearer " + api_key)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            d = json.loads(resp.read().decode("utf-8", "ignore"))
    except Exception as e:  # noqa: BLE001
        raise StorageError("视觉模型调用失败：%s" % str(e)[:160])
    try:
        return (d["choices"][0]["message"]["content"] or "").strip()
    except Exception:  # noqa: BLE001
        raise StorageError("视觉模型返回格式异常：" + json.dumps(d, ensure_ascii=False)[:160])


# ---------------------------------------------------------------------------
# 索引 / 检索
# ---------------------------------------------------------------------------

def _pending(c, root: str, files: list) -> list:
    """过滤出未索引或已变更的文件。"""
    out = []
    for p in files:
        try:
            mt = int(os.path.getmtime(p))
        except OSError:
            continue
        row = c.execute("SELECT mtime FROM photos WHERE path=?", (p,)).fetchone()
        if row is None or row[0] != mt:
            out.append((p, mt))
    return out


def index_gallery(root: str, batch: int = 10) -> dict:
    """处理一小批未索引照片；前端循环调用直到 remaining=0。"""
    root = os.path.abspath((root or "").strip())
    files = scan_library(root)
    c = _conn()
    try:
        pending = _pending(c, root, files)
        done_n = 0
        last_error = ""
        for p, mt in pending[:max(1, int(batch))]:
            try:
                desc = _describe_image(p)
                emb = ai.embed(desc)
                if not emb:
                    raise StorageError("向量生成失败（检查 AI 配置与供应商向量支持）")
                c.execute("INSERT OR REPLACE INTO photos(path,root,mtime,desc,emb,dim,created) "
                          "VALUES(?,?,?,?,?,?,?)",
                          (p, root, mt, desc, _vec_serialize(emb), len(emb),
                           time.strftime("%Y-%m-%d %H:%M:%S")))
                c.commit()
                done_n += 1
            except StorageError as e:
                last_error = str(e)
            except Exception as e:  # noqa: BLE001
                last_error = str(e)[:160]
        remaining = len(_pending(c, root, files))
        return {"ok": True, "scanned": len(files), "indexed_now": done_n,
                "indexed_total": c.execute("SELECT COUNT(*) FROM photos").fetchone()[0],
                "remaining": remaining, "last_error": last_error,
                "done": remaining == 0}
    finally:
        c.close()


def _cosine(a: list, b: list) -> float:
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if not na or not nb:
        return 0.0
    return dot / (na * nb)


def semantic_search(query: str, top_k: int = 12) -> dict:
    q = (query or "").strip()
    if not q:
        raise StorageError("请输入搜索内容")
    qv = ai.embed(q)
    if not qv:
        raise StorageError("查询向量化失败：AI 未配置或当前供应商不支持向量（智谱/通义/OpenAI 支持）")
    c = _conn()
    rows = c.execute("SELECT path, desc, emb FROM photos").fetchall()
    c.close()
    scored = []
    for path, desc, blob in rows:
        try:
            emb = _vec_deserialize(blob)
        except Exception:  # noqa: BLE001
            continue
        s = _cosine(qv, emb)
        if s > 0.05:
            scored.append({"path": path, "desc": desc, "score": round(s, 4)})
    scored.sort(key=lambda x: -x["score"])
    return {"ok": True, "query": q, "total_indexed": len(rows), "results": scored[:top_k]}


def thumb_ok(path: str) -> bool:
    """缩略图放行校验：必须是索引里登记过的文件。"""
    if not path:
        return False
    c = _conn()
    row = c.execute("SELECT 1 FROM photos WHERE path=?", (os.path.abspath(path),)).fetchone()
    c.close()
    return row is not None
