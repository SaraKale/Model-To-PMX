# -*- coding: utf-8 -*-
"""pmx2psk.py - MMD PMX → Unreal ActorX (.psk / .pskx)，纯标准库。

与 convert/psk2pmx.py **互为逆操作**，换轴、手性、UV 口径都对齐那一侧：

换轴
    psk2pmx 用的是 (x, y, z)_psk → (x, z, y)_pmx —— 行列式 −1，是一次反射，
    因为 PSK 右手系要翻成 MMD 左手系（纯旋转永远对不上，别改回 (x, z, −y)）。
    这里就原样再来一次同样的交换：(x, y, z)_pmx → (x, z, y)_psk。

绕序
    反射会把手性翻一次，PMX 自己「绕序与法线同向」，翻完就变成
    「绕序与法线反向」—— 正好就是实测 PSK/PSKX 样本的口径
    （样本实测 agree≈400 / disagree≈3500）。所以默认 **不反转绕序**；
    --flip 只在对接某些要求「同向」的工具时才用。

单位
    PSK 没有单位字段，实测样本（二重螺旋）是 **1 单位 = 1 cm**（菲比 139.9、
    艾米斯 158.4，都是厘米级身高），而 MMD 常规身高 20 单位。
    所以默认按「目标身高 cm」归一（默认 160），要 1:1 就用 --scale keep。

骨骼
    PMX 的 bone.pos 是**模型空间绝对坐标**（与 mmd_tools 读写的口径一致，
    本项目 psk2pmx / vrm2pmx 写出来的也是绝对坐标）；
    PSK 的 REFSKELT 存的是**相对父骨**的局部平移 + 单位四元数，
    所以这里要减掉父骨。PMX 的 IK / 付与(继承) / 刚体 / 关节在 PSK 里
    没有对应结构，一律按普通骨处理（日志会计数）。

能表达 / 不能表达
    能：顶点、法线、UV（含附加 UV）、三角面、材质名、骨骼层级、稀疏权重、
        顶点表情（MRPHINFO / MRPHDATA）、贴图（拷到 textures/ 目录）。
    不能：IK、付与、物理、材质参数（PSK 的 MATT 只有名字 + 6 个 int）、
        顶点色（PMX 没有，统一写白）、UV/材质/骨骼表情。

用法：
    python pmx2psk.py model.pmx -o model.pskx
    python pmx2psk.py model.pmx -o model.psk --std --height 160
"""

import argparse
import os
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(os.path.dirname(_HERE), "formats")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fbxout        # noqa: E402  （复用 PMX 顶点权重解包，BDEF/SDEF/QDEF 口径一致）
import pmxio         # noqa: E402
import pskio         # noqa: E402

PSK_HEIGHT = 160.0           # 默认目标身高（cm）；MMD 常规 20 单位 → ×8
_TEX_EXT = (".png", ".jpg", ".jpeg", ".tga", ".bmp", ".dds")


def _log_default(msg, tag=None):
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("utf-8", "replace").decode("ascii", "replace"))


def _safe_name(s, n=63):
    """PSK 的名字字段是定长 ASCII；非 ASCII 全换成 _，避免写坏块长度。"""
    out = []
    for ch in (s or ""):
        out.append(ch if 32 < ord(ch) < 127 else "_")
    return ("".join(out).strip() or "noname")[:n]


def _ascii(s, n=63):
    """能原样写进 PSK 定长 ASCII 字段就返回它，否则返回 None。

    PSK 的名字字段是 char[64]，日文（UTF-16 / UTF-8）塞进去会被读者截断或
    解成乱码，所以这里只认可打印 ASCII，剩下的交给调用方换名字。
    """
    s = (s or "").strip()
    if not s or len(s) > n:
        return None
    return s if all(32 < ord(c) < 127 for c in s) else None


# MMD 标准日文骨名 → 通用英文名的兜底表。
# 只在「日文名 + 没有可用英文名」时兜底（真 MMD 模型常见）；
# 由本工具 psk2pmx 转出来的模型英文名都存在 name_en 里，先走 name_en。
_JP_TO_EN = {
    "全ての親": "Root", "操作中心": "OperationCenter", "センター": "Center",
    "グルーブ": "Groove", "腰": "Waist", "上半身": "UpperBody",
    "上半身1": "UpperBody1", "上半身2": "UpperBody2", "首": "Neck",
    "頭": "Head", "両目": "Eyes", "右目": "Eye_R", "左目": "Eye_L",
    "右肩": "Shoulder_R", "左肩": "Shoulder_L", "右腕": "Arm_R",
    "左腕": "Arm_L", "右ひじ": "Elbow_R", "左ひじ": "Elbow_L",
    "右手首": "Wrist_R", "左手首": "Wrist_L", "右足": "Leg_R",
    "左足": "Leg_L", "右ひざ": "Knee_R", "左ひざ": "Knee_L",
    "右足首": "Ankle_R", "左足首": "Ankle_L", "右つま先": "Toe_R",
    "左つま先": "Toe_L", "右足ＩＫ": "LegIK_R", "左足ＩＫ": "LegIK_L",
    "右つま先ＩＫ": "ToeIK_R", "左つま先ＩＫ": "ToeIK_L",
    "右足D": "LegD_R", "左足D": "LegD_L", "右ひざD": "KneeD_R",
    "左ひざD": "KneeD_L", "右足首D": "AnkleD_R", "左足首D": "AnkleD_L",
    "右足先EX": "ToeEX_R", "左足先EX": "ToeEX_L",
    "右肩P": "ShoulderP_R", "左肩P": "ShoulderP_L",
    "右肩C": "ShoulderC_R", "左肩C": "ShoulderC_L",
}


def _pmx_name(item, idx, fallback):
    """PMX 条目（骨骼/材质/表情）→ 能写进 PSK 的 ASCII 名。

    优先英文名 → 日文名查兜底表 → 日文名本身若就是 ASCII → 序号名。
    """
    for cand in (_ascii(item.get("name_en")),
                 _JP_TO_EN.get((item.get("name") or "").strip()),
                 _ascii(item.get("name"))):
        if cand:
            return cand
    return fallback % idx


def _topo_order(bones):
    """返回 (顺序, 旧下标→新下标)；保证父骨排在子骨前面（PSK 的隐含要求）。"""
    n = len(bones)
    kids = [[] for _ in range(n)]
    roots = []
    for i, b in enumerate(bones):
        p = b.get("parent", -1)
        if p is None or p < 0 or p >= n or p == i:
            roots.append(i)
        else:
            kids[p].append(i)
    order = []
    seen = [False] * n
    stack = list(reversed(roots))
    while stack:
        i = stack.pop()
        if seen[i]:
            continue
        seen[i] = True
        order.append(i)
        stack.extend(reversed(kids[i]))
    for i in range(n):
        if not seen[i]:
            order.append(i)
    remap = [0] * n
    for new_i, old_i in enumerate(order):
        remap[old_i] = new_i
    return order, remap


# --------------------------------------------------------------------- 主体 --
def convert(pmx_path, out_path, scale_mode="psk", height=PSK_HEIGHT,
            log=None, name=None, flip_winding=False, keep_add_uv=True,
            morphs=True, pskx=None, export_textures=True, face32=None):
    """PMX → .psk / .pskx。返回统计 dict。

    scale_mode : "psk"/"auto" 按身高归一到 height cm；"keep"/"none"/"off"/1
                 保持 PMX 原尺度；其它值按浮点数当缩放系数。
    pskx       : None 自动（按扩展名：.pskx → 写扩展块，.psk → 只写标准块）；
                 True/False 强制。
    flip_winding : 反转三角绕序（默认 False —— 反射已经翻过一次，见模块说明）。
    """
    _l = log or _log_default
    m = pmxio.read_pmx(pmx_path)
    verts = m["vertices"]
    nv = len(verts)
    _l("PMX：%s · 顶点 %d · 三角面 %d · 骨骼 %d · 材质 %d · 表情 %d"
       % (os.path.basename(pmx_path), nv, len(m["faces"]) // 3,
          len(m["bones"]), len(m["materials"]), len(m["morphs"])))

    # ---- 缩放
    ys = [v["pos"][1] for v in verts]
    lo_y, hi_y = (min(ys), max(ys)) if ys else (0.0, 0.0)
    h_pmx = hi_y - lo_y
    if scale_mode in ("psk", "auto", None):
        scale = float(height) / h_pmx if h_pmx > 1e-6 else 1.0
    elif scale_mode in ("keep", "none", "off", 1, 1.0, "raw"):
        scale = 1.0
    else:
        scale = float(scale_mode)
    _l("缩放：PMX 高 %.3f 单位 × %.6f → %.2f（PSK 单位=cm）"
       % (h_pmx, scale, h_pmx * scale))

    # ---- 坐标变换：与 psk2pmx 同一个 (x, z, y) 交换（行列式 −1，一次反射）
    def xf(p):
        return (p[0] * scale, p[2] * scale, p[1] * scale)

    # ---- 骨骼：PSK 存相对父骨的平移
    src_bones = m["bones"]
    nb = len(src_bones)
    need_reorder = any(
        (b.get("parent") or -1) >= i and (b.get("parent") or -1) >= 0
        for i, b in enumerate(src_bones))
    if need_reorder:
        order, remap = _topo_order(src_bones)
        _l("提示：源 PMX 里有骨骼的父骨排在它后面，已按拓扑重排（权重同步映射）",
           "warn")
    else:
        order = list(range(nb))
        remap = list(range(nb))

    psk_bones = []
    n_inherit = 0
    n_ik = 0
    for new_i in order:
        b = src_bones[new_i]
        wp = xf(b["pos"])
        p = b.get("parent", -1)
        if p is None or p < 0 or p >= nb or p == new_i:
            p = -1
        else:
            p = remap[p]
        if p >= 0:
            pp = xf(src_bones[order[p]]["pos"])
            lp = (wp[0] - pp[0], wp[1] - pp[1], wp[2] - pp[2])
        else:
            lp = wp
        psk_bones.append(pskio.PskBone(
            name=_pmx_name(b, new_i, "bone%03d"),
            flags=0, children_count=0, parent_index=p,
            rotation=(0.0, 0.0, 0.0, 1.0), location=lp, length=0.0,
            size=(1.0, 1.0, 1.0)))
        if b.get("inherit_rot") is not None or b.get("inherit_mov") is not None:
            n_inherit += 1
        if b.get("ik"):
            n_ik += 1

    # ---- 顶点 / 楔（PSK 是一点一楔，UV 直接沿用，不翻 V：与 psk2pmx 同口径）
    #      材质下标：PMX 的面按材质连续分段，反查每个顶点属于哪段
    mat_of_face = []
    for mi, mt in enumerate(m["materials"]):
        mat_of_face.extend([mi] * (int(mt.get("faces", 0)) // 3))
    faces_src = m["faces"]
    n_tri = len(faces_src) // 3
    if len(mat_of_face) < n_tri:
        mat_of_face.extend([max(0, len(m["materials"]) - 1)]
                           * (n_tri - len(mat_of_face)))
    mat_of_vert = [0] * nv
    for t in range(n_tri):
        mi = mat_of_face[t] if t < len(mat_of_face) else 0
        for k in range(3):
            vi = faces_src[t * 3 + k]
            if 0 <= vi < nv:
                mat_of_vert[vi] = mi

    points = []
    normals = []
    wedges = []
    for vi, v in enumerate(verts):
        P = xf(v["pos"])
        points.append(P)
        n = v.get("normal") or (0.0, 0.0, 1.0)
        t = xf(n)
        ln = (t[0] ** 2 + t[1] ** 2 + t[2] ** 2) ** 0.5 or 1.0
        normals.append((t[0] / ln, t[1] / ln, t[2] / ln))
        uv = v.get("uv") or (0.0, 0.0)
        wedges.append((vi, uv[0], uv[1], mat_of_vert[vi]))

    # ---- 面
    faces = []
    for t in range(n_tri):
        a, b, c = faces_src[t * 3], faces_src[t * 3 + 1], faces_src[t * 3 + 2]
        if flip_winding:
            a, c = c, a
        faces.append(((a, b, c), mat_of_face[t] if t < len(mat_of_face) else 0))

    # ---- 权重
    weights = []
    n_bad_bone = 0
    for vi, v in enumerate(verts):
        for bi, w in fbxout._weights_of(v):
            if w <= 1e-6:
                continue
            nb2 = remap[bi] if 0 <= bi < nb else -1
            if nb2 < 0:
                n_bad_bone += 1
                continue
            weights.append((w, vi, nb2))
    if n_bad_bone:
        _l("提示：%d 条权重指向不存在的骨骼，已丢弃" % n_bad_bone, "warn")

    # ---- 附加 UV → EXTRAUVS0..（全 0 的通道不写，免得多出几个空通道）
    extra_uvs = []
    if keep_add_uv and m.get("add_uv"):
        for k in range(min(4, int(m["add_uv"]))):
            seq = []
            live = False
            for v in verts:
                au = v.get("add_uv") or []
                t = au[k] if k < len(au) else (0.0, 0.0, 0.0, 0.0)
                if abs(t[0]) + abs(t[1]) > 1e-6:
                    live = True
                seq.append((t[0], t[1]))
            if live:
                extra_uvs.append(seq)

    # ---- 表情：只搬「顶点表情」；MRPH 按点索引，这里点=楔=PMX 顶点，一一对应
    psk_morphs = []
    n_morph_other = 0
    if morphs:
        used = set()
        for mo in m["morphs"]:
            if mo.get("kind") != 1:
                n_morph_other += 1
                continue
            offs = []
            for off in (mo.get("offsets") or []):
                vi, d = off[0], off[1]
                if not (0 <= vi < nv):
                    continue
                dd = xf(d)
                if abs(dd[0]) + abs(dd[1]) + abs(dd[2]) < 1e-9:
                    continue
                offs.append((vi, dd))
            if not offs:
                continue
            nm = _pmx_name(mo, len(psk_morphs), "morph%03d")
            base = nm
            k = 1
            while nm in used:
                nm = "%s_%d" % (base, k)
                k += 1
            used.add(nm)
            psk_morphs.append(pskio.PskMorph(nm, offs))

    # ---- 材质（PSK 的 MATT 只有名字 + 6 个 int，PMX 的材质参数带不过去）
    psk_mats = []
    for mi, mt in enumerate(m["materials"]):
        psk_mats.append(pskio.PskMaterial(
            name=_pmx_name(mt, mi, "material%02d"),
            texture_index=0, poly_flags=0, aux_material=0, aux_flags=0,
            lod_bias=0, lod_style=0))

    # ---- 组装
    psk = pskio.Psk()
    psk.points = points
    psk.wedges = wedges
    psk.faces = faces
    psk.materials = psk_mats
    psk.bones = psk_bones
    psk.weights = weights
    psk.normals = normals
    psk.colors = [(255, 255, 255, 255)] * nv
    psk.extra_uvs = extra_uvs
    psk.morphs = psk_morphs
    psk.face32 = bool(face32) if face32 is not None else (nv > 0xFFFF)

    if pskx is None:
        pskx = os.path.splitext(out_path)[1].lower() == ".pskx"

    size = pskio.write_psk(psk, out_path, pskx=pskx,
                           face32=(None if face32 is None else bool(face32)),
                           log=log or _l)

    # ---- 贴图：PSK 不带贴图，按 psk2pmx 的查找口径（材质名 → textures/）
    n_tex = 0
    out_dir = os.path.dirname(os.path.abspath(out_path))
    if export_textures and m.get("textures"):
        src_dir = os.path.dirname(os.path.abspath(pmx_path))
        tex_dir = os.path.join(out_dir, "textures")
        os.makedirs(tex_dir, exist_ok=True)
        for mi, mt in enumerate(m["materials"]):
            ti = mt.get("tex", -1)
            if not isinstance(ti, int) or not (0 <= ti < len(m["textures"])):
                continue
            rel = m["textures"][ti]
            src = rel if os.path.isabs(rel) else os.path.join(src_dir, rel)
            if not os.path.isfile(src):
                continue
            _n, ext = os.path.splitext(src)
            if ext.lower() not in _TEX_EXT:
                continue
            dst = os.path.join(tex_dir, _safe_name(psk_mats[mi].name) + ext)
            try:
                shutil.copyfile(src, dst)
                n_tex += 1
            except OSError as e:
                _l("提示：贴图 %s 复制失败：%s" % (os.path.basename(src), e),
                   "warn")

    # ---- 回读自校验
    chk = pskio.read_psk(out_path)
    ok = (len(chk.points) == nv and len(chk.wedges) == nv
          and len(chk.faces) == n_tri and len(chk.bones) == nb
          and len(chk.materials) == len(psk_mats)
          and len(chk.weights) == len(weights)
          and ((not pskx) or len(chk.morphs) == len(psk_morphs)))
    _l("已写出 %s%s · %.2f MB · 顶点 %d · 三角面 %d · 骨骼 %d · 材质 %d · "
       "权重 %d · 表情 %d · UV %d · 贴图 %d · 自校验 %s"
       % (os.path.basename(out_path), "（标准 psk，无扩展块）" if not pskx
          else "（pskx，含法线/顶点色/附加UV/表情）", size / 1048576.0,
          len(chk.points), len(chk.faces), len(chk.bones), len(chk.materials),
          len(chk.weights), len(chk.morphs), len(chk.extra_uvs), n_tex,
          "通过" if ok else "不通过"), "ok" if ok else "err")
    if n_inherit:
        _l("提示：%d 根骨带 PMX 付与(继承)，PSK 没有对应结构，已按普通骨处理"
           % n_inherit, "warn")
    if n_ik:
        _l("提示：%d 根骨带 IK，PSK 不含 IK 链，已按普通骨处理" % n_ik, "warn")
    if n_morph_other:
        _l("提示：%d 个非顶点表情（UV/材质/骨骼表情）无法写入 PSK，已跳过"
           % n_morph_other, "warn")
    if not pskx and (psk_morphs or extra_uvs or normals):
        _l("提示：标准 .psk 不写法线 / 附加 UV / 表情，这些内容已丢弃"
           "（要保留请输出 .pskx）", "warn")

    return {"bytes": size, "vertices": nv, "tris": n_tri, "bones": nb,
            "materials": len(psk_mats), "weights": len(weights),
            "morphs": len(psk_morphs), "uvsets": len(extra_uvs),
            "textures": n_tex, "scale": scale, "pskx": bool(pskx),
            "face32": bool(chk.face32), "fidelity": ok}


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="PMX → Unreal ActorX (.psk / .pskx)（纯 Python）")
    ap.add_argument("src")
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--std", action="store_true",
                    help="写标准 .psk（不写 VTXNORMS/VERTEXCOLOR/EXTRAUVS/MRPH）")
    ap.add_argument("--pskx", action="store_true",
                    help="强制写扩展块（默认按 -o 的扩展名判断）")
    ap.add_argument("--scale", default="psk",
                    help="psk(默认，按身高归一) / keep / 具体倍数")
    ap.add_argument("--height", type=float, default=PSK_HEIGHT,
                    help="归一到多少 cm（默认 %.0f）" % PSK_HEIGHT)
    ap.add_argument("--flip", action="store_true",
                    help="反转三角绕序（默认不反转，见模块说明）")
    ap.add_argument("--no-morph", action="store_true", help="不写顶点表情")
    ap.add_argument("--no-add-uv", action="store_true", help="不写附加 UV 通道")
    ap.add_argument("--no-textures", action="store_true", help="不导出贴图")
    a = ap.parse_args(argv)
    out = a.out or (os.path.splitext(a.src)[0] + ".pskx")
    st = convert(a.src, out, scale_mode=a.scale, height=a.height,
                 flip_winding=a.flip, keep_add_uv=not a.no_add_uv,
                 morphs=not a.no_morph, pskx=(True if a.pskx
                                              else (False if a.std else None)),
                 export_textures=not a.no_textures,
                 log=lambda m_, t=None: print(m_))
    return 0 if st.get("fidelity") else 2


if __name__ == "__main__":
    sys.exit(main())
