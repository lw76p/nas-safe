#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 NAS Safe 桌面助手托盘/EXE 图标（多尺寸 ICO）。

正常图标：蓝色圆角盾牌 + 白色对勾；告警图标：蓝色圆角盾牌 + 红色感叹号。
盾牌比例与网页 LOGO 一致，顶部圆角、两侧饱满、底部尖。
"""
import io
import os
import struct
import sys
from PIL import Image, ImageDraw

BLUE = (37, 99, 235)
BLUE_DARK = (29, 78, 216)
BLUE_LIGHT = (59, 130, 246)
RED = (239, 68, 68)


def _draw_shield(draw, size):
    """画一个与网页 LOGO 一致的盾牌：顶部平、两侧微鼓、肩部明显、底部尖。"""
    s = size
    cx = s / 2.0
    pad = s * 0.08
    top = pad
    shoulder = s * 0.45
    tip_y = s - pad * 0.5
    top_w = s * 0.74  # 顶部宽度
    shoulder_w = s * 0.86  # 肩部宽度

    # 盾牌主体多边形：左上、右上、右肩、底尖、左肩
    poly = [
        (cx - top_w / 2, top),
        (cx + top_w / 2, top),
        (cx + shoulder_w / 2, shoulder),
        (cx, tip_y),
        (cx - shoulder_w / 2, shoulder),
    ]

    # 阴影
    shadow = [(x + 1, y + 1) for x, y in poly]
    draw.polygon(shadow, fill=(0, 0, 0, 60))

    # 主体
    draw.polygon(poly, fill=BLUE + (255,))

    # 描边
    draw.polygon(poly, outline=BLUE_DARK + (200,), width=max(1, size // 26))

    # 内部高光：小一号的同款盾牌，居上，营造立体感
    if size >= 24:
        inset_factor = 0.82
        hi = [(cx + (x - cx) * inset_factor, y * inset_factor + s * 0.02) for x, y in poly]
        draw.polygon(hi, fill=BLUE_LIGHT + (110,))


def _draw_exclamation(draw, size):
    """在盾牌正中央画加粗红色感叹号。"""
    s = size
    cx = s / 2.0
    bar_w = max(2.5, s * 0.16)
    bar_top = s * 0.28
    bar_bottom = s * 0.60
    draw.rounded_rectangle(
        [cx - bar_w / 2, bar_top, cx + bar_w / 2, bar_bottom],
        radius=max(1, s * 0.04),
        fill=RED,
    )
    dot_r = max(1.8, s * 0.08)
    dot_y = s * 0.74
    draw.ellipse(
        [cx - dot_r, dot_y - dot_r, cx + dot_r, dot_y + dot_r],
        fill=RED,
    )


def _draw_check(draw, size):
    """正常状态在盾牌正中画白色对勾（与网页 LOGO 一致）。"""
    s = size
    w = max(2.0, s * 0.12)
    draw.line(
        [(s * 0.32, s * 0.54), (s * 0.46, s * 0.68)],
        fill=(255, 255, 255, 255), width=int(w),
    )
    draw.line(
        [(s * 0.46, s * 0.68), (s * 0.70, s * 0.38)],
        fill=(255, 255, 255, 255), width=int(w),
    )


def _render_frame(size, alert=False):
    """渲染单尺寸RGBA图像。"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    _draw_shield(draw, size)
    if alert:
        _draw_exclamation(draw, size)
    else:
        _draw_check(draw, size)
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
