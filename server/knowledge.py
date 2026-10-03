"""二期：RAG 知识库（家庭版 / 专业版专属）。

设计：
  - SQLite 向量库（纯 Python 余弦相似度，零 numpy 依赖）
  - 文本分块 -> embedding（复用 ai.embed，OpenAI 兼容 /embeddings 接口）
  - 摄入：txt / md / srt 原生解析；pdf / docx 走可选依赖（缺则明确报错）
  - 查询：向量召回 top-k 片段 + LLM 基于上下文作答，并回传来源文档
  - 向量维度自适应（以首次写入的维度为准）

embedding 供应商要求：
  - 必须支持 OpenAI 兼容 /embeddings 接口。
  - 云端：阿里云 qwen 的 text-embedding-v2/v3 可用（在 AI 设置里把「嵌入模型」填上）。
  - 本地：Ollama 的 nomic-embed-text 等嵌入模型（普通对话模型如 qwen2.5:7b 不能做嵌入）。
  - deepseek 等无 embeddings 接口的供应商会返回空向量，摄入会被拦下并提示换供应商。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time

import ai
import storage

# -1 表示不限制
UNLIMITED = -1


# ---------------------------------------------------------------------------
# 路径 / 配额
# ---------------------------------------------------------------------------

def _state_dir() -> str:
    try:
        return storage.state_dir()
    except Exception:  # noqa: BLE001
        return os.path.join(os.getcwd(), "state")


def _db_path() -> str:
    os.makedirs(_state_dir(), exist_ok=True)
    return os.path.join(_state_dir(), "knowledge.db")


def _quota_path() -> str:
    return os.path.join(_state_dir(), "kb_quota.json")


def _now_month() -> str:
    return time.strftime("%Y-%m")


def quota_remaining() -> int:
    """当前月份剩余知识库调用次数。-1 表示不限量。"""
    try:
        from editions import limits
        q = limits().get("kb_quota", 0)
    except Exception:  # noqa: BLE001
        q = 0
    if q == UNLIMITED:
        return UNLIMITED
    try:
        with open(_quota_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return q
    if data.get("month") != _now_month():
        return q
    used = int(data.get("used", 0))
    return max(0, q - used)


def quota_check() -> tuple[bool, int]:
    rem = quota_remaining()
    if rem == UNLIMITED:
        return True, rem
    return rem > 0, rem


def quota_incr() -> None:
    try:
        with open(_quota_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    if data.get("month") != _now_month():
        data = {"month": _now_month(), "used": 0}
    data["used"] = int(data.get("used", 0)) + 1
    with open(_quota_path(), "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 向量库
# ---------------------------------------------------------------------------

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_db_path())
    c.execute("""CREATE TABLE IF NOT EXISTS docs(
        id TEXT PRIMARY KEY, title TEXT, source TEXT, kind TEXT,
        chunks INTEGER DEFAULT 0, created REAL, meta TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS chunks(
        id TEXT PRIMARY KEY, doc_id TEXT, idx INTEGER, text TEXT,
        emb BLOB, dim INTEGER)""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_chunks_doc ON chunks(doc_id)")
    return c


def _vec_serialize(v: list) -> bytes:
    return json.dumps(v, separators=(",", ":")).encode("utf-8")


def _vec_deserialize(b) -> list:
    return json.loads(b.decode("utf-8"))


def _cosine(a: list, b: list) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _chunk_text(text: str, size: int = 480, overlap: int = 80) -> list:
    text = (text or "").replace("\r", "\n")
    paras = [p.strip() for p in text.split("\n") if p.strip()]
    chunks: list = []
    buf = ""
    for p in paras:
        if len(buf) + len(p) <= size:
            buf = (buf + "\n" + p).strip()
        else:
            if buf:
                chunks.append(buf)
            if len(p) > size:
                for i in range(0, len(p), size - overlap):
                    chunks.append(p[i:i + size])
                buf = ""
            else:
                buf = p
    if buf:
        chunks.append(buf)
    return [c for c in chunks if c.strip()]


def _embed(text: str) -> list:
    return ai.embed(text)


def ingest_text(title: str, text: str, kind: str = "text") -> dict:
    c = _conn()
    doc_id = "doc_%d" % int(time.time() * 1000)
    chunks = _chunk_text(text)
    dim = None
    cnt = 0
    for i, ch in enumerate(chunks):
        emb = _embed(ch)
        if not emb:
            continue
        dim = len(emb)
        cid = "%s_%d" % (doc_id, i)
        c.execute("INSERT INTO chunks VALUES(?,?,?,?,?,?)",
                  (cid, doc_id, i, ch, _vec_serialize(emb), dim))
        cnt += 1
    if cnt == 0:
        c.close()
        raise ValueError(
            "没有可用的文本向量：当前 AI 供应商不支持 embeddings。"
            "请改用支持嵌入的模型——云端用阿里云 qwen 的 text-embedding，"
            "本地用 Ollama 的 nomic-embed-text（普通对话模型不能做嵌入）。")
    c.execute("INSERT INTO docs VALUES(?,?,?,?,?,?,?)",
              (doc_id, title or "未命名", "", kind, cnt, time.time(), "{}"))
    c.commit()
    c.close()
    return {"id": doc_id, "title": title, "chunks": cnt}


def _strip_srt(text: str) -> str:
    lines = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        if re.match(r"^\d+$", ln):
            continue
        if re.match(r"^\d{2}:\d{2}:\d{2}", ln):
            continue
        lines.append(ln)
    return "\n".join(lines)


def _read_pdf(path: str) -> str:
    try:
        from pdfminer.high_level import extract_text
    except Exception:  # noqa: BLE001
        raise ValueError("PDF 解析需要 pdfminer.six（pip install pdfminer.six），当前环境未安装")
    return extract_text(path) or ""


def _read_docx(path: str) -> str:
    try:
        import docx
    except Exception:  # noqa: BLE001
        raise ValueError("DOCX 解析需要 python-docx（pip install python-docx），当前环境未安装")
    d = docx.Document(path)
    return "\n".join(p.text for p in d.paragraphs if p.text and p.text.strip())


def ingest_file(path: str) -> dict:
    title = os.path.basename(path)
    ext = os.path.splitext(path)[1].lower()
    if ext in (".txt", ".md", ".srt", ".text", ".csv"):
        # csv 用 utf-8-sig：吃掉 Excel 导出常带的 BOM
        enc = "utf-8-sig" if ext == ".csv" else "utf-8"
        with open(path, encoding=enc, errors="replace") as f:
            text = f.read()
        if ext == ".srt":
            text = _strip_srt(text)
    elif ext == ".pdf":
        text = _read_pdf(path)
    elif ext == ".docx":
        text = _read_docx(path)
    elif ext == ".doc":
        raise ValueError("不支持老版 .doc 格式：请先用 Word/WPS 另存为 .docx 再上传")
    else:
        raise ValueError("不支持的文件类型：%s（支持 txt/md/srt/csv/pdf/docx）" % ext)
    return ingest_text(title, text, kind=ext.lstrip("."))


def list_docs() -> list:
    c = _conn()
    rows = c.execute(
        "SELECT id,title,kind,chunks,created FROM docs ORDER BY created DESC").fetchall()
    c.close()
    return [{
        "id": r[0], "title": r[1], "kind": r[2], "chunks": r[3],
        "created": time.strftime("%Y-%m-%d %H:%M", time.localtime(r[4])),
    } for r in rows]


def delete_doc(doc_id: str) -> bool:
    c = _conn()
    c.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
    c.execute("DELETE FROM docs WHERE id=?", (doc_id,))
    c.commit()
    c.close()
    return True


def search(query: str, top_k: int = 6) -> list:
    qv = _embed(query)
    if not qv:
        return []
    c = _conn()
    rows = c.execute("SELECT id,doc_id,text,emb FROM chunks").fetchall()
    c.close()
    scored = []
    for r in rows:
        emb = _vec_deserialize(r[3])
        if len(emb) != len(qv):
            continue
        scored.append((_cosine(qv, emb), r[1], r[2]))
    scored.sort(key=lambda x: -x[0])
    out = []
    for s, doc_id, text in scored:
        if s < 0.05:
            continue
        out.append({"score": round(s, 4), "doc_id": doc_id, "text": text[:400]})
        if len(out) >= top_k:
            break
    return out


def query(question: str, top_k: int = 6) -> tuple:
    """返回 (答案文本, 错误, 来源doc_id列表)。答案来自 LLM 基于召回片段作答。"""
    hits = search(question, top_k)
    if not hits:
        return None, "知识库为空或未命中，请先摄入文档", []
    ctx = "\n\n".join("[资料 %d] %s" % (i + 1, h["text"]) for i, h in enumerate(hits))
    prompt = (
        "请仅基于下面的资料回答用户问题，不要编造资料外的内容；"
        "若资料不足以回答，请明确说明。\n\n资料：\n%s\n\n问题：%s" % (ctx, question))
    ans, err = ai.answer(prompt, history=None)
    sources = []
    for h in hits:
        if h["doc_id"] not in sources:
            sources.append(h["doc_id"])
    if err:
        return None, err, sources
    return ans, "", sources


def doc_titles(ids: list) -> dict:
    if not ids:
        return {}
    c = _conn()
    ph = ",".join("?" * len(ids))
    rows = c.execute("SELECT id,title FROM docs WHERE id IN (%s)" % ph, ids).fetchall()
    c.close()
    return {r[0]: r[1] for r in rows}
