# -*- coding: utf-8 -*-
"""pmx2vrm.py - 把 PMX 模型转成 VRM（glTF 2.0 / GLB 容器），纯 Python 标准库。

不需要 Blender、不需要 mmd_tools、不需要 Unity。

坐标约定
--------
PMX（MMD）   正面 -Z，角色右手侧 +X，Y 向上，正面为顺时针缠绕
VRM 0.x      正面 -Z，角色右手侧 +X，Y 向上（glTF 右手系、逆时针为正面）
VRM 1.0      正面 +Z，角色左手侧 +X，Y 向上

所以：
  * 转 0.x 坐标原样搬运；转 1.0 绕 Y 轴旋转 180°。
  * 两种情况都要反转三角形缠绕顺序（PMX 顺时针 → glTF 逆时针）。
  * 尺寸：MMD 模型约 20 单位高，VRM 用米，默认按 1.6 m 归一。

用法：
    python pmx2vrm.py model.pmx -o model.vrm
    python pmx2vrm.py model.pmx --spec 0x --title "名字" --author "作者"
"""
import argparse
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pmxio
import vrmio

# --------------------------------------------------------- 骨骼名映射表 ----
MMD_TO_VRM = {
    "下半身": "hips",
    "上半身": "spine",
    "上半身2": "chest",
    "上半身3": "upperChest",
    "首": "neck",
    "頭": "head",
    "左目": "leftEye", "右目": "rightEye", "両目": "leftEye",
    "左肩": "leftShoulder", "右肩": "rightShoulder",
    "左腕": "leftUpperArm", "右腕": "rightUpperArm",
    "左ひじ": "leftLowerArm", "右ひじ": "rightLowerArm",
    "左肘": "leftLowerArm", "右肘": "rightLowerArm",
    "左手首": "leftHand", "右手首": "rightHand",
    "左足": "leftUpperLeg", "右足": "rightUpperLeg",
    "左ひざ": "leftLowerLeg", "右ひざ": "rightLowerLeg",
    "左足首": "leftFoot", "右足首": "rightFoot",
    "左つま先": "leftToes", "右つま先": "rightToes",
}
for _side, _L, _R in (("親指", "Thumb", "Thumb"),
                      ("人指", "Index", "Index"),
                      ("中指", "Middle", "Middle"),
                      ("薬指", "Ring", "Ring"),
                      ("小指", "Little", "Little")):
    for _n, _suf in (("０", "Metacarpal"), ("0", "Metacarpal"),
                     ("１", "Proximal"), ("1", "Proximal"),
                     ("２", "Middle2"), ("2", "Middle2"),
                     ("３", "Distal"), ("3", "Distal")):
        _slot = {"Metacarpal": "Metacarpal", "Proximal": "Proximal",
                 "Middle2": "Intermediate", "Distal": "Distal"}[_suf]
        if _L == "Thumb":
            _slot = {"Metacarpal": "Metacarpal", "Proximal": "Proximal",
                     "Middle2": "Distal", "Distal": "Distal"}[_suf]
        MMD_TO_VRM["左" + _side + _n] = "left" + _L + _slot
        MMD_TO_VRM["右" + _side + _n] = "right" + _L + _slot

# 英文名兜底
ALT_TO_VRM = {
    "hips": "hips", "spine": "spine", "chest": "chest",
    "upperchest": "upperChest", "neck": "neck", "head": "head",
    "leye": "leftEye", "reye": "rightEye",
    "lowerbody": "hips", "upperbody": "spine", "upperbody2": "chest",
    "lshoulder": "leftShoulder", "rshoulder": "rightShoulder",
    "larm": "leftUpperArm", "rarm": "rightUpperArm",
    "lelbow": "leftLowerArm", "relbow": "rightLowerArm",
    "lwrist": "leftHand", "rwrist": "rightHand",
    "lleg": "leftUpperLeg", "rleg": "rightUpperLeg",
    "lknee": "leftLowerLeg", "rknee": "rightLowerLeg",
    "lankle": "leftFoot", "rankle": "rightFoot",
    "ltoe": "leftToes", "rtoe": "rightToes",
}

VRM_PARENT = {
    "spine": "hips", "chest": "spine", "upperChest": "chest",
    "neck": "upperChest", "head": "neck",
    "leftEye": "head", "rightEye": "head", "jaw": "head",
    "leftShoulder": "upperChest", "leftUpperArm": "leftShoulder",
    "leftLowerArm": "leftUpperArm", "leftHand": "leftLowerArm",
    "rightShoulder": "upperChest", "rightUpperArm": "rightShoulder",
    "rightLowerArm": "rightUpperArm", "rightHand": "rightLowerArm",
    "leftUpperLeg": "hips", "leftLowerLeg": "leftUpperLeg",
    "leftFoot": "leftLowerLeg", "leftToes": "leftFoot",
    "rightUpperLeg": "hips", "rightLowerLeg": "rightUpperLeg",
    "rightFoot": "rightLowerLeg", "rightToes": "rightFoot",
}

REQUIRED_1_0 = ["hips", "spine", "head",
                "leftUpperArm", "leftLowerArm", "leftHand",
                "rightUpperArm", "rightLowerArm", "rightHand",
                "leftUpperLeg", "leftLowerLeg", "leftFoot",
                "rightUpperLeg", "rightLowerLeg", "rightFoot"]
REQUIRED_0X = REQUIRED_1_0 + ["chest", "neck"]

# 自动补齐时的相对偏移（左、上、前）
_SYNTH = {
    "spine": (0.0, 0.12, 0.0), "chest": (0.0, 0.12, 0.0),
    "upperChest": (0.0, 0.10, 0.0), "neck": (0.0, 0.10, 0.0),
    "head": (0.0, 0.12, 0.0),
    "leftShoulder": (0.05, 0.10, 0.0), "rightShoulder": (-0.05, 0.10, 0.0),
    "leftUpperArm": (0.12, 0.0, 0.0), "rightUpperArm": (-0.12, 0.0, 0.0),
    "leftLowerArm": (0.25, 0.0, 0.0), "rightLowerArm": (-0.25, 0.0, 0.0),
    "leftHand": (0.24, 0.0, 0.0), "rightHand": (-0.24, 0.0, 0.0),
    "leftUpperLeg": (0.09, -0.08, 0.0), "rightUpperLeg": (-0.09, -0.08, 0.0),
    "leftLowerLeg": (0.0, -0.40, 0.0), "rightLowerLeg": (0.0, -0.40, 0.0),
    "leftFoot": (0.0, -0.40, 0.06), "rightFoot": (0.0, -0.40, 0.06),
}
_HIPS_Y = 0.90

EXPR_1_0 = {
    "まばたき": "blink", "瞬き": "blink", "ウィンク": "blinkRight",
    "ウィンク２": "blinkLeft", "ウィンク右": "blinkRight",
    "ウィンク左": "blinkLeft", "ｳｨﾝｸ": "blinkRight", "ｳｨﾝｸ２": "blinkLeft",
    "びっくり": "surprised", "怒り": "angry", "怒る": "angry",
    "悲しみ": "sad", "困る": "sad", "真面目": "relaxed",
    "にこり": "happy", "笑い": "happy", "にやり": "happy",
    "あ": "aa", "あ２": "aa", "い": "ih", "う": "ou", "え": "ee", "お": "oh",
    "上": "lookUp", "下": "lookDown", "左": "lookLeft", "右": "lookRight",
}
EXPR_0X = {
    "まばたき": "Blink", "瞬き": "Blink", "ウィンク": "Blink_R",
    "ウィンク２": "Blink_L", "ウィンク右": "Blink_R", "ウィンク左": "Blink_L",
    "怒り": "Angry", "悲しみ": "Sorrow", "困る": "Sorrow",
    "にこり": "Joy", "笑い": "Joy",
    "あ": "A", "あ２": "A", "い": "I", "う": "U", "え": "E", "お": "O",
    "上": "LookUp", "下": "LookDown", "左": "LookLeft", "右": "LookRight",
}


# ------------------------------------------------------------------ 工具 --
def _norm(s):
    return (s or "").strip().replace(" ", "").replace("_", "").lower()


def _topo_bones(bones):
    """把骨骼重排成「父在前」，返回 (新序号→原序号, 原序号→新序号)。

    用「祖先链深度」排序：父节点的深度永远比子节点小 1，排序后父必在前。
    比递归/DFS 稳，遇到坏数据（成环）也不会死循环。
    """
    n = len(bones)
    depth = [0] * n
    for i in range(n):
        seen = set()
        d = 0
        k = i
        while d <= n + 5:
            p = bones[k]["parent"]
            if not (0 <= p < n) or p == k or p in seen:
                break
            seen.add(p)
            d += 1
            k = p
        depth[i] = d
    order = sorted(range(n), key=lambda i: (depth[i], i))
    remap = {old: new for new, old in enumerate(order)}
    return order, remap


def _flatten_morphs(morphs):
    """把顶点表情（含グループ展开）整理成 [(名字, {顶点: (dx,dy,dz)})]。"""
    cache = {}

    def resolve(i, seen):
        if i in cache:
            return cache[i]
        if i in seen:
            return {}
        seen = seen or set()
        seen.add(i)
        mo = morphs[i]
        if mo["kind"] == 1:
            d = {}
            for vi, off in mo["offsets"]:
                if abs(off[0]) + abs(off[1]) + abs(off[2]) > 1e-9:
                    d[vi] = (off[0], off[1], off[2])
            return d
        if mo["kind"] == 0:
            out = {}
            for sub, rate in mo["offsets"]:
                if 0 <= sub < len(morphs):
                    for vi, d in resolve(sub, set(seen)).items():
                        ox, oy, oz = out.get(vi, (0.0, 0.0, 0.0))
                        out[vi] = (ox + d[0] * rate, oy + d[1] * rate,
                                   oz + d[2] * rate)
            return out
        return {}

    out = []
    for i, mo in enumerate(morphs):
        if mo["kind"] not in (0, 1):
            continue
        d = resolve(i, set())
        if d:
            out.append((mo["name"], d))
    return out


def _degmap(xr, yr):
    return {"curve": [0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0, 0.0],
            "xRange": xr, "yRange": yr}


# ---------------------------------------------------------------- 主流程 --
def convert(pmx_path, vrm_path, spec="1.0", scale_mode="auto",
            rotate="auto",             reverse_winding="auto", meta=None,
            max_morphs=None, log=None, force_double_sided=False):
    # 确保输出目录存在（CLI 直接调用时尤其容易漏建）
    _odir = os.path.dirname(os.path.abspath(vrm_path))
    try:
        os.makedirs(_odir, exist_ok=True)
    except OSError as e:
        raise OSError("无法创建输出目录 %s：%s" % (_odir, e))

    """返回统计 dict。log(msg, tag=None) 用于输出进度。

    reverse_winding:
        "auto"（默认）——用「几何面法线 vs 顶点法线」投票自动判定；
        True / False   ——手动强制反转 / 不反转。

    实测 PMX 与 glTF 的缠绕约定一致（从外面看都是逆时针），
    所以正常情况下 auto 会判定为「不反转」。以前硬编码 True 会让模型
    被背面剔除，表现就是大面积镂空 + 轮廓线糊成黑色。
    """

    def _log(msg, tag=None):
        if log is not None:
            try:
                log(msg, tag)
            except TypeError:
                log(msg)

    model = pmxio.read_pmx(pmx_path)
    base_dir = os.path.dirname(os.path.abspath(pmx_path))
    verts = model["vertices"]
    bones = model["bones"]
    faces = model["faces"]
    materials = model["materials"]
    is1 = str(spec).startswith("1")

    # ---- 缠绕顺序自动判定（PMX 与 glTF 约定一致，通常不需要反转）
    if reverse_winding in ("auto", None, ""):
        flip, ag, dis = pmxio.detect_winding(
            [v["pos"] for v in verts],
            [v["normal"] for v in verts], faces)
        reverse_winding = bool(flip)
        _log("绕序自动判定：与法线同向 %d 面 / 反向 %d 面 → %s"
             % (ag, dis, "需要反转" if flip else "保持不变"))
    else:
        reverse_winding = bool(reverse_winding)

    # ---- 缩放
    if scale_mode in ("auto", "mmd", None, ""):
        ys = [v["pos"][1] for v in verts] or [0.0]
        height = max(ys) - min(ys)
        scale = (1.6 / height) if height > 1e-6 else 0.08
    else:
        scale = float(scale_mode)

    # ---- 旋转
    if rotate in ("auto", None, ""):
        rotate = "y180" if is1 else "none"
    y180 = (rotate == "y180")

    def xf(p):
        x, y, z = p
        if y180:
            x, z = -x, -z
        return (x * scale, y * scale, z * scale)

    def xf_dir(p):
        x, y, z = p
        if y180:
            x, z = -x, -z
        return (x, y, z)

    def unit(left_amount, up_amount, front_amount):
        if is1:                       # VRM 1.0：左 +X、前 +Z
            return (left_amount, up_amount, front_amount)
        return (-left_amount, up_amount, -front_amount)

    _log("PMX：顶点 %d · 三角面 %d · 骨骼 %d · 材质 %d · 表情 %d"
         % (len(verts), len(faces) // 3, len(bones), len(materials),
            len(model["morphs"])))
    _log("缩放 %.5f（MMD 单位 → 米）· 朝向 %s · 绕序%s"
         % (scale, "旋转 180°" if y180 else "保持不变",
            "反转" if reverse_winding else "保持不变"))

    # ---- 骨骼 → glTF 节点
    order, remap = _topo_bones(bones)
    nodes = []
    node_of_bone = [0] * len(bones)
    top_nodes = []
    for new_i, old_i in enumerate(order):
        b = bones[old_i]
        pos = xf(b["pos"])
        par = b["parent"]
        if 0 <= par < len(bones) and par != old_i:
            ppos = xf(bones[par]["pos"])
            trans = (pos[0] - ppos[0], pos[1] - ppos[1], pos[2] - ppos[2])
            parent_node = node_of_bone[remap[par]]
        else:
            trans = pos
            parent_node = None
        idx = len(nodes)
        nodes.append({"name": b["name"] or ("bone%d" % old_i),
                      "translation": [trans[0], trans[1], trans[2]]})
        node_of_bone[new_i] = idx
        if parent_node is not None and parent_node != idx:
            nodes[parent_node].setdefault("children", []).append(idx)
        else:
            top_nodes.append(idx)

    def bone_node(pmx_index):
        if 0 <= pmx_index < len(bones):
            return node_of_bone[remap[pmx_index]]
        return None

    if not bones:                                   # 极端情况：没有骨骼
        nodes.append({"name": "root_bone", "translation": [0.0, 0.0, 0.0]})
        top_nodes.append(len(nodes) - 1)

    # ---- humanoid 映射
    human = {}
    for i, b in enumerate(bones):
        v = MMD_TO_VRM.get(b["name"])
        if not v:
            v = ALT_TO_VRM.get(_norm(b.get("name_en") or ""))
        if v and v not in human:
            human[v] = node_of_bone[remap[i]]

    def ensure(name):
        if name in human:
            return human[name]
        pnode = None
        pname = VRM_PARENT.get(name)
        if pname:
            pnode = ensure(pname)
        off = _SYNTH.get(name, (0.0, 0.05, 0.0))
        lv, uv, fv = unit(off[0], off[1], off[2])
        if name == "hips" and pnode is None:
            trans = [0.0, _HIPS_Y, 0.0]
        else:
            trans = [lv, uv, fv]
        nodes.append({"name": "vrm_" + name, "translation": trans})
        idx = len(nodes) - 1
        if pnode is not None:
            nodes[pnode].setdefault("children", []).append(idx)
        else:
            top_nodes.append(idx)
        human[name] = idx
        return idx

    for r in (REQUIRED_1_0 if is1 else REQUIRED_0X):
        if r not in human:
            ensure(r)
            _log("提示：模型缺少 %s，已自动生成占位骨骼" % r, "warn")

    # ---- 逐材质构建网格
    gb = vrmio.GLBBuilder()
    gb.samplers.append(vrmio.default_sampler())
    img_cache = {}
    gltf_materials = []
    vrm_material_props = []
    prims = []

    def resolve_tex(idx):
        if idx is None or idx < 0 or idx >= len(model["textures"]):
            return -1
        if idx in img_cache:
            return img_cache[idx]
        rel = model["textures"][idx].replace("\\", "/")
        data = None
        for c in (os.path.join(base_dir, rel),
                  os.path.join(base_dir, os.path.basename(rel))):
            if os.path.isfile(c):
                data, mime = vrmio.load_texture(c)
                if data:
                    break
        if not data:
            img_cache[idx] = -1
            return -1
        ti = gb.add_texture(gb.add_image(data, mime))
        img_cache[idx] = ti
        return ti

    cursor = 0
    for mi_, mat in enumerate(materials):
        cnt = mat["faces"]
        tri_count = cnt // 3
        positions, normals, uvs, joints, weights = [], [], [], [], []
        local = {}
        idxs = []

        def get_local(vi):
            j = local.get(vi)
            if j is not None:
                return j
            v = verts[vi]
            p = xf(v["pos"])
            n = xf_dir(v["normal"])
            ln = math.sqrt(n[0] * n[0] + n[1] * n[1] + n[2] * n[2]) or 1.0
            n = (n[0] / ln, n[1] / ln, n[2] / ln)
            j = len(positions) // 3
            local[vi] = j
            positions.extend(p)
            normals.extend(n)
            uvs.extend(v["uv"])
            wt = v["wtype"]
            bs = [x for x in v["wbones"] if 0 <= x < len(bones)]
            if not bs:
                bs = [0]
            if wt == 0:
                wl = [(bs[0], 1.0)]
            elif wt in (1, 3):
                w0 = v["wweights"][0] if v["wweights"] else 1.0
                second = bs[1] if len(bs) > 1 else bs[0]
                wl = [(bs[0], w0), (second, 1.0 - w0)]
            else:
                ws = list(v["wweights"]) + [0.0] * 4
                wl = [(bs[k] if k < len(bs) else bs[0], ws[k])
                      for k in range(4)]
                if sum(x[1] for x in wl) <= 1e-9:
                    wl = [(bs[0], 1.0), (bs[0], 0.0), (bs[0], 0.0), (bs[0], 0.0)]
            wl = [(bone_node(b) if bone_node(b) is not None else 0, w)
                  for b, w in wl]
            tot = sum(w for _, w in wl) or 1.0
            wl = [(b, w / tot) for b, w in wl]
            while len(wl) < 4:
                wl.append((wl[0][0], 0.0))
            joints.extend([b for b, _ in wl[:4]])
            weights.extend([w for _, w in wl[:4]])
            return j

        for t in range(tri_count):
            a = faces[cursor + t * 3]
            b = faces[cursor + t * 3 + 1]
            c = faces[cursor + t * 3 + 2]
            ia, ib, ic = get_local(a), get_local(b), get_local(c)
            if reverse_winding:
                ia, ic = ic, ia
            idxs.extend([ia, ib, ic])
        cursor += cnt

        if not idxs:
            continue
        prims.append({"local": local, "vcount": len(local),
                      "idxs": idxs, "pos": positions, "nrm": normals,
                      "uv": uvs, "jn": joints, "wt": weights,
                      "mat": mi_, "targets": []})

        ti = resolve_tex(mat["tex"])
        diff = list(mat["diffuse"])
        opaque = diff[3] >= 0.999
        gmat = {
            "name": mat["name"] or ("mat%d" % mi_),
            "pbrMetallicRoughness": {
                "baseColorFactor": [diff[0], diff[1], diff[2], diff[3]],
                "metallicFactor": 0.0,
                "roughnessFactor": 0.9,
            },
            "doubleSided": True if force_double_sided else bool(mat["flag"] & 0x01),
            "emissiveFactor": [0.0, 0.0, 0.0],
            "alphaMode": "OPAQUE" if opaque else "BLEND",
        }
        if ti >= 0:
            gmat["pbrMetallicRoughness"]["baseColorTexture"] = {"index": ti}
        gltf_materials.append(gmat)

        if not is1:
            vrm_material_props.append({
                "name": gmat["name"],
                "shader": "VRM/MToon",
                "renderQueue": 2000 if opaque else 3000,
                "floatProperties": {
                    "_Cutoff": 0.5, "_BumpScale": 1.0,
                    "_ReceiveShadowRate": 1.0, "_ShadeShift": 0.0,
                    "_ShadeToony": 0.9, "_LightColorAttenuation": 0.0,
                    "_IndirectLightIntensity": 0.1, "_RimLightingMix": 0.0,
                    "_RimFresnelPower": 1.0, "_RimLift": 0.0,
                    "_OutlineWidth": 0.0, "_OutlineScaledMaxDistance": 1.0,
                    "_OutlineLightingMix": 1.0, "_UvAnimScrollX": 0.0,
                    "_UvAnimScrollY": 0.0, "_UvAnimRotation": 0.0,
                    "_MToonVersion": 0.0, "_DebugMode": 0.0,
                    "_BlendMode": 0.0 if opaque else 2.0,
                    "_OutlineWidthMode": 0.0, "_OutlineColorMode": 0.0,
                    "_CullMode": 0.0 if (mat["flag"] & 0x01) else 1.0,
                    "_OutlineCullMode": 1.0, "_SrcBlend": 1.0,
                    "_DstBlend": 0.0, "_ZWrite": 1.0, "_AlphaToMask": 0.0,
                },
                "vectorProperties": {
                    "_Color": [diff[0], diff[1], diff[2], diff[3]],
                    "_ShadeColor": [0.97, 0.81, 0.86, 1.0],
                    "_EmissionColor": [0.0, 0.0, 0.0, 1.0],
                    "_OutlineColor": [0.0, 0.0, 0.0, 1.0],
                    "_RimColor": [0.0, 0.0, 0.0, 1.0],
                },
                "textureProperties": ({"_MainTex": ti} if ti >= 0 else {}),
                "keywordMap": {"_NORMALMAP": False, "_ALPHATEST_ON": False,
                               "_ALPHABLEND_ON": not opaque,
                               "_ALPHAPREMULTIPLY_ON": False},
                "tagMap": {"RenderType": "Opaque" if opaque else "Transparent"},
            })

    _log("网格：%d 个 primitive（材质）" % len(prims))
    if not prims:
        raise RuntimeError("这个 PMX 没有可用的面数据")

    # ---- 写 accessor
    for pr in prims:
        pr["acc"] = {
            "POSITION": gb.add_accessor(pr["pos"], vrmio.FLOAT, "VEC3", 34962),
            "NORMAL": gb.add_accessor(pr["nrm"], vrmio.FLOAT, "VEC3", 34962),
            "TEXCOORD_0": gb.add_accessor(pr["uv"], vrmio.FLOAT, "VEC2", 34962),
            "JOINTS_0": gb.add_accessor(pr["jn"], vrmio.UNSIGNED_SHORT,
                                        "VEC4", 34962),
            "WEIGHTS_0": gb.add_accessor(pr["wt"], vrmio.FLOAT, "VEC4", 34962),
        }
        pr["indices"] = gb.add_accessor(pr["idxs"], vrmio.UNSIGNED_INT,
                                        "SCALAR", 34963, minmax=False)

    # ---- 表情 → morph target
    flat = _flatten_morphs(model["morphs"])
    # max_morphs: None = 全部；0 = 不导出；n = 只取前 n 个
    if max_morphs is not None and max_morphs >= 0:
        flat = flat[:int(max_morphs)]
    target_names = []
    expr_preset = {}
    expr_custom = {}
    blend_groups = []
    table = EXPR_1_0 if is1 else EXPR_0X

    for mname, deltas in flat:
        slot = len(target_names)
        for pr in prims:
            arr = [0.0] * (pr["vcount"] * 3)
            loc = pr["local"]
            for vi, d in deltas.items():
                j = loc.get(vi)
                if j is None:
                    continue
                dx, dy, dz = xf_dir(d)
                arr[j * 3] = dx
                arr[j * 3 + 1] = dy
                arr[j * 3 + 2] = dz
            pr["targets"].append(
                {"POSITION": gb.add_accessor(arr, vrmio.FLOAT, "VEC3", 34962)})
        target_names.append(mname)
        preset = table.get(mname)
        if is1:
            obj = {"morphTargetBinds": [{"node": 0, "index": slot,
                                         "weight": 1.0}],
                   "materialColorBinds": [], "textureTransformBinds": [],
                   "isBinary": False}
            if preset and preset not in expr_preset:
                expr_preset[preset] = obj
            else:
                expr_custom[mname] = obj
        else:
            blend_groups.append({"name": mname,
                                 "presetName": preset or "Unknown",
                                 "binds": [{"mesh": 0, "index": slot,
                                            "weight": 100}],
                                 "materialValues": [], "isBinary": False})
    if target_names:
        _log("表情：导出 %d 个 morph target" % len(target_names))

    # ---- 收尾节点
    gltf_mesh = {"name": model["name"] or "body", "primitives": []}
    for pr in prims:
        p = {"attributes": pr["acc"], "indices": pr["indices"],
             "material": pr["mat"]}
        if pr["targets"]:
            p["targets"] = pr["targets"]
        gltf_mesh["primitives"].append(p)
    if target_names:
        gltf_mesh["extras"] = {"targetNames": target_names}

    inv = []
    for i in range(len(bones)):
        p = xf(bones[i]["pos"])
        inv.extend([1.0, 0.0, 0.0, 0.0,
                    0.0, 1.0, 0.0, 0.0,
                    0.0, 0.0, 1.0, 0.0,
                    -p[0], -p[1], -p[2], 1.0])
    if not inv:
        inv = [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0]
    ibm = gb.add_accessor(inv, vrmio.FLOAT, "MAT4", minmax=False)

    mesh_node_index = len(nodes)
    nodes.append({"name": "mesh", "mesh": 0, "skin": 0})
    root_index = len(nodes)
    nodes.append({"name": "root",
                  "children": list(top_nodes) + [mesh_node_index]})

    joints = [node_of_bone[remap[i]] for i in range(len(bones))]
    if not joints:
        joints = [0]

    gltf = {
        "asset": {"version": "2.0", "generator": "pmx2vrm.py (pure python)"},
        "scene": 0,
        "scenes": [{"nodes": [root_index]}],
        "nodes": nodes,
        "meshes": [gltf_mesh],
        "skins": [{"joints": joints, "inverseBindMatrices": ibm,
                   "skeleton": root_index}],
        "materials": gltf_materials,
        "accessors": gb.accessors,
        "bufferViews": gb.bufferViews,
        "buffers": [{"byteLength": len(gb.bin)}],
    }
    if gb.images:
        gltf["images"] = gb.images
        gltf["textures"] = gb.textures
        gltf["samplers"] = gb.samplers

    # morphTargetBinds 的 node 指向 mesh 节点；0.x 的 mesh 指向 mesh 索引
    if is1:
        for grp in (expr_preset, expr_custom):
            for obj in grp.values():
                for b in obj["morphTargetBinds"]:
                    b["node"] = mesh_node_index
    else:
        for g in blend_groups:
            for b in g["binds"]:
                b["mesh"] = 0

    # ---- VRM 扩展
    meta = meta or {}
    title = meta.get("title") or model["name"] or "PMX Model"
    author = meta.get("author") or ""
    if is1:
        vrm_ext = {
            "specVersion": "1.0",
            "humanoid": {"humanBones": {k: {"node": v}
                                        for k, v in human.items()}},
            "meta": {
                "name": title,
                "version": meta.get("version", "1"),
                "authors": [author] if author else ["unknown"],
                "copyrightInformation": meta.get("copyright", ""),
                "contactInformation": meta.get("contact", ""),
                "references": [],
                "thirdPartyLicenses": "",
                "thumbnailImage": None,
                "licenseUrl": meta.get("license_url",
                                       "https://vrm.dev/licenses/1.0/"),
                "avatarPermission": meta.get("avatar_permission", "everyone"),
                "allowExcessivelyViolentUsage": False,
                "allowExcessivelySexualUsage": False,
                "commercialUsage": meta.get("commercial_usage",
                                            "personalNonProfit"),
                "allowPoliticalOrReligiousUsage": False,
                "allowAntisocialOrHateUsage": False,
                "creditNotation": "required",
                "allowRedistribution": False,
                "modification": "prohibited",
            },
            "firstPerson": {"meshAnnotations": [{"node": mesh_node_index,
                                                 "type": "auto"}]},
        }
        if expr_preset or expr_custom:
            vrm_ext["expressions"] = {}
            if expr_preset:
                vrm_ext["expressions"]["preset"] = expr_preset
            if expr_custom:
                vrm_ext["expressions"]["custom"] = expr_custom
        if "leftEye" in human or "rightEye" in human:
            vrm_ext["lookAt"] = {
                "type": "bone",
                "offsetFromHeadBone": [0.0, 0.06, 0.0],
                "rangeMapHorizontalInner": {"inputMaxValue": 90.0,
                                            "outputScale": 10.0},
                "rangeMapHorizontalOuter": {"inputMaxValue": 90.0,
                                            "outputScale": 10.0},
                "rangeMapVerticalDown": {"inputMaxValue": 90.0,
                                         "outputScale": 10.0},
                "rangeMapVerticalUp": {"inputMaxValue": 90.0,
                                       "outputScale": 10.0},
            }
        gltf["extensionsUsed"] = ["VRMC_vrm"]
        gltf["extensions"] = {"VRMC_vrm": vrm_ext}
    else:
        vrm_ext = {
            "exporterVersion": "pmx2vrm.py",
            "specVersion": "0.0",
            "meta": {
                "title": title,
                "version": meta.get("version", "1"),
                "author": author,
                "contactInformation": meta.get("contact", ""),
                "reference": "",
                "texture": None,
                "allowedUserName": "Everyone",
                "violentUssageName": "Disallow",
                "sexualUssageName": "Disallow",
                "commercialUssageName": meta.get("commercial_0x", "Disallow"),
                "otherPermissionUrl": "",
                "licenseName": "Other",
                "otherLicenseUrl": meta.get("license_url", ""),
            },
            "humanoid": {
                "humanBones": [{"bone": k, "node": v, "useDefaultValues": True}
                               for k, v in human.items()],
                "armStretch": 0.05, "legStretch": 0.05,
                "upperArmTwist": 0.5, "lowerArmTwist": 0.5,
                "upperLegTwist": 0.5, "lowerLegTwist": 0.5,
                "feetSpacing": 0.0, "hasTranslationDoF": False,
            },
            "firstPerson": {
                "firstPersonBone": human.get("head", 0),
                "firstPersonBoneOffset": {"x": 0.0, "y": 0.06, "z": 0.0},
                "meshAnnotations": [{"firstPersonFlag": "Auto", "mesh": 0}],
                "lookAtTypeName": ("Bone" if ("leftEye" in human or
                                              "rightEye" in human)
                                   else "BlendShape"),
                "lookAtHorizontalInner": _degmap(90, 10),
                "lookAtHorizontalOuter": _degmap(90, 10),
                "lookAtVerticalDown": _degmap(90, 10),
                "lookAtVerticalUp": _degmap(90, 10),
            },
            "blendShapeMaster": {"blendShapeGroups": blend_groups},
            "secondaryAnimation": {"boneGroups": []},
            "materialProperties": vrm_material_props,
        }
        gltf["extensionsUsed"] = ["VRM"]
        gltf["extensions"] = {"VRM": vrm_ext}

    size = vrmio.write_glb(gltf, bytes(gb.bin), vrm_path)
    _log("已写出 %s（%.2f MB）· 节点 %d · 表情 %d"
         % (os.path.basename(vrm_path), size / 1048576.0, len(nodes),
            len(target_names)), "ok")
    return {"nodes": len(nodes), "primitives": len(prims),
            "morphs": len(target_names), "bytes": size, "scale": scale,
            "vertices": sum(p["vcount"] for p in prims)}


# -------------------------------------------------------------------- cli --
def main(argv=None):
    ap = argparse.ArgumentParser(description="PMX → VRM（纯 Python）")
    ap.add_argument("pmx")
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--spec", default="1.0", choices=["1.0", "0x"])
    ap.add_argument("--scale", default="auto",
                    help="auto（归一到 1.6 m）或具体倍率")
    ap.add_argument("--rotate", default="auto", choices=["auto", "none", "y180"])
    ap.add_argument("--flip-winding", action="store_true",
                    help="强制反转三角形缠绕顺序（默认自动判定）")
    ap.add_argument("--force-double-sided", action="store_true",
                    help="所有材质强制双面（模型仍有镂空时的兜底手段）")
    ap.add_argument("--max-morphs", type=int, default=None,
                    help="只导出前 N 个表情（0 = 不导出，默认全部）")
    ap.add_argument("--title", default=None)
    ap.add_argument("--author", default=None)
    a = ap.parse_args(argv)

    out = a.out or (os.path.splitext(a.pmx)[0] + ".vrm")
    meta = {}
    if a.title:
        meta["title"] = a.title
    if a.author:
        meta["author"] = a.author
    st = convert(a.pmx, out, spec=("1.0" if a.spec == "1.0" else "0.x"),
                 scale_mode=a.scale, rotate=a.rotate,
                 reverse_winding=(True if a.flip_winding else "auto"),
                 meta=meta, max_morphs=a.max_morphs,
                 force_double_sided=a.force_double_sided,
                 log=lambda msg, tag=None: print(msg))
    print("完成：%s（%.2f MB）" % (out, st["bytes"] / 1048576.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
