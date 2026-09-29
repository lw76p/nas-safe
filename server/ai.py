"""
NAS Safe — AI 解读模块（仅标准库，零第三方依赖）

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
    "deepseek": {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat", "needs_key": True},
    "openai":   {"base_url": "https://api.openai.com/v1",   "model": "gpt-4o-mini",  "needs_key": True},
    "qwen":     {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus", "needs_key": True},
    "zhipu":    {"base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash", "needs_key": True},
    "ollama":   {"base_url": "http://localhost:11434/v1", "model": "qwen2.5:7b", "needs_key": False},
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
# 对话调用（统一 OpenAI 兼容接口）
# ---------------------------------------------------------------------------

def _chat(messages: list, cfg: dict, timeout: int = 30) -> (str, str):
    prov = cfg.get("provider", "deepseek")
    info = PROVIDERS.get(prov, PROVIDERS["deepseek"])
    base = (cfg.get("base_url") or info["base_url"]).rstrip("/")
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
        {"role": "user", "content": f"以下是 NAS Safe 的数据，请解读：\n\n{text}"},
    ]
    return _chat(messages, cfg)


# ---- 进阶能力（预留，后续接自然语言搜文件 / 规则建议） --------------------

def answer(question: str, context: str = "") -> (str, str):
    cfg = load_config()
    if not is_ready():
        return None, ""
    messages = [
        {"role": "system", "content": "你是 NAS 数据安全助手，用通俗中文回答。"},
        {"role": "user", "content": (f"背景：{context}\n\n问题：{question}" if context else question)},
    ]
    return _chat(messages, cfg)
