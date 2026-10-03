"""
TS Safe — AI 解读模块（仅标准库，零第三方依赖）

定位（见 PRODUCT.md §五）：AI 是"翻译官 + 检索员 + 提案人"，不是"决策者"。
  - 把 SMART / 快照 / 体检 / 告警数据翻译成人话 + 给处置建议
  - 不做判断"是不是勒索攻击"（规则引擎更准更快可解释）

供应商（全部 OpenAI 兼容 chat/completions 接口）：
  - deepseek  云端  https://api.deepseek.com/v1
  - openai    云端  https://api.openai.com/v1
  - qwen      云端  https://dashscope.aliyuncs.com/compatible-mode/v1
  - zhipu     云端  https://open.bigmodel.cn/api/paas/v4
  - ollama    本地  http://localhost:11434/v1（一键本地 AI，无需 key）

配置存于 state 目录的 ai.json，**绝不进仓库**；未启用 / 无 key 时
interpret() 返回 None（前端据此隐藏按钮），核心功能零影响。
"""

from __future__ import annotations

import json
import os
import urllib.request
import urllib.error

import storage  # 复用 state_dir()


# 各供应商默认 base_url 与模型（本地 Ollama 不需要 key）
PROVIDERS = {
    "deepseek": {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat", "needs_key": True, "embed_model": ""},
    "openai":   {"base_url": "https://api.openai.com/v1",   "model": "gpt-4o-mini",  "needs_key": True, "embed_model": "text-embedding-3-small"},
    "qwen":     {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus", "needs_key": True, "embed_model": "text-embedding-v2"},
    "zhipu":    {"base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash", "needs_key": True, "embed_model": "embedding-2"},
    "qiniu":    {"base_url": "https://api.qnaigc.com/v1",  "model": "deepseek-v3", "needs_key": True, "embed_model": ""},
    "ollama":   {"base_url": "http://localhost:11434/v1", "model": "qwen2.5:7b", "needs_key": False, "embed_model": "nomic-embed-text"},
}


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def config_path() -> str:
    return os.path.join(storage.state_dir(), "ai.json")


def load_config() -> dict:
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"enabled": False, "provider": "deepseek", "base_url": "", "model": "", "api_key": "", "local_mode": False}


def save_config(cfg: dict) -> None:
    os.makedirs(storage.state_dir(), exist_ok=True)
    with open(config_path(), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def is_ready() -> bool:
    """是否已配置可用（供前端判断是否显示 AI 按钮）。"""
    cfg = load_config()
    if not cfg.get("enabled"):
        return False
    prov = cfg.get("provider", "deepseek")
    info = PROVIDERS.get(prov, {})
    if info.get("needs_key") and not cfg.get("api_key"):
        return False
    return True


# ---------------------------------------------------------------------------
# 本地 AI 自动发现（自动搜索 + 自动选最优模型）
#
# 支持：Ollama(11434) / LM Studio(1234) / llama.cpp(8080) / vLLM(8000) /
#       LocalAI(8080) / Xinference(9997) / LiteLLM(4000) —— 全部 OpenAI 兼容。
# 选优：本地拿不到跑分，用确定性代理 —— 模型体积(字节) + 名称参数量(70b>7b)
#       + 优质家族加成 + 排除 embedding/rerank 类，选综合分最高的。
# ---------------------------------------------------------------------------

_LOCAL_PORTS = [11434, 1234, 8080, 8000, 9997, 4000]
_LAN_SCAN_PORTS = [11434, 1234]  # 局域网全段只扫最常见的两个端口，控制耗时
_OLLAMA_UA = "Mozilla/5.0 (compatible; TS Safe/1.0)"


def _http_json(url: str, timeout: float = 2.5):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", _OLLAMA_UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _identify(host: str, port: int):
    """识别 host:port 上跑的本地 AI 服务。返回 {kind, base_url, models} 或 None。"""
    base = f"http://{host}:{port}"

    # Ollama 原生接口（带模型体积，选优最准）
    try:
        body = _http_json(base + "/api/tags", 2.0)
        models = [
            {"name": m.get("name"), "size_b": m.get("size")}
            for m in body.get("models", []) if m.get("name")
        ]
        if models:
            return {"kind": "ollama", "base_url": base + "/v1", "models": models}
    except Exception:  # noqa: BLE001
        pass

    # OpenAI 兼容 /v1/models（LM Studio / llama.cpp / vLLM / LocalAI / Xinference / LiteLLM）
    try:
        body = _http_json(base + "/v1/models", 2.0)
        ids = [d.get("id") for d in body.get("data", []) if d.get("id")]
        if ids:
            return {"kind": "openai", "base_url": base + "/v1",
                    "models": [{"name": i} for i in ids]}
    except Exception:  # noqa: BLE001
        pass
    return None


_GOOD_FAMILIES = ("qwen", "deepseek", "llama", "glm", "mistral", "phi", "gemma", "gpt")


def _model_score(name: str, size_b) -> float:
    """模型优选打分。-1 = 排除（embedding/rerank/whisper 等非对话模型）。"""
    import re

    n = name or ""
    if re.search(r"embed|bge-|rerank|nomic|clip|whisper|vision|guard", n, re.I):
        return -1.0
    score = 0.0
    if size_b:
        score += float(size_b) / 1e9          # Ollama 真实体积(GB)
    m = re.search(r"(\d+(?:\.\d+)?)\s*[bB](?![a-zA-Z0-9])", n)
    if m:
        score += float(m.group(1)) * 2        # 名称里的参数量权重更高
    low = n.lower()
    if any(k in low for k in _GOOD_FAMILIES):
        score += 3.0
    if "instruct" in low or "chat" in low:
        score += 1.0
    return score


def _pick_best(models: list):
    """从模型列表里选综合分最高的；全被排除时退化为第一个。"""
    best, best_s = None, float("-inf")
    for m in models or []:
        s = _model_score(m.get("name"), m.get("size_b"))
        if s > best_s:
            best, best_s = m, s
    if best is None and models:
        best = models[0]
    return (best or {}).get("name")


def _tcp_scan(hosts: list, ports: list, timeout: float = 1.0, join_s: float = 6.0) -> set:
    """并发 TCP 探测，返回存活的 (host, port) 集合。"""
    import socket
    import threading

    alive: set = set()
    lock = threading.Lock()

    def _probe(h: str, p: int) -> None:
        s = socket.socket()
        s.settimeout(timeout)
        try:
            s.connect((h, p))
            s.close()
            with lock:
                alive.add((h, p))
        except OSError:
            pass

    threads = [threading.Thread(target=_probe, args=(h, p), daemon=True)
               for h in hosts for p in ports]
    for t in threads:
        t.start()
    for t in threads:
        t.join(join_s)
    return alive


def discover_local() -> dict:
    """搜索本机/局域网/NAS 上的本地 AI 服务，自动选出每个服务上的最优模型。

    三步：① 常见候选主机 × 全端口集（NAS IP / 容器网关 / host.docker.internal）
          ② NAS 网段受限扫描（只扫 11434/1234，约 5-8 秒）
          ③ 经 NAS 本机通道探测（服务只监听 127.0.0.1 时容器不可达，单独提示）"""
    found: list = []
    seen: set = set()  # 已识别的 (host, port)

    # ① 候选主机 × 全端口集
    cand_hosts = ["host.docker.internal", "172.17.0.1", "172.18.0.1"]
    nas_ip = os.environ.get("NASSAFE_QNAP_HOST") or os.environ.get("NASSAFE_HOST")
    if nas_ip and nas_ip not in ("127.0.0.1", "localhost"):
        cand_hosts.insert(0, nas_ip)
    for h, p in sorted(_tcp_scan(cand_hosts, _LOCAL_PORTS)):
        info = _identify(h, p)
        if info:
            info["recommended"] = _pick_best(info["models"])
            info["via"] = "candidate"
            found.append(info)
            seen.add((h, p))

    # ② NAS 网段受限扫描
    if nas_ip:
        try:
            import ipaddress

            net = ipaddress.ip_network(f"{nas_ip}/24", strict=False)
            hosts = [str(h) for h in net.hosts() if str(h) != nas_ip]
            for h, p in sorted(_tcp_scan(hosts, _LAN_SCAN_PORTS)):
                if (h, p) in seen:
                    continue
                info = _identify(h, p)
                if info:
                    info["recommended"] = _pick_best(info["models"])
                    info["via"] = "lan"
                    found.append(info)
        except Exception:  # noqa: BLE001 网段计算/扫描失败不致命
            pass

    # ③ NAS 本机通道（经 SSH 在 NAS 上探测，能发现"只监听 127.0.0.1"的情况）
    try:
        from qnap import default_client

        script = ("curl -s -m 2 http://127.0.0.1:11434/api/tags 2>/dev/null; "
                  "echo '---'; "
                  "curl -s -m 2 http://127.0.0.1:1234/v1/models 2>/dev/null")
        out = default_client().run_shell(script)
        parts = out.split("---")
        nas_models = None
        if parts and parts[0].strip():
            try:
                body = json.loads(parts[0].strip())
                nas_models = [{"name": m.get("name"), "size_b": m.get("size")}
                              for m in body.get("models", []) if m.get("name")]
            except Exception:  # noqa: BLE001
                pass
        if nas_models and nas_ip:
            reachable = any(f["base_url"].startswith(f"http://{nas_ip}:") for f in found)
            if not reachable:
                found.append({
                    "kind": "ollama",
                    "base_url": f"http://{nas_ip}:11434/v1",
                    "models": nas_models,
                    "recommended": _pick_best(nas_models),
                    "via": "nas_local",
                    "hint": "本地 AI 服务在 NAS 上运行，但只监听了 127.0.0.1，容器内可能连不上；"
                            "请设置 OLLAMA_HOST=0.0.0.0 后重启服务（DeployEasy 部署的默认可达）",
                })
    except Exception:  # noqa: BLE001
        pass

    return {"found": found}


# ---------------------------------------------------------------------------
# 对话调用（统一 OpenAI 兼容接口）
# ---------------------------------------------------------------------------

def _ollama_default_base() -> str:
    """Ollama 默认地址兜底：容器里 localhost 连不到宿主，优先用 NAS IP。"""
    ip = os.environ.get("NASSAFE_QNAP_HOST") or os.environ.get("NASSAFE_HOST")
    if ip and ip not in ("127.0.0.1", "localhost"):
        return f"http://{ip}:11434/v1"
    return "http://localhost:11434/v1"


def _chat(messages: list, cfg: dict, timeout: int = 120) -> (str, str):
    prov = cfg.get("provider", "deepseek")
    info = PROVIDERS.get(prov, PROVIDERS["deepseek"])
    base = (cfg.get("base_url") or info["base_url"]).rstrip("/")
    if prov == "ollama" and "localhost" in base:
        base = _ollama_default_base()  # 配置缺 base_url 时兜底到 NAS IP
    if prov != "ollama" and ":11434" in base:
        # 云端供应商却配了本地 Ollama 地址（端口 11434）——历史残留串配置，忽略之
        base = info["base_url"].rstrip("/")
    model = cfg.get("model") or info["model"]
    api_key = cfg.get("api_key", "")
    url = f"{base}/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 1200,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
        return body["choices"][0]["message"]["content"].strip(), ""
    except urllib.error.URLError as exc:
        return "", f"网络错误: {exc}"
    except Exception as exc:  # noqa: BLE001
        return "", f"调用失败: {exc}"


# ---------------------------------------------------------------------------
# 对外：解读
# ---------------------------------------------------------------------------

_SYSTEM = (
    "你是 NAS 数据安全的助手。用户是 NAS（网络存储）普通玩家，不是工程师。"
    "请用通俗中文解释下面的数据，指出风险等级，并给出 2-3 条可立即执行的处置建议。"
    "不要使用过于专业的术语，必要时举例。控制在 300 字以内。"
)


def interpret(report_text: str) -> (str, str):
    """把一份体检 / 告警报告翻译成人话。返回 (文本, 错误)。未配置返回 (None, '')。"""
    cfg = load_config()
    if not is_ready():
        return None, ""
    text = (report_text or "").strip()
    if not text:
        return None, "没有可解读的内容"
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": f"以下是 TS Safe 的数据，请解读：\n\n{text}"},
    ]
    return _chat(messages, cfg)


# ---- 进阶能力（预留，后续接自然语言搜文件 / 规则建议） --------------------

def answer(question: str, context: str = "", history: list | None = None) -> (str, str):
    cfg = load_config()
    if not is_ready():
        return None, ""
    messages = [
        {"role": "system", "content": "你是 NAS 数据安全助手，用通俗中文回答。"},
    ]
    # 多轮对话历史：只认 user/assistant 两种角色，单条截断 4000 字、最多 20 条防爆
    for m in (history or [])[-20:]:
        if isinstance(m, dict) and m.get("role") in ("user", "assistant") \
                and isinstance(m.get("content"), str) and m["content"].strip():
            messages.append({"role": m["role"], "content": m["content"][:4000]})
    messages.append({"role": "user", "content": (f"背景：{context}\n\n问题：{question}" if context else question)})
    return _chat(messages, cfg)


# ---------------------------------------------------------------------------
# 对外：向量嵌入（RAG 知识库用，OpenAI 兼容 /embeddings）
# ---------------------------------------------------------------------------

def embed(text: str, cfg: dict | None = None, timeout: int = 60) -> list:
    """文本 -> 向量。未配置 / 供应商不支持 / 调用失败均返回空列表。"""
    cfg = cfg or load_config()
    if not is_ready():
        return []
    prov = cfg.get("provider", "deepseek")
    info = PROVIDERS.get(prov, PROVIDERS["deepseek"])
    base = (cfg.get("base_url") or info["base_url"]).rstrip("/")
    if prov == "ollama" and "localhost" in base:
        base = _ollama_default_base()
    # 嵌入模型优先级：配置项 embed_model > 供应商默认 > 对话模型
    model = (cfg.get("embed_model") or info.get("embed_model")
             or cfg.get("model") or info["model"])
    api_key = cfg.get("api_key", "")
    url = f"{base}/embeddings"
    payload = {"model": model, "input": text}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
        emb = (body.get("data") or [{}])[0].get("embedding") or []
        return emb if isinstance(emb, list) else []
    except urllib.error.URLError as exc:
        return []
    except Exception:  # noqa: BLE001
        return []
