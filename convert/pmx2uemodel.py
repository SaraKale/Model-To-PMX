# -*- coding: utf-8 -*-
"""pmx2uemodel.py - MMD PMX → UEFormat(.uemodel)，纯标准库。

与 convert/uemodel2pmx.py **互为逆操作**，坐标和绕序都对齐那一侧的实测结论：

坐标
    PMX（左手系、Y-up、脸朝 -Z）→ UE（左手系、Z-up、+X 前/+Y 右）
        (x, y, z)_pmx → (x, -z, y)_ue
    两边手性相同，纯换轴不反射；但 MMD 里 1 单位≈8cm、UE 里 1 单位=1cm，
    所以默认按身高归一（默认目标 180cm，可用 --height 改，或 --scale keep 保持）。

绕序（实测结论，别当成可选）
    真 uemodel（CUE4Parse 导出的 R2T1XinMd10011）里「几何法线 vs 存储法线」
    反向占 3681/3955 → UE 的索引绕序与 PMX 相反。
    PMX 自己是一致的（同向占多数），所以这里**必须反转绕序**，
    否则 Blender / UE 里看是内外翻的。

能表达 / 不能表达
    能：顶点、法线、UV（含附加 UV）、索引、材质面区间、骨骼层级、稀疏权重、
        顶点表情（MORPHTARGETS）。
    不能：PMX 的付与(继承)、IK、物理、UV表情/材质表情/骨表情 —— uemodel 里
        没有对应结构，转换时计数并在日志里说明（不静默丢）。

用法：
    python pmx2uemodel.py model.pmx -o model.uemodel
    python pmx2uemodel.py model.pmx --version 10 --height 180 --scale keep
"""

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(os.path.dirname(_HERE), "formats")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fbxout        # noqa: E402  （复用 PMX 顶点权重解包，BDEF/SDEF/QDEF 口径一致）
import pmxio         # noqa: E402
import uemodelio     # noqa: E402

UE_HEIGHT = 180.0            # UE 常规人形身高（cm）；MMD 常规 20 单位
VERSION_DEFAULT = 9          # 实测样本就是 v9，兼容面最广；要新布局可 --version 10


def _log_default(msg, tag=None):
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("utf-8", "replace").decode("ascii", "replace"))


def _mat3_identity():
    return (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)


# --------------------------------------------------------------------- 主体 --
def convert(pmx_path, out_path, scale_mode="ue", height=UE_HEIGHT, version=None,
            log=None, name=None, flip_winding=True, keep_add_uv=True,
            morphs=True, file_scale=None):
    """PMX → uemodel。返回统计 dict。

    scale_mode : "ue"/"auto" 按身高归一到 height cm；"keep"/None 保持 PMX 原尺度；
                 其它值按浮点数当缩放系数。
    flip_winding : 反转三角绕序（默认开 —— UE 的绕序与 PMX 相反，见模块说明）。
    """
    _l = log or _log_default
    m = pmxio.read_pmx(pmx_path)
    nv = len(m["vertices"])
    _l("PMX：%s · 顶点 %d · 三角面 %d · 骨骼 %d · 材质 %d · 表情 %d"
       % (os.path.basename(pmx_path), nv, len(m["faces"]) // 3,
          len(m["bones"]), len(m["materials"]), len(m["morphs"])))

    # ---- 缩放
    ys = [v["pos"][1] for v in m["vertices"]]
    lo_y, hi_y = (min(ys), max(ys)) if ys else (0.0, 0.0)
    h_pmx = hi_y - lo_y
    if file_scale:
        scale = float(file_scale)
    elif scale_mode in ("ue", "auto", None):
        scale = float(height) / h_pmx if h_pmx > 1e-6 else 1.0
    elif scale_mode in ("keep", "none", "off"):
        scale = 1.0
    else:
        scale = float(scale_mode)
    _l("缩放：PMX 高 %.3f 单位 × %.6f → %.2f cm" % (h_pmx, scale, h_pmx * scale))

    # ---- 坐标变换 + 缩放
    def xfu(p):
        return (p[0] * scale, -p[2] * scale, p[1] * scale)

    # ---- 骨骼：UE 的 bone.pos 是**相对父骨**的局部平移（旋转恒等）
    src_bones = m["bones"]
    nb = len(src_bones)
    ue_bones = []
    n_inherit = 0
    n_ik = 0
    for i, b in enumerate(src_bones):
        p = b.get("parent", -1)
        if p is None or p < 0 or p >= nb or p == i:
            p = -1
        wp = xfu(b["pos"])
        if p >= 0:
            pp = xfu(src_bones[p]["pos"])
            lp = (wp[0] - pp[0], wp[1] - pp[1], wp[2] - pp[2])
        else:
            lp = wp
        ue_bones.append(uemodelio.Bone(name=b.get("name") or ("bone%d" % i),
                                       parent=p, pos=lp,
                                       rot=(0.0, 0.0, 0.0, 1.0),
                                       scale=(1.0, 1.0, 1.0)))
        if b.get("inherit_rot") is not None or b.get("inherit_mov") is not None:
            n_inherit += 1
        if b.get("ik"):
            n_ik += 1

    sk = uemodelio.Skeleton()
    sk.path = ""
    sk.bones = ue_bones

    # ---- LOD0
    lod = uemodelio.LOD("LOD0")
    lod.vertices = [xfu(v["pos"]) for v in m["vertices"]]
    lod.normals = []
    lod.binormals = []
    for i, v in enumerate(m["vertices"]):
        n = v.get("normal") or (0.0, 0.0, 1.0)
        t = xfu(n)
        ln = (t[0] ** 2 + t[1] ** 2 + t[2] ** 2) ** 0.5 or 1.0
        lod.normals.append((t[0] / ln, t[1] / ln, t[2] / ln))
        lod.binormals.append(1.0)

    # UV：PMX 和 UE 的 UV 原点都在左上，不需要翻 V
    uv0 = [v.get("uv") or (0.0, 0.0) for v in m["vertices"]]
    lod.uvs.append(uemodelio.UVSet("UV0", uv0))
    if keep_add_uv and m.get("add_uv"):
        # 附加 UV 全为 0 就别写，免得下游多出几个空通道
        for k in range(m["add_uv"]):
            extra = []
            live = False
            for v in m["vertices"]:
                au = v.get("add_uv") or []
                t = au[k] if k < len(au) else (0.0, 0.0, 0.0, 0.0)
                if abs(t[0]) + abs(t[1]) > 1e-6:
                    live = True
                extra.append((t[0], t[1]))
            if live:
                lod.uvs.append(uemodelio.UVSet("UV%d" % (k + 1), extra))

    # ---- 面：索引绕序反过来（PMX 与 UE 相反）
    idx = []
    faces = m["faces"]
    for t in range(0, len(faces) - 2, 3):
        a, b, c = faces[t], faces[t + 1], faces[t + 2]
        if flip_winding:
            a, c = c, a
        idx.extend((a, b, c))
    lod.indices = idx

    # ---- 材质（PMX 的 faces 是**索引数**；uemodel 的 num_faces 是三角面数）
    #      first_index 用索引缓冲里的位置，即面起点 ×3（与实测样本一致：
    #      0 / 24111 / 51186 …，24111 = 8037×3）
    first = 0
    for mt in m["materials"]:
        nf = int(mt.get("faces", 0)) // 3
        if nf <= 0:
            continue
        lod.materials.append(uemodelio.Material(
            name=(mt.get("name") or "material").strip(),
            path="", first_index=first, num_faces=nf))
        first += nf * 3
    cover = first // 3
    total = len(lod.indices) // 3
    if cover != total:
        # 面区间不连续/少了，把差额并进最后一个材质，别写出「面数对不上」的文件
        if lod.materials:
            lod.materials[-1].num_faces += total - cover
        _l("提示：材质面区间合计 %d 面，索引缓冲有 %d 面，已把差额并入最后一个材质"
           % (cover, total), "warn")

    # ---- 权重：PMX 顶点 → 稀疏 (骨, 顶点, 权重) 三元组
    nw = 0
    for vi, v in enumerate(m["vertices"]):
        for bi, w in fbxout._weights_of(v):
            if w <= 1e-6:
                continue
            lod.weights.append(uemodelio.Weight(bone=bi, vertex=vi, weight=w))
            nw += 1

    # ---- 表情：只搬「顶点表情」（uemodel 的 MORPHTARGETS 只有位置/法线增量）
    n_morph_other = 0
    if morphs:
        for mo in m["morphs"]:
            if mo.get("kind") != 1:
                n_morph_other += 1
                continue
            deltas = []
            for off in (mo.get("offsets") or []):
                vi, d = off[0], off[1]
                if not (0 <= vi < nv):
                    continue
                if abs(d[0]) + abs(d[1]) + abs(d[2]) < 1e-9:
                    continue
                deltas.append(uemodelio.MorphDelta(
                    pos=xfu(d), normal=(0.0, 0.0, 0.0), vertex=vi))
            if deltas:
                lod.morphs.append(uemodelio.Morph(name=mo.get("name") or "morph",
                                                  deltas=deltas))

    # ---- 组装
    mo = uemodelio.UEModel()
    mo.identifier = uemodelio.IDENT_MODEL
    mo.version = int(version or VERSION_DEFAULT)
    stem = os.path.splitext(os.path.basename(pmx_path))[0]
    mo.name = (name or m.get("name") or stem).strip() or stem
    mo.path = ""
    mo.compressed = False
    mo.lods = [lod]
    mo.skeleton = sk
    mo.collisions = []

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    size = uemodelio.write_uemodel(mo, out_path, version=mo.version)

    # ---- 写回自校验（读回来比数量；read 内部要求末尾字节数相符）
    chk = uemodelio.read_uemodel(out_path)
    cl = chk.lods[0]
    ok = (len(cl.vertices) == len(lod.vertices)
          and len(cl.indices) == len(lod.indices)
          and len(chk.skeleton.bones) == len(ue_bones)
          and len(cl.weights) == len(lod.weights)
          and len(cl.materials) == len(lod.materials)
          and len(cl.morphs) == len(lod.morphs))
    _l("已写出 %s（v%d · %.2f MB）· 顶点 %d · 三角面 %d · 骨骼 %d · 材质 %d · "
       "权重 %d · 表情 %d · UV %d · 自校验 %s"
       % (os.path.basename(out_path), chk.version, size / 1048576.0,
          len(cl.vertices), len(cl.indices) // 3, len(chk.skeleton.bones),
          len(cl.materials), len(cl.weights), len(cl.morphs), len(cl.uvs),
          "通过" if ok else "不通过"), "ok" if ok else "err")
    if n_inherit:
        _l("提示：%d 根骨带 PMX 付与(继承)，uemodel 没有对应结构，已按普通骨处理"
           % n_inherit, "warn")
    if n_ik:
        _l("提示：%d 根骨带 IK，uemodel 不含 IK 链，已按普通骨处理" % n_ik, "warn")
    if n_morph_other:
        _l("提示：%d 个非顶点表情（UV/材质/骨骼表情）无法写入 uemodel，已跳过"
           % n_morph_other, "warn")

    return {"bytes": size, "version": chk.version, "vertices": len(cl.vertices),
            "tris": len(cl.indices) // 3, "bones": len(chk.skeleton.bones),
            "materials": len(cl.materials), "weights": len(cl.weights),
            "morphs": len(cl.morphs), "uvsets": len(cl.uvs),
            "scale": scale, "fidelity": ok}


def main(argv=None):
    ap = argparse.ArgumentParser(description="PMX → UEFormat(.uemodel)（纯 Python）")
    ap.add_argument("src")
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--scale", default="ue",
                    help="ue(默认，按身高归一) / keep / 具体倍数")
    ap.add_argument("--height", type=float, default=UE_HEIGHT,
                    help="归一到多少 cm（默认 %.0f）" % UE_HEIGHT)
    ap.add_argument("--version", type=int, default=VERSION_DEFAULT,
                    help="UEFormat 版本，默认 %d（实测样本的版本）" % VERSION_DEFAULT)
    ap.add_argument("--no-flip", action="store_true",
                    help="不反转绕序（默认反转；UE 的绕序与 PMX 相反）")
    ap.add_argument("--no-morph", action="store_true", help="不写顶点表情")
    ap.add_argument("--no-add-uv", action="store_true", help="不写附加 UV 通道")
    a = ap.parse_args(argv)
    out = a.out or (os.path.splitext(a.src)[0] + ".uemodel")
    st = convert(a.src, out, scale_mode=a.scale, height=a.height,
                 version=a.version, flip_winding=not a.no_flip,
                 keep_add_uv=not a.no_add_uv, morphs=not a.no_morph)
    return 0 if st.get("fidelity") else 2


if __name__ == "__main__":
    sys.exit(main())
