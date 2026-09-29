"""通知 + AI 模块的优雅降级与配置测试（不依赖任何外部密钥 / 网络）。"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import notify
import ai

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}")


def test_notify_disabled_skip():
    # 未启用 → dispatch 返回 skipped，不抛异常、不联网
    os.environ["NASSAFE_STATE_DIR"] = tempfile.mkdtemp(prefix="nas_t_")
    notify.save_config({"enabled": False, "channels": []})
    r = notify.dispatch([{"level": "critical", "title": "x", "detail": "y"}], [])
    check("未启用时 dispatch 返回 skipped", r.get("skipped") is not None)


def test_notify_no_channels():
    os.environ["NASSAFE_STATE_DIR"] = tempfile.mkdtemp(prefix="nas_t_")
    notify.save_config({"enabled": True, "channels": []})
    r = notify.dispatch([], [])
    check("启用但无通道 → sent 为空", r.get("enabled") and r["sent"] == [])


def test_mask_helper_not_used_here():
    # 仅确认模块可导入与入口存在
    check("notify.scan_and_dispatch 可调用", callable(notify.scan_and_dispatch))
    check("notify.start_notifier 可调用", callable(notify.start_notifier))


def test_ai_not_ready_without_key():
    os.environ["NASSAFE_STATE_DIR"] = tempfile.mkdtemp(prefix="nas_t_")
    ai.save_config({"enabled": True, "provider": "deepseek", "api_key": ""})
    check("云端供应商缺 key → is_ready=False", ai.is_ready() is False)
    text, err = ai.interpret("快照异常")
    check("未就绪时 interpret 返回 (None,'')", text is None and err == "")


def test_ai_ollama_ready_without_key():
    os.environ["NASSAFE_STATE_DIR"] = tempfile.mkdtemp(prefix="nas_t_")
    ai.save_config({"enabled": True, "provider": "ollama", "api_key": ""})
    check("本地 ollama 免 key → is_ready=True", ai.is_ready() is True)


def test_ai_disabled_not_ready():
    os.environ["NASSAFE_STATE_DIR"] = tempfile.mkdtemp(prefix="nas_t_")
    ai.save_config({"enabled": False, "provider": "deepseek", "api_key": "x"})
    check("未启用 → is_ready=False", ai.is_ready() is False)


if __name__ == "__main__":
    print("== test_notify_ai ==")
    test_notify_disabled_skip()
    test_notify_no_channels()
    test_mask_helper_not_used_here()
    test_ai_not_ready_without_key()
    test_ai_ollama_ready_without_key()
    test_ai_disabled_not_ready()
    print(f"\n通过 {PASS} / 失败 {FAIL}")
    sys.exit(1 if FAIL else 0)
