#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 NAS Safe 桌面助手托盘/EXE 图标（多尺寸 ICO）。

正常图标：蓝色盾牌；告警图标：蓝色盾牌正中红色感叹号。
生成 16/24/32/48/256 多帧，确保托盘 16×16 也不糊成圆。
"""
import io
import os
import struct
import sys
from PIL import Image, ImageDraw

BLUE = (37, 99, 235)
RED = (239, 68, 68)
SHIELD_FILL = (37, 99, 235)
SHIELD_STROKE = (29, 78, 216)


def _shield_polygon(size):
    """生成盾牌轮廓点（上宽下窄，底部尖），以左上角为 (0,0)。"""
    s = size
    # 顶部略低于最上沿，两侧留出边距
    margin = s * 0.08
    top = margin
    left = margin
    right = s - margin
    # 肩高
    shoulder = s * 0.32
    # 底部尖点
    tip_x = s / 2.0
    tip_y = s - margin * 0.6
    return [
        (left, top),
        (right, top),
        (right, shoulder),
        (tip_x, tip_y),
        (left, shoulder),
    ]


def _draw_exclamation(draw, size):
    """在盾牌正中央画加粗红色感叹号。"""
    s = size
    cx = s / 2.0
    cy = s / 2.0
    bar_w = max(2.5, s * 0.17)
    # 竖条：占据中上部分
    bar_top = s * 0.27
    bar_bottom = s * 0.62
    draw.rounded_rectangle(
        [cx - bar_w / 2, bar_top, cx + bar_w / 2, bar_bottom],
        radius=max(1, s * 0.04),
        fill=RED,
    )
    # 圆点
    dot_r = max(1.8, s * 0.085)
    dot_y = s * 0.77
    draw.ellipse(
        [cx - dot_r, dot_y - dot_r, cx + dot_r, dot_y + dot_r],
        fill=RED,
    )


def _render_frame(size, alert=False):
    """渲染单尺寸RGBA图像。"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    poly = _shield_polygon(size)
    # 画阴影/描边：让盾牌在浅色/深色任务栏都有边界感
    if size >= 16:
        shadow_poly = [(x + 1, y + 1) for x, y in poly]
        draw.polygon(shadow_poly, fill=(0, 0, 0, 80))
    draw.polygon(poly, fill=SHIELD_FILL + (255,))
    # 内高光线，增强立体感
    if size >= 24:
        inset = [(x * 0.88 + size * 0.06, y * 0.88 + size * 0.06) for x, y in poly]
        draw.polygon(inset, fill=(59, 130, 246, 160))
    draw.polygon(poly, outline=SHIELD_STROKE + (160,), width=max(1, size // 24))

    if alert:
        _draw_exclamation(draw, size)
    return img


def _write_ico(path, imgs):
    """手动写多尺寸 ICO：每帧为 PNG 压缩，支持 256×256。"""
    pngs = []
    for im in imgs:
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        pngs.append(buf.getvalue())
    header = struct.pack("<HHH", 0, 1, len(imgs))
    dirs = b""
    data = b""
    offset = 6 + 16 * len(imgs)
    for im, png in zip(imgs, pngs):
        w = im.width if im.width < 256 else 0
        h = im.height if im.height < 256 else 0
        dirs += struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, len(png), offset)
        data += png
        offset += len(png)
    with open(path, "wb") as f:
        f.write(header + dirs + data)


def build_ico(out_path, alert=False, sizes=(16, 24, 32, 48, 256)):
    frames = [_render_frame(s, alert) for s in sizes]
    # 最小尺寸放在最前面，Windows 会按 DPI 自动选择最合适的帧
    _write_ico(out_path, frames)


def main():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    agent_dir = os.path.join(repo, "agent")
    os.makedirs(agent_dir, exist_ok=True)
    build_ico(os.path.join(agent_dir, "nassafe_agent.ico"), alert=False)
    build_ico(os.path.join(agent_dir, "nassafe_agent_alert.ico"), alert=True)
    print("图标已生成：")
    for name in ("nassafe_agent.ico", "nassafe_agent_alert.ico"):
        p = os.path.join(agent_dir, name)
        print(f"  {p}  ({os.path.getsize(p)} bytes)")


if __name__ == "__main__":
    main()
