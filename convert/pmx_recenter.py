# -*- coding: utf-8 -*-
"""pmx_recenter.py - 把**已有的 PMX** 精确对齐到 MMD 世界原点 (0,0,0)。

不需要源文件、不需要重新转换：读 PMX → 平移所有「绝对坐标」→ 写回。
用户手上那份已经导出的、导入 MMD 后离原点老远的模型，直接用这个修。

为什么需要它
------------------------------------------------------------------
转换器内部本来就会居中（x/z 取顶点包围盒中心、y 脚底归零），但有两种情况
仍然会让人觉得「骨骼和物体整体偏离世界中心」：

1. 老版本转出的 PMX / 别处来的 PMX 压根没居中；
2. 网格居中了，**根骨骼没有**。PMX 里骨骼 pos 也是绝对坐标，源模型的
   Root 常常落在 (0, 0, -1.6) 这种位置。MMD 里选中「全ての親」或用骨骼
   当参照时，看到的就是「整个人连骨架一起偏在一边」。

本模块同时解决这两点：整体平移让网格回到原点，再把根骨单独钉到 (0,0,0)。
根骨 pos 是绝对坐标，改它不会动到其它骨骼 / 顶点（MMD 用父子初始位置差
推局部矩阵，单独改根骨后子骨的世界位置不变），所以是安全的；这也正是
标准 MMD 模型「全ての親」该在的位置。想保留根骨原高度用 --keep-root-y。

哪些字段要跟着平移
------------------------------------------------------------------
要改（绝对坐标）：
    顶点 pos、骨骼 pos、刚体 pos、关节 pos
不要改（相对量 / 方向 / 角度）：
    法线、UV、追加UV、顶点/骨骼/UV 表情的位移增量、骨骼 tail 的方向向量、
    fixed_axis / local_axis、IK 角度限制、关节的 move_min/max / 弹簧。
漏改骨骼会让权重全部指错；多改表情会让表情动起来飞出去。

用法
------------------------------------------------------------------
    python pmx_recenter.py model.pmx                  # 同目录写 model_centered.pmx
    python pmx_recenter.py model.pmx --mode origin   # 几何中心严格落在 (0,0,0)
    python pmx_recenter.py model.pmx --mode root     # 以根骨为 X/Z 基准
    python pmx_recenter.py model.pmx --offset 0 2 0  # 手动抬 2 个单位
    python pmx_recenter.py model.pmx --inplace       # 原地修（自动留 .bak）
    python pmx_recenter.py model.pmx --dry-run       # 只报偏移，不写文件

默认模式 ground：x/z 取顶点包围盒中心、y 脚底落地，这是 MMD 的惯例
（人物站在地面网格上，原点在两脚之间）。--mode origin 才是字面意义的
「包围盒中心 = (0,0,0)」，但那样半个身子会沉到地面以下，一般别用。
"""
import argparse
import os
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(os.path.dirname(_HERE), "formats")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pmxio  # noqa: E402  formats/pmxio.py

MODES = ("ground", "origin", "root", "offset")


# ------------------------------------------------------------------ 基准 --
def vertex_bbox(model):
    """顶点包围盒 (lo, hi)；没有顶点直接报错，别拿 (0,0,0) 糊弄过去。"""
    pts = [v["pos"] for v in (model.get("vertices") or [])]
    if not pts:
        raise ValueError("PMX 里没有顶点，无法计算居中基准")
    lo = tuple(min(p[i] for p in pts) for i in range(3))
    hi = tuple(max(p[i] for p in pts) for i in range(3))
    return lo, hi


def root_indices(model, bone_name=None):
    """要钉到原点的骨：指定名字优先，否则所有 parent<0 的根骨。"""
    bones = model.get("bones") or []
    if not bones:
        return []
    if bone_name:
        hit = [i for i, b in enumerate(bones)
               if b.get("name") == bone_name or b.get("name_en") == bone_name]
        if not hit:
            hit = [i for i, b in enumerate(bones)
                   if bone_name in (b.get("name") or "")
                   or bone_name in (b.get("name_en") or "")]
        if not hit:
            raise ValueError("找不到骨骼：%s" % bone_name)
        return hit
    hit = [i for i, b in enumerate(bones) if b.get("parent", -1) < 0]
    return hit or [0]          # 没显式根骨就退到第一根


def compute_offset(model, mode="ground", bone_name=None, custom=None,
                   keep_y=False):
    """算出把模型搬到原点所需的整体平移量 (dx, dy, dz)。"""
    if mode not in MODES:
        raise ValueError("未知的对齐模式 %r，可选：%s" % (mode, ", ".join(MODES)))
    lo, hi = vertex_bbox(model)
    cx = (lo[0] + hi[0]) * 0.5
    cy = (lo[1] + hi[1]) * 0.5
    cz = (lo[2] + hi[2]) * 0.5

    if mode == "offset":
        if custom is None:
            raise ValueError("--mode offset 需要同时给 --offset X Y Z")
        off = tuple(float(x) for x in custom)
    elif mode == "root":
        idx = root_indices(model, bone_name)
        pts = [model["bones"][i]["pos"] for i in idx]
        rx = sum(p[0] for p in pts) / len(pts)
        rz = sum(p[2] for p in pts) / len(pts)
        off = (-rx, -lo[1], -rz)
    elif mode == "origin":
        off = (-cx, -cy, -cz)
    else:                                       # ground，MMD 惯例
        off = (-cx, -lo[1], -cz)

    if keep_y and mode != "offset":
        off = (off[0], 0.0, off[2])
    return off


# ------------------------------------------------------------------ 平移 --
def _add3(p, o):
    return (p[0] + o[0], p[1] + o[1], p[2] + o[2])


def apply_offset(model, off, snap_root=True, bone_name=None,
                 snap_root_y=True):
    """原地平移所有绝对坐标；snap_root 时再把根骨单独钉到 (0, y, 0)。

    返回统计 dict，方便 GUI / CLI 打日志。
    """
    for v in model.get("vertices") or []:
        v["pos"] = _add3(v["pos"], off)
    for b in model.get("bones") or []:
        b["pos"] = _add3(b["pos"], off)
    rbs = model.get("rigid_bodies") or []
    for rb in rbs:
        rb["pos"] = _add3(rb["pos"], off)
    jts = model.get("joints") or []
    for j in jts:
        j["pos"] = _add3(j["pos"], off)

    snapped = []
    if snap_root:
        for i in root_indices(model, bone_name):
            b = model["bones"][i]
            old = b["pos"]
            b["pos"] = (0.0, 0.0 if snap_root_y else old[1], 0.0)
            snapped.append((i, b.get("name", ""), old))
    return {"offset": off, "snapped": snapped,
            "rigid_bodies": len(rbs), "joints": len(jts)}


# ------------------------------------------------------------------ 文件 --
def _unique_backup(src):
    base = src + ".bak"
    if not os.path.exists(base):
        return base
    n = 2
    while os.path.exists("%s.%d" % (base, n)):
        n += 1
    return "%s.%d" % (base, n)


def _emit(log, msg, tag=None):
    if log is None:
        print(msg)
    else:
        try:
            log(msg, tag)
        except TypeError:
            log(msg)


def recenter_file(src, dst=None, mode="ground", bone_name=None, custom=None,
                  keep_y=False, snap_root=True, snap_root_y=True,
                  inplace=False, backup=True, dry_run=False, log=None):
    """修一个 PMX，返回统计 dict。

    dst=None  → 同目录 <stem>_centered.pmx（贴图相对路径保持有效）
    inplace   → 覆盖原文件，默认先留 .bak
    dry_run   → 只算偏移、只报不写
    """
    src = os.path.abspath(src)
    if not os.path.isfile(src):
        raise ValueError("找不到 PMX：%s" % src)
    stem, ext = os.path.splitext(os.path.basename(src))
    if inplace:
        dst = src
    elif dst is None:
        dst = os.path.join(os.path.dirname(src), stem + "_centered.pmx")
    dst = os.path.abspath(dst)
    if not dst.lower().endswith(".pmx"):
        dst += ".pmx"

    model = pmxio.read_pmx(src)
    lo0, hi0 = vertex_bbox(model)
    roots0 = [(i, model["bones"][i].get("name", ""),
               tuple(model["bones"][i]["pos"]))
              for i in root_indices(model, bone_name)]

    off = compute_offset(model, mode=mode, bone_name=bone_name,
                         custom=custom, keep_y=keep_y)
    info = apply_offset(model, off, snap_root=snap_root,
                        bone_name=bone_name, snap_root_y=snap_root_y)
    lo1, hi1 = vertex_bbox(model)

    _emit(log, "原点对齐：整体平移 (%.4f, %.4f, %.4f)"
          % tuple(off), "info")
    _emit(log, "顶点包围盒 前 %s ~ %s → 后 %s ~ %s"
          % (tuple(round(x, 3) for x in lo0), tuple(round(x, 3) for x in hi0),
             tuple(round(x, 3) for x in lo1), tuple(round(x, 3) for x in hi1)),
          "info")
    if roots0:
        _emit(log, "根骨 %s：%s → %s"
              % (roots0[0][1], tuple(round(x, 3) for x in roots0[0][2]),
                 tuple(round(x, 3) for x in
                       model["bones"][roots0[0][0]]["pos"])), "info")
    if info["rigid_bodies"] or info["joints"]:
        _emit(log, "刚体 %d / 关节 %d 的位置已同步平移"
              % (info["rigid_bodies"], info["joints"]), "info")

    info.update({"src": src, "dst": dst, "lo0": lo0, "hi0": hi0,
                 "lo1": lo1, "hi1": hi1, "roots0": roots0,
                 "dry_run": bool(dry_run)})
    if dry_run:
        _emit(log, "（dry-run：未写文件）", "warn")
        return info

    if dst == src:
        if backup:
            bak = _unique_backup(src)
            shutil.copy2(src, bak)
            _emit(log, "原文件已备份：%s" % os.path.basename(bak), "info")
        # 先写临时再替换，写坏了也不至于把原文件毁掉
        tmp = src + ".tmp"
        try:
            pmxio.write_pmx(model, tmp)
            os.replace(tmp, src)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    else:
        if (os.path.dirname(dst) != os.path.dirname(src)
                and not os.path.isdir(os.path.join(os.path.dirname(dst),
                                                   "textures"))):
            _emit(log, "注意：输出到了别的目录，请连同 textures/ 一起复制过去",
                  "warn")
        pmxio.write_pmx(model, dst)
    _emit(log, "已写出：%s" % dst, "ok")
    return info


# ------------------------------------------------------------------- CLI --
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="把已有 PMX 对齐到 MMD 世界原点 (0,0,0)，无需源文件/重转")
    ap.add_argument("pmx", help="要修的 .pmx")
    ap.add_argument("-o", "--output", help="输出路径（默认同目录 *_centered.pmx）")
    ap.add_argument("--mode", choices=MODES, default="ground",
                    help="ground=脚底落地+XZ居中(默认) / origin=几何中心严格(0,0,0)"
                         " / root=以根骨为XZ基准 / offset=手动偏移")
    ap.add_argument("--bone", help="指定基准骨骼名（mode=root 或钉根骨时用）")
    ap.add_argument("--offset", nargs=3, type=float, metavar=("X", "Y", "Z"),
                    help="mode=offset 时的手动平移量")
    ap.add_argument("--keep-y", action="store_true", help="不改动 Y（只归 X/Z）")
    ap.add_argument("--no-snap-root", action="store_true",
                    help="不把根骨单独钉到 (0,0,0)")
    ap.add_argument("--keep-root-y", action="store_true",
                    help="根骨 Y 保留原高度（默认根骨严格落在 (0,0,0)）")
    ap.add_argument("--inplace", action="store_true",
                    help="原地覆盖（自动留 .bak 备份）")
    ap.add_argument("--no-backup", action="store_true",
                    help="配合 --inplace：不留备份")
    ap.add_argument("--dry-run", action="store_true", help="只算偏移，不写文件")
    a = ap.parse_args(argv)
    try:
        recenter_file(a.pmx, dst=a.output, mode=a.mode, bone_name=a.bone,
                      custom=a.offset, keep_y=a.keep_y,
                      snap_root=not a.no_snap_root,
                      snap_root_y=not a.keep_root_y,
                      inplace=a.inplace, backup=not a.no_backup,
                      dry_run=a.dry_run)
    except Exception as e:
        print("失败：%s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
