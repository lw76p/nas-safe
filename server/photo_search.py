"""三期：照片语义搜索（专业版专属，脚手架）。

目标：对照片库做语义索引，支持「找出去年海边日落」「有猫和咖啡杯」这类自然语言检索。
状态：脚手架。索引与检索依赖「图像 embedding 模型」，需等二期 embedding 能力在图像域落地后接入。

已就绪：
  - 库扫描（枚举图片文件 + 基础元数据）
  - 接口骨架（index_gallery / semantic_search），明确返回「尚未启用」而非崩溃
待接入（TODO）：
  - 图像 embedding：经 ai.embed 的图像变体，或专用多模态模型（如 CLIP / qwen-vl 的向量输出）
  - 跨设备统一索引（配合 devices 模块）
  - 与 RAG 知识库共用向量库与余弦检索
"""
from __future__ import annotations

import os

STATUS = "scaffold"
SUPPORTED_EXT = (".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif", ".bmp")


def scan_library(root: str) -> dict:
    """扫描目录下的图片文件（不递归超过一定深度，避免卡死）。"""
    if not root or not os.path.isdir(root):
        return {"ok": False, "error": "目录不存在：%s" % root, "files": []}
    files = []
    for dirpath, _dirs, names in os.walk(root):
        for n in names:
            if n.lower().endswith(SUPPORTED_EXT):
                files.append(os.path.join(dirpath, n))
        # 浅扫描：最多下探 3 层
        depth = dirpath[len(root):].count(os.sep)
        if depth >= 3:
            _dirs[:] = []
    return {"ok": True, "root": root, "count": len(files), "files": files[:200]}


def index_gallery(root: str) -> dict:
    """TODO：接入图像 embedding 后实现。当前返回未启用状态。"""
    scan = scan_library(root)
    return {
        "ok": False,
        "status": STATUS,
        "message": "照片语义搜索尚未启用：需先接入图像 embedding 模型（二期 embedding 能力在图像域落地后开放）。",
        "scanned": scan.get("count", 0),
    }


def semantic_search(query: str, top_k: int = 10) -> dict:
    """TODO：接入图像 embedding 后实现。当前返回未启用状态。"""
    return {
        "ok": False,
        "status": STATUS,
        "message": "照片语义搜索尚未启用：用自然语言搜照片的功能正在开发中，敬请期待专业版更新。",
    }
