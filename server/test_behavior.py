#!/usr/bin/env python3
"""v3 勒索行为检测单元测试（不依赖真实 NAS）。

覆盖：
  - shannon_entropy 基本性质（随机字节高熵 / 全 0 低熵）
  - analyze_path 对不存在目录 / 勒索扩展名的统计
  - detect_behavior 的扩展名突变、批量改名、熵值骤升三类信号与 suspicious 判定
"""
import os
import sys
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import behavior


PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[通过] {name}")
    else:
        FAIL += 1
        print(f"[失败] {name} {extra}")


def _mkfile(path, content):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)


def test_entropy_properties():
    high = behavior.shannon_entropy(os.urandom(4096))
    low = behavior.shannon_entropy(b"\x00" * 4096)
    text = behavior.shannon_entropy(b"hello world " * 200)
    check("随机字节熵接近 8", high > 7.0, str(high))
    check("全 0 字节熵接近 0", low < 0.1, str(low))
    check("文本熵介于两者之间", low < text < high, str(text))


def test_analyze_nonexistent():
    r = behavior.analyze_path("/no/such/dir")
    check("不存在目录 exists=False", r.get("exists") is False)


def test_analyze_ransom_ext():
    d = tempfile.mkdtemp(prefix="nas_beh_")
    try:
        _mkfile(os.path.join(d, "photo.jpg"), b"\xff\xd8\xff\xe0" + b"a" * 200)
        _mkfile(os.path.join(d, "note.txt"), b"plain text content here")
        _mkfile(os.path.join(d, "secret.locked"), b"encrypted-blob-xxxx")
        r = behavior.analyze_path(d)
        check("勒索扩展名被正确计数", r["ransom_files"] == 1, str(r))
        check("总文件数正确", r["total_files"] == 3, str(r))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_detect_extension_mutation():
    d = tempfile.mkdtemp(prefix="nas_beh_")
    try:
        # 5 个正常 .txt + 5 个 .locked → 勒索扩展名占比 50%
        for i in range(5):
            _mkfile(os.path.join(d, f"ok{i}.txt"), b"plain text")
        for i in range(5):
            _mkfile(os.path.join(d, f"hit{i}.locked"), b"encrypted")
        res = behavior.detect_behavior([d])
        sigs = {s["type"] for s in res["signals"]}
        check("识别为可疑", res["suspicious"] is True, str(res))
        check("触发 extension_mutation 信号", "extension_mutation" in sigs, str(res))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_detect_mass_rename():
    d = tempfile.mkdtemp(prefix="nas_beh_")
    try:
        # 10 个文件突然统一使用异常扩展名 .qweek（非勒索/非安全高熵/非常见）→ 批量改名
        for i in range(10):
            _mkfile(os.path.join(d, f"file{i}.qweek"), b"some content here")
        res = behavior.detect_behavior([d])
        sigs = {s["type"] for s in res["signals"]}
        check("识别为可疑（批量改名）", res["suspicious"] is True, str(res))
        check("触发 mass_rename 信号", "mass_rename" in sigs, str(res))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_detect_entropy_spike():
    d = tempfile.mkdtemp(prefix="nas_beh_")
    try:
        # 6 个随机内容、使用非安全高熵扩展名 .qweek 的文件 → 熵值骤升
        for i in range(6):
            _mkfile(os.path.join(d, f"rand{i}.qweek"), os.urandom(2000))
        # 再放 4 个正常 .txt 拉低比例
        for i in range(4):
            _mkfile(os.path.join(d, f"plan{i}.txt"), b"plain text document")
        res = behavior.detect_behavior([d])
        check("检测到高熵文件", res["high_entropy_files"] > 0, str(res))
        check("熵值骤升信号存在", any(
            s["type"] == "entropy_spike" for s in res["signals"]), str(res))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_detect_clean_directory():
    d = tempfile.mkdtemp(prefix="nas_beh_")
    try:
        _mkfile(os.path.join(d, "a.txt"), b"hello")
        _mkfile(os.path.join(d, "b.jpg"), b"\xff\xd8\xff\xe0" + b"x" * 200)
        res = behavior.detect_behavior([d])
        check("正常目录不误报", res["suspicious"] is False, str(res))
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    test_entropy_properties()
    test_analyze_nonexistent()
    test_analyze_ransom_ext()
    test_detect_extension_mutation()
    test_detect_mass_rename()
    test_detect_entropy_spike()
    test_detect_clean_directory()
    print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
    sys.exit(1 if FAIL else 0)
