#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拓扑：彻底去掉入场缩放导致的「节点跳到位」，并给节点分配长短错落的半径。"""
import io
import os
import sys

ROOT = r"C:\Users\aa\WorkBuddy\2026-09-29-16-08-29\nas-safe-clone"
CSS = os.path.join(ROOT, "web", "style.css")
JS = os.path.join(ROOT, "web", "app.js")


def patch(path, pairs):
    with io.open(path, "r", encoding="utf-8", newline="") as f:
        s = f.read()
    crlf = s.count("\r\n") * 2 > s.count("\n")
    s = s.replace("\r\n", "\n")
    for old, new in pairs:
        if s.count(old) != 1:
            raise AssertionError((path, s.count(old), old[:80]))
        s = s.replace(old, new)
    if crlf:
        s = s.replace("\n", "\r\n")
    with io.open(path, "w", encoding="utf-8", newline="") as f:
        f.write(s)
    print("patched", os.path.basename(path))


patch(CSS, [
    # 入场只做淡入，绝不改 transform/scale：之前 scale 从 72% 长到 100%，
    # 而连线端点按最终图标半径计算，导致前几帧线比图标长，看起来像节点跳上来。
    (
        """/* 入场只做「原地淡入 + 放大」，绝不位移：节点一开始就落在连接线端点上，
   刷新时不会出现「从离线位置跳到连接位」的现象 */
@keyframes topoNodeIn {
  from { opacity: 0; transform: translate(-50%, -50%) scale(calc(var(--s-idle, .38) * var(--zs, 1) * .72)); }
}""",
        """/* 入场只做「原地淡入」：节点一开始就落在最终大小与连接线端点上，
   刷新时不会出现「从离线位置跳到连接位」的现象 */
@keyframes topoNodeIn {
  from { opacity: 0; }
  to   { opacity: 1; }
}""",
    ),
])

patch(JS, [
    # 1) 在 topoDeviceIcon 附近插入 radiusBias 辅助函数（放在 renderTopology 前面）
    (
        """function renderTopology(data) {""",
        """// 让节点连线有长有短、错落有致：按索引分配到内/中/外三层，加小抖动但保持可预测
function radiusBias(i, n) {
  if (n <= 4) return 1;
  const tiers = [1.10, 0.86, 1.00];
  const jitter = (((i * 13) % 7) - 3) * 0.018;   // ±0.054，确定性抖动
  const v = tiers[i % 3] + jitter;
  return Math.max(0.78, Math.min(1.14, v));
}

function renderTopology(data) {""",
    ),
    # 2) base 位置乘上 rBias
    (
        """  // 基准位置归一化到 0..1，便于漂移时同步更新连线端点
  const base = devs.map((d, i) => {
    const ang = (-90 + i * (360 / n)) * Math.PI / 180;
    return { x: (cx + rx * Math.cos(ang)) / W, y: (cy + ry * Math.sin(ang)) / H };
  });""",
        """  // 基准位置归一化到 0..1，便于漂移时同步更新连线端点
  // 设备多时半径长短错落，避免所有连线等长、节点挤在同一圆环上
  const base = devs.map((d, i) => {
    const rb = radiusBias(i, n);
    const ang = (-90 + i * (360 / n)) * Math.PI / 180;
    return { x: (cx + rx * rb * Math.cos(ang)) / W, y: (cy + ry * rb * Math.sin(ang)) / H, rb };
  });""",
    ),
    # 3) items 里保存 rBase，漂移时使用
    (
        """      mode: "polar", a0: Math.atan2(base[i].y * H - cy, base[i].x * W - cx),
      aOff: 0, rOff: 0, zr: 0,""",
        """      mode: "polar", a0: Math.atan2(base[i].y * H - cy, base[i].x * W - cx),
      rBase: base[i].rb,
      aOff: 0, rOff: 0, zr: 0,""",
    ),
    (
        """        const ang = it.a0 + it.aOff, rr = Math.min(1 + it.rOff + it.zr, RMAX);""",
        """        const ang = it.a0 + it.aOff, rr = Math.min((it.rBase || 1) + it.rOff + it.zr, RMAX);""",
    ),
])

print("OK")
