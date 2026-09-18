# -*- coding: utf-8 -*-
"""vrm2pmx.py - 把 VRM（0.x / 1.0）转成 MMD 的 PMX 2.0，纯 Python 标准库。

反向流程见 pmx2vrm.py 头部说明，坐标与绕序的处理互为逆操作：
  * VRM（右手系）→ PMX（左手系）手性相反：1.0 取反 Z（脸 +Z → -Z），
    0.x 取反 X（左手 -X → +X）；两种情况三角绕序都要反转。
  * glTF 逆时针为正面 → PMX 顺时针为正面，反转三角形缠绕顺序。
  * 米 → MMD 单位，默认把模型归一到 20 单位高（MMD 常规身高）。

用法：
    python vrm2pmx.py model.vrm -o model.pmx
    python vrm2pmx.py model.vrm --scale 12.5
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pmxio
import vrmio


# ------------------------------------------------------------------ 工具 --
def _log_default(msg, tag=None):
    print(msg)


def _uniq(name, used):
    base = (name or "bone").strip() or "bone"
    if base not in used:
        used.add(base)
        return base
    i = 1
    while "%s_%d" % (base, i) in used:
        i += 1
    out = "%s_%d" % (base, i)
    used.add(out)
    return out


def _get(gltf, key, i, default=None):
    lst = gltf.get(key) or []
    if 0 <= i < len(lst):
        return lst[i]
    return default


def _accessor_of(prim_or_target, name):
    return prim_or_target.get(name)


# ---- 列主序 4x4 仿射矩阵工具（glTF 的 matrix / IBM 都是这个布局）----
_M4_I = (1.0, 0.0, 0.0, 0.0,
         0.0, 1.0, 0.0, 0.0,
         0.0, 0.0, 1.0, 0.0,
         0.0, 0.0, 0.0, 1.0)


def _m4_mul(a, b):
    """a·b，列主序。"""
    o = [0.0] * 16
    for c in range(4):
        for r in range(4):
            o[c * 4 + r] = (a[r] * b[c * 4] + a[4 + r] * b[c * 4 + 1] +
                            a[8 + r] * b[c * 4 + 2] +
                            a[12 + r] * b[c * 4 + 3])
    return o


def _m4_inv(m):
    """4x4 求逆（Gauss-Jordan，部分主元），列主序进出。"""
    a = [[m[c * 4 + r] for c in range(4)] +
         [1.0 if r == c else 0.0 for c in range(4)]
         for r in range(4)]
    for col in range(4):
        piv = max(range(col, 4), key=lambda r: abs(a[r][col]))
        if abs(a[piv][col]) < 1e-12:
            return None
        if piv != col:
            a[col], a[piv] = a[piv], a[col]
        d = a[col][col]
        a[col] = [x / d for x in a[col]]
        for r in range(4):
            if r != col and a[r][col]:
                f = a[r][col]
                a[r] = [x - f * y for x, y in zip(a[r], a[col])]
    return [a[r][4 + c] for c in range(4) for r in range(4)]


def _m4_is_ident(m, eps=1e-6):
    for c in range(4):
        for r in range(4):
            want = 1.0 if c == r else 0.0
            if abs(m[c * 4 + r] - want) > eps:
                return False
    return True


def _m4_apply(m, p):
    x, y, z = p
    return (m[0] * x + m[4] * y + m[8] * z + m[12],
            m[1] * x + m[5] * y + m[9] * z + m[13],
            m[2] * x + m[6] * y + m[10] * z + m[14])


def _m3_apply(m, n):
    x, y, z = n
    return (m[0] * x + m[4] * y + m[8] * z,
            m[1] * x + m[5] * y + m[9] * z,
            m[2] * x + m[6] * y + m[10] * z)


# ---------------------------------------------------------------- 主流程 --
def convert(vrm_path, pmx_path, scale_mode="auto", rotate="auto",
            reverse_winding="auto", name=None, log=None, tex_dir=None,
            enable_edge=False, force_double_sided=False):
    """把 VRM / GLB 转成 PMX 2.0。

    reverse_winding:
        "auto"（默认）——用「几何面法线 vs 顶点法线」投票自动判定；
        True / False   ——手动强制反转 / 不反转。
    enable_edge:
        是否给材质开启 MMD 轮廓线。VRM 没有轮廓线概念，密集网格上
        MMD 的轮廓容易糊成一片黑，所以默认关闭。
    force_double_sided:
        所有材质强制双面（模型出现镂空时的兜底手段）。
    """
    log = log or _log_default

    def _l(msg, tag=None):
        try:
            log(msg, tag)
        except TypeError:
            log(msg)

    gltf, bin_data = vrmio.read_glb(vrm_path)
    ext = gltf.get("extensions") or {}
    vrm1 = ext.get("VRMC_vrm")
    vrm0 = ext.get("VRM")
    if vrm1 is None and vrm0 is None:
        _l("警告：没找到 VRM 扩展，按普通 glTF 处理", "warn")
    is1 = vrm1 is not None

    # ---- 手性修正 / 缩放（2026-09-18 重写，别改回「绕 Y 旋转 180°」）
    # VRM（glTF 右手系）→ PMX（DirectX 左手系）：同一组数字直接用是
    # 镜像人形，必须反射一个轴：0.x 取反 x（脸 -Z、左手 -X → 左手 +X），
    # 1.0 取反 z（脸 +Z、左手 +X → 脸 -Z、左手 +X）。
    # y180 是纯旋转，改不了手性 → 转出的 PMX 左右翻转。
    if rotate in ("auto", "y180", None, ""):      # y180 为旧值，视同 auto
        mx, mz = (1.0, -1.0) if is1 else (-1.0, 1.0)
    else:                                          # "none"
        mx, mz = 1.0, 1.0

    nodes = gltf.get("nodes") or []
    worlds = vrmio.node_world_matrices(gltf)

    # ---- 先收集网格，用于算包围盒
    meshes = gltf.get("meshes") or []
    skins = gltf.get("skins") or []

    def prim_arrays(prim):
        a = prim.get("attributes") or {}
        out = {}
        for key, acc in (("POSITION", a.get("POSITION")),
                         ("NORMAL", a.get("NORMAL")),
                         ("TEXCOORD_0", a.get("TEXCOORD_0")),
                         ("JOINTS_0", a.get("JOINTS_0")),
                         ("WEIGHTS_0", a.get("WEIGHTS_0"))):
            out[key] = (vrmio.read_accessor(gltf, bin_data, acc)
                        if acc is not None else None)
        out["indices"] = (vrmio.read_accessor(gltf, bin_data, prim["indices"])
                          if "indices" in prim else None)
        return out

    # ---- 缠绕顺序自动判定（必须用手性修正后的数据投票）
    # VRM 的面「从外面看逆时针」（glTF 规范）；镜面反射会把它颠倒成
    # 顺时针，所以正常情况下 auto 会判定为「需要反转」——反射后的
    # 三角形必须反转绕序，MMD 里才是正确的正面。
    # 以前硬编码 True / 不反射，分别对应「镂空黑面」与「左右翻转」。
    if reverse_winding in ("auto", None, ""):
        ag = dis = 0
        for mesh in meshes:
            for prim in (mesh.get("primitives") or []):
                arr = prim_arrays(prim)
                p = arr["POSITION"] or []
                nv = arr["NORMAL"]
                idx = arr["indices"]
                if not nv or len(p) < 300:
                    continue
                nn = len(p) // 3
                if idx is None:
                    idx = list(range(nn))
                _, a2, d2 = pmxio.detect_winding(
                    [(p[i * 3] * mx, p[i * 3 + 1], p[i * 3 + 2] * mz)
                     for i in range(nn)],
                    [(nv[i * 3] * mx, nv[i * 3 + 1], nv[i * 3 + 2] * mz)
                     for i in range(nn)],
                    idx, sample=1500)
                ag += a2
                dis += d2
                break
            if ag + dis:
                break
        reverse_winding = bool(dis > ag)
        _l("绕序自动判定：与法线同向 %d 面 / 反向 %d 面 → %s"
           % (ag, dis, "需要反转" if reverse_winding else "保持不变"))
    else:
        reverse_winding = bool(reverse_winding)

    # 节点上的 mesh 归属
    mesh_of_node = {}
    for i, nd in enumerate(nodes):
        if "mesh" in nd:
            mesh_of_node[i] = nd["mesh"]

    # ---- 骨骼
    joint_set = set()
    for sk in skins:
        for j in (sk.get("joints") or []):
            joint_set.add(j)
    # humanoid 里登记的骨骼即便没参与蒙皮也一并保留，避免丢掉头/眼等关键节点
    hb = (vrm1 or vrm0 or {}).get("humanoid") or {}
    if is1:
        for info in (hb.get("humanBones") or {}).values():
            if isinstance(info, dict) and "node" in info:
                joint_set.add(info["node"])
    elif vrm0:
        for info in (hb.get("humanBones") or []):
            if "node" in info:
                joint_set.add(info["node"])
    if not joint_set:
        # 没有蒙皮：把所有 node 都当成骨骼
        joint_set = set(range(len(nodes)))

    root_nodes = []
    for sc in (gltf.get("scenes") or [{}]):
        for n in (sc.get("nodes") or []):
            root_nodes.append(n)
    if not root_nodes:
        root_nodes = list(range(len(nodes)))

    parent_of = [-1] * len(nodes)
    for i, nd in enumerate(nodes):
        for c in (nd.get("children") or []):
            if 0 <= c < len(nodes):
                parent_of[c] = i

    # 按 DFS 顺序遍历，保证父在前
    ordered = []
    seen = set()
    stack = list(reversed(root_nodes))
    while stack:
        i = stack.pop()
        if i in seen or not (0 <= i < len(nodes)):
            continue
        seen.add(i)
        ordered.append(i)
        for c in reversed(nodes[i].get("children") or []):
            stack.append(c)
    for i in range(len(nodes)):
        if i not in seen:
            ordered.append(i)

    joint_list = [i for i in ordered if i in joint_set]
    joint_pos = {}
    for j in joint_list:
        joint_pos[j] = vrmio.world_translation(worlds[j])

    # ---- 每个 mesh 的「绑定姿势」变换（2026-09-18 加）
    # glTF 蒙皮：顶点世界位置 = Σ w · W_j · IBM_j · v（W_j = 关节静止世界
    # 矩阵）。规范文件 IBM_j = W_j⁻¹ → bind = W_j·IBM_j = 单位阵，顶点
    # 不需要修正（零开销快路径）。
    # 但不少导出器（UniGLTF 等）把顶点存在 mesh 节点的局部空间里，
    # IBM 相应差了一个公共量——Blender/UniVRM 导入时会按 W_j·IBM_j 把
    # 顶点摆到正确位置，直接读原始顶点就会整体错位（症状：身体零件
    # 散开、头发束乱飞）。此时 bind = mesh 节点的世界矩阵，把顶点乘
    # 回去即可。
    def _mesh_node_of(mesh_i):
        for nd_i, mi_ in mesh_of_node.items():
            if mi_ == mesh_i:
                return nd_i
        return None

    mesh_bind = {}
    for mesh_i, mesh in enumerate(meshes):
        node_i = _mesh_node_of(mesh_i)
        skin = None
        if node_i is not None and "skin" in nodes[node_i]:
            skin = _get(gltf, "skins", nodes[node_i]["skin"])
        bind_mats = None
        if skin and skin.get("inverseBindMatrices") is not None:
            joints_l = skin.get("joints") or []
            ibm_flat = vrmio.read_accessor(
                gltf, bin_data, skin["inverseBindMatrices"])
            if ibm_flat and len(ibm_flat) >= 16 * len(joints_l):
                bind_mats = []
                all_ident = True
                for i2, jn2 in enumerate(joints_l):
                    wm = worlds[jn2] if 0 <= jn2 < len(worlds) else None
                    if wm is None:
                        bind_mats = None
                        break
                    # bind = W_j · IBM_j（注意：不是乘 IBM 的逆！
                    # 蒙皮公式里 IBM 直接右乘顶点）
                    bm = _m4_mul(wm, list(ibm_flat[i2 * 16:(i2 + 1) * 16]))
                    bind_mats.append(bm)
                    if not _m4_is_ident(bm):
                        all_ident = False
                if all_ident:
                    bind_mats = None
        mesh_bind[mesh_i] = bind_mats

    # 包围盒（用骨骼 + 网格顶点粗算高度；顶点要先过绑定变换，
    # 否则像 UniGLTF 这种 mesh 局部空间的文件会算出错误缩放）
    ymin = ymax = 0.0
    if joint_pos:
        ys = [p[1] for p in joint_pos.values()]
        ymin, ymax = min(ys), max(ys)
    for mesh_i, m in enumerate(meshes):
        bind = mesh_bind.get(mesh_i)
        for pr in m.get("primitives") or []:
            attrs = pr.get("attributes") or {}
            acc = attrs.get("POSITION")
            if acc is None:
                continue
            arr = vrmio.read_accessor(gltf, bin_data, acc)
            if not arr:
                continue
            if bind is None:
                yy = arr[1::3]
                ymin = min(ymin, min(yy))
                ymax = max(ymax, max(yy))
                continue
            jna = attrs.get("JOINTS_0")
            wta = attrs.get("WEIGHTS_0")
            jnv = vrmio.read_accessor(gltf, bin_data, jna) if jna is not None else None
            wtv = vrmio.read_accessor(gltf, bin_data, wta) if wta is not None else None
            nn = len(arr) // 3
            for v in range(nn):
                y = 0.0
                wsum = 0.0
                if jnv and wtv:
                    for k in range(4):
                        w = wtv[v * 4 + k] if len(wtv) > v * 4 + k else 0.0
                        j = int(jnv[v * 4 + k]) if len(jnv) > v * 4 + k else 0
                        if w > 1e-6 and 0 <= j < len(bind):
                            bm = bind[j]
                            x = arr[v * 3]
                            z = arr[v * 3 + 2]
                            y += w * (bm[1] * x + bm[5] * arr[v * 3 + 1] +
                                      bm[9] * z + bm[13])
                            wsum += w
                if wsum > 1e-6:
                    if y < ymin:
                        ymin = y
                    if y > ymax:
                        ymax = y
                else:
                    y = arr[v * 3 + 1]
                    if y < ymin:
                        ymin = y
                    if y > ymax:
                        ymax = y
    height = ymax - ymin

    if scale_mode in ("auto", "mmd", None, ""):
        scale = (20.0 / height) if height > 1e-6 else 12.5
    else:
        scale = float(scale_mode)

    def xf(p):
        x, y, z = p
        return (x * mx * scale, y * scale, z * mz * scale)

    _l("VRM：%s · 节点 %d · 网格 %d · 蒙皮 %d"
       % ("1.0" if is1 else ("0.x" if vrm0 else "无扩展"), len(nodes),
          len(meshes), len(skins)))
    _l("缩放 %.4f（米 → MMD 单位）· 手性修正 %s · 绕序%s"
       % (scale,
          ("z 取反" if is1 else "x 取反") if (mx, mz) != (1.0, 1.0) else "无",
          "反转" if reverse_winding else "保持不变"))

    # ---- 骨骼 → PMX
    joint_slot = {}
    pmx_bones = []
    used_names = set()
    for j in joint_list:
        joint_slot[j] = len(pmx_bones)
        pos = xf(joint_pos[j])
        # 父骨骼 = 最近的同时也是 joint 的祖先
        p = parent_of[j]
        while p >= 0 and p not in joint_slot:
            p = parent_of[p]
        pmx_bones.append({
            "name": _uniq((nodes[j].get("name") or "bone"), used_names),
            "name_en": nodes[j].get("name") or "bone",
            "pos": pos,
            "parent": joint_slot.get(p, -1) if p >= 0 else -1,
            "layer": 0, "flag": 0, "tail_kind": 0, "tail": (0.0, 0.0, 0.0),
            "inherit_rot": None, "inherit_mov": None, "fixed_axis": None,
            "local_axis": None, "external": None, "ik": None,
            "_children": [],
        })
    for j in joint_list:
        p = parent_of[j]
        while p >= 0 and p not in joint_slot:
            p = parent_of[p]
        if p >= 0:
            pmx_bones[joint_slot[p]]["_children"].append(joint_slot[j])

    for i, b in enumerate(pmx_bones):
        flag = 0x0002 | 0x0004 | 0x0008 | 0x0010
        kids = b.pop("_children")
        if kids:
            flag |= 0x0001
            b["tail_kind"] = 1
            b["tail"] = kids[0]
        else:
            p = b["parent"]
            if 0 <= p < len(pmx_bones):
                dx = b["pos"][0] - pmx_bones[p]["pos"][0]
                dy = b["pos"][1] - pmx_bones[p]["pos"][1]
                dz = b["pos"][2] - pmx_bones[p]["pos"][2]
                ln = (dx * dx + dy * dy + dz * dz) ** 0.5
                if ln > 1e-6:
                    b["tail"] = (dx / ln, dy / ln, dz / ln)
                else:
                    b["tail"] = (0.0, 1.0, 0.0)
            else:
                b["tail"] = (0.0, 1.0, 0.0)
        b["flag"] = flag

    # ---- 贴图导出
    out_dir = os.path.dirname(os.path.abspath(pmx_path))
    if tex_dir:
        out_dir = tex_dir
    stem = os.path.splitext(os.path.basename(pmx_path))[0]
    pmx_textures = []
    tex_of_image = {}

    def export_texture(image_index):
        if image_index in tex_of_image:
            return tex_of_image[image_index]
        img = _get(gltf, "images", image_index)
        if img is None:
            tex_of_image[image_index] = -1
            return -1
        if "bufferView" in img:
            bv = gltf["bufferViews"][img["bufferView"]]
            off = bv.get("byteOffset", 0)
            data = bin_data[off:off + bv["byteLength"]]
        elif img.get("uri", "").startswith("data:"):
            import base64
            data = base64.b64decode(img["uri"].split(",", 1)[1])
        else:
            tex_of_image[image_index] = -1
            return -1
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError:
            pass
        path = os.path.join(out_dir, "%s_tex%02d.png" % (stem, image_index))
        try:
            real = vrmio.save_image(data, path)
        except OSError:
            tex_of_image[image_index] = -1
            return -1
        pmx_textures.append(os.path.basename(real))
        tex_of_image[image_index] = len(pmx_textures) - 1
        return tex_of_image[image_index]

    # ---- 顶点 / 面 / 材质
    pmx_verts = []
    pmx_faces = []
    pmx_materials = []
    target_slots = {}       # (mesh_i, prim_i, slot) → 全局顶点映射表
    prim_vertex_map = []    # 每个 primitive 的 local→global 列表
    prim_ctx = {}           # (mesh_i, prim_i) → 表情增量要过的线性变换素材

    for mesh_i, mesh in enumerate(meshes):
        node_i = _mesh_node_of(mesh_i)
        skin = None
        if node_i is not None and "skin" in nodes[node_i]:
            skin = _get(gltf, "skins", nodes[node_i]["skin"])
        joint_nodes = [joint_slot.get(j, -1)
                       for j in ((skin or {}).get("joints") or [])]
        has_weights = bool(joint_nodes)
        bind = mesh_bind.get(mesh_i)

        for prim_i, prim in enumerate(mesh.get("primitives") or []):
            arr = prim_arrays(prim)
            pos = arr["POSITION"] or []
            nrm = arr["NORMAL"]
            uv = arr["TEXCOORD_0"]
            jn = arr["JOINTS_0"]
            wt = arr["WEIGHTS_0"]
            idx = arr["indices"]
            n = len(pos) // 3
            if n == 0:
                continue
            if idx is None:
                idx = list(range(n))

            # 没有蒙皮时用节点世界矩阵烘进去
            if not has_weights and node_i is not None:
                m = worlds[node_i]
            else:
                m = None
            # glTF 的表情增量与 POSITION 处在同一个局部空间，所以必须过和顶点
            # **完全一样**的线性变换（节点世界矩阵，或蒙皮时按权重混合的绑定矩阵
            # 的线性部分）——平移项在「差值」里天然抵消，只用线性部分。
            prim_ctx[(mesh_i, prim_i)] = (m, bind, jn, wt, has_weights)

            base = len(pmx_verts)
            l2g = []
            for v in range(n):
                p = (pos[v * 3], pos[v * 3 + 1], pos[v * 3 + 2])
                nv0 = ((nrm[v * 3], nrm[v * 3 + 1], nrm[v * 3 + 2])
                       if nrm else None)
                if m:
                    p = _m4_apply(m, p)
                    if nv0:
                        nv0 = _m3_apply(m, nv0)
                elif bind is not None and has_weights and jn and wt:
                    # 绑定姿势修正：按权重混合各关节的 bind 矩阵
                    bx = by = bz = 0.0
                    b00 = b01 = b02 = b10 = b11 = b12 = 0.0
                    b20 = b21 = b22 = 0.0
                    btx = bty = btz = 0.0
                    wsum = 0.0
                    for k in range(4):
                        w = wt[v * 4 + k] if len(wt) > v * 4 + k else 0.0
                        j = int(jn[v * 4 + k]) if len(jn) > v * 4 + k else 0
                        if w > 1e-6 and 0 <= j < len(bind):
                            bm = bind[j]
                            b00 += w * bm[0]; b01 += w * bm[4]
                            b02 += w * bm[8]; b10 += w * bm[1]
                            b11 += w * bm[5]; b12 += w * bm[9]
                            b20 += w * bm[2]; b21 += w * bm[6]
                            b22 += w * bm[10]
                            btx += w * bm[12]; bty += w * bm[13]
                            btz += w * bm[14]
                            wsum += w
                    if wsum > 1e-6:
                        inv = 1.0 / wsum
                        px, py, pz = p
                        p = (b00 * px + b01 * py + b02 * pz + btx * inv,
                             b10 * px + b11 * py + b12 * pz + bty * inv,
                             b20 * px + b21 * py + b22 * pz + btz * inv)
                        if nv0:
                            nx = b00 * nv0[0] + b01 * nv0[1] + b02 * nv0[2]
                            ny = b10 * nv0[0] + b11 * nv0[1] + b12 * nv0[2]
                            nz = b20 * nv0[0] + b21 * nv0[1] + b22 * nv0[2]
                            nv0 = (nx, ny, nz)
                P = xf(p)
                if nv0:
                    nv = (nv0[0] * mx, nv0[1], nv0[2] * mz)
                    ln = (nv[0] ** 2 + nv[1] ** 2 + nv[2] ** 2) ** 0.5 or 1.0
                    N = (nv[0] / ln, nv[1] / ln, nv[2] / ln)
                else:
                    N = (0.0, 1.0, 0.0)
                U = (uv[v * 2], uv[v * 2 + 1]) if uv else (0.0, 0.0)
                wl = []
                if has_weights and jn and wt:
                    for k in range(4):
                        w = wt[v * 4 + k] if len(wt) > v * 4 + k else 0.0
                        if w > 1e-6:
                            b = int(jn[v * 4 + k])
                            b = joint_nodes[b] if 0 <= b < len(joint_nodes) else -1
                            if b >= 0:
                                wl.append((b, float(w)))
                if not wl:
                    wl = [(0, 1.0)]
                wl.sort(key=lambda t: -t[1])
                wl = wl[:4]
                tot = sum(x[1] for x in wl) or 1.0
                wl = [(b, w / tot) for b, w in wl]
                if len(wl) == 1:
                    wtype, wbones, wweights = 0, [wl[0][0]], []
                elif len(wl) == 2:
                    wtype, wbones = 1, [wl[0][0], wl[1][0]]
                    wweights = [wl[0][1]]
                else:
                    wtype = 2
                    wbones = [wl[k][0] if k < len(wl) else wl[0][0]
                              for k in range(4)]
                    wweights = [wl[k][1] if k < len(wl) else 0.0
                                for k in range(4)]
                pmx_verts.append({"pos": P, "normal": N, "uv": U,
                                  "add_uv": [], "wtype": wtype,
                                  "wbones": wbones, "wweights": wweights,
                                  "sdef": None, "edge": 1.0})
                l2g.append(len(pmx_verts) - 1)

            for t in range(0, len(idx) - 2, 3):
                a, b, c = idx[t], idx[t + 1], idx[t + 2]
                if a >= n or b >= n or c >= n:
                    continue
                a, b, c = l2g[a], l2g[b], l2g[c]
                if reverse_winding:
                    a, c = c, a
                pmx_faces.extend([a, b, c])

            prim_vertex_map.append({"mesh": mesh_i, "prim": prim_i,
                                    "l2g": l2g, "node": node_i})
            target_slots[(mesh_i, prim_i)] = l2g

            # ---- 材质
            gmat = _get(gltf, "materials", prim.get("material", -1), {}) or {}
            pbr = gmat.get("pbrMetallicRoughness") or {}
            diff = list(pbr.get("baseColorFactor") or [1.0, 1.0, 1.0, 1.0])
            tex_idx = -1
            ti = (pbr.get("baseColorTexture") or {}).get("index")
            if ti is not None:
                simg = (_get(gltf, "textures", ti) or {}).get("source")
                if simg is not None:
                    tex_idx = export_texture(simg)
            pmx_materials.append({
                "name": gmat.get("name") or ("mat%d" % len(pmx_materials)),
                "name_en": gmat.get("name") or ("mat%d" % len(pmx_materials)),
                "diffuse": (diff[0], diff[1], diff[2], diff[3]),
                "specular": (0.0, 0.0, 0.0),
                "shininess": 0.0,
                "ambient": (0.5, 0.5, 0.5),
        "flag": (0x0E
                 | (0x01 if (force_double_sided or gmat.get("doubleSided"))
                    else 0)
                 | (0x10 if enable_edge else 0)),
        "edge_color": (0.0, 0.0, 0.0, 1.0),
        "edge_size": 1.0 if enable_edge else 0.0,
                "tex": tex_idx, "sph": -1, "sph_mode": 0,
                "toon_flag": 1, "toon": 0, "memo": "",
                "faces": (len(pmx_faces) - sum(m["faces"]
                                               for m in pmx_materials)),
            })
    _l("网格：顶点 %d · 三角面 %d · 材质 %d · 贴图 %d"
       % (len(pmx_verts), len(pmx_faces) // 3, len(pmx_materials),
          len(pmx_textures)))

    # ---- 表情
    # 表情增量 → PMX 偏移 = 该 primitive 的顶点线性变换 + 换轴/缩放。
    # 缺了前一步，蒙皮顶点被绑定矩阵摆正过、增量却没摆正（UniGLTF 系 VRM 的
    # mesh 局部空间），表情会朝错误方向/幅度动。标准 VRM 的 bind 是单位阵、
    # 非蒙皮模型 m 也是 None，都走下面的快路径，输出与从前逐字节一致。
    _ox_s, _oy_s, _oz_s = mx * scale, scale, mz * scale

    def delta_lin(mesh_i, prim_i, lv, d):
        ctx = prim_ctx.get((mesh_i, prim_i))
        if ctx is not None:
            m, bind, jn, wt, hw = ctx
            if m is not None:
                d = _m3_apply(m, d)
            elif bind is not None and hw and jn and wt:
                b00 = b01 = b02 = b10 = b11 = b12 = b20 = b21 = b22 = 0.0
                wsum = 0.0
                for k in range(4):
                    w = wt[lv * 4 + k] if len(wt) > lv * 4 + k else 0.0
                    j = int(jn[lv * 4 + k]) if len(jn) > lv * 4 + k else 0
                    if w > 1e-6 and 0 <= j < len(bind):
                        bm = bind[j]
                        b00 += w * bm[0]; b01 += w * bm[4]; b02 += w * bm[8]
                        b10 += w * bm[1]; b11 += w * bm[5]; b12 += w * bm[9]
                        b20 += w * bm[2]; b21 += w * bm[6]; b22 += w * bm[10]
                        wsum += w
                if wsum > 1e-6:
                    x, y, z = d
                    d = ((b00 * x + b01 * y + b02 * z) / wsum,
                         (b10 * x + b11 * y + b12 * z) / wsum,
                         (b20 * x + b21 * y + b22 * z) / wsum)
        return (d[0] * _ox_s, d[1] * _oy_s, d[2] * _oz_s)

    # 每个 primitive 的 morph target 读成 {local_vertex: (dx,dy,dz)}
    def target_delta(mesh_i, prim_i, slot):
        mesh = _get(gltf, "meshes", mesh_i)
        if mesh is None:
            return {}
        prim = (mesh.get("primitives") or [])[prim_i]
        tg = (prim.get("targets") or [])[slot:slot + 1]
        if not tg:
            return {}
        acc = tg[0].get("POSITION")
        if acc is None:
            return {}
        arr = vrmio.read_accessor(gltf, bin_data, acc)
        out = {}
        for v in range(len(arr) // 3):
            dx, dy, dz = arr[v * 3], arr[v * 3 + 1], arr[v * 3 + 2]
            if abs(dx) + abs(dy) + abs(dz) > 1e-9:
                out[v] = (dx, dy, dz)
        return out

    used_targets = set()
    pmx_morphs = []
    morph_names = set()

    def add_morph(name, deltas):
        """deltas: {全局顶点索引: (dx,dy,dz)}"""
        if not deltas:
            return
        nm = _uniq(name, morph_names)
        offs = []
        for vi in sorted(deltas):
            d = deltas[vi]
            offs.append((vi, (d[0], d[1], d[2])))
        pmx_morphs.append({"name": nm, "name_en": nm, "panel": 1,
                           "kind": 1, "offsets": offs})

    def collect_binds(binds, weight_scale):
        out = {}
        for b in binds or []:
            if "node" in b:                       # VRM 1.0
                node_i = b["node"]
                mesh_i = mesh_of_node.get(node_i)
                slot = b.get("index", 0)
                w = float(b.get("weight", 1.0)) * weight_scale
                for pv in prim_vertex_map:
                    if pv["mesh"] != mesh_i:
                        continue
                    used_targets.add((mesh_i, pv["prim"], slot))
                    for lv, d in target_delta(mesh_i, pv["prim"], slot).items():
                        if lv >= len(pv["l2g"]):
                            continue
                        d = delta_lin(mesh_i, pv["prim"], lv, d)
                        g = pv["l2g"][lv]
                        ox, oy, oz = out.get(g, (0.0, 0.0, 0.0))
                        out[g] = (ox + d[0] * w, oy + d[1] * w, oz + d[2] * w)
            else:                                  # VRM 0.x
                mesh_i = b.get("mesh", 0)
                slot = b.get("index", 0)
                w = float(b.get("weight", 100.0)) * weight_scale
                for pv in prim_vertex_map:
                    if pv["mesh"] != mesh_i:
                        continue
                    used_targets.add((mesh_i, pv["prim"], slot))
                    for lv, d in target_delta(mesh_i, pv["prim"], slot).items():
                        if lv >= len(pv["l2g"]):
                            continue
                        d = delta_lin(mesh_i, pv["prim"], lv, d)
                        g = pv["l2g"][lv]
                        ox, oy, oz = out.get(g, (0.0, 0.0, 0.0))
                        out[g] = (ox + d[0] * w, oy + d[1] * w, oz + d[2] * w)
        return out

    if is1:
        exprs = (vrm1.get("expressions") or {})
        for preset, obj in (exprs.get("preset") or {}).items():
            add_morph(preset, collect_binds(obj.get("morphTargetBinds"), 1.0))
        for cname, obj in (exprs.get("custom") or {}).items():
            add_morph(cname, collect_binds(obj.get("morphTargetBinds"), 1.0))
    elif vrm0:
        for grp in ((vrm0.get("blendShapeMaster") or {})
                    .get("blendShapeGroups") or []):
            add_morph(grp.get("presetName") or grp.get("name") or "morph",
                      collect_binds(grp.get("binds"), 0.01))

    # 没被任何 expression 用到的 target，按 targetNames 补上
    for mesh_i, mesh in enumerate(meshes):
        names = ((mesh.get("extras") or {}).get("targetNames") or [])
        for prim_i, prim in enumerate(mesh.get("primitives") or []):
            for slot in range(len(prim.get("targets") or [])):
                key = (mesh_i, prim_i, slot)
                if key in used_targets:
                    continue
                nm = names[slot] if slot < len(names) else "morph%d" % slot
                d = {}
                for pv in prim_vertex_map:
                    if pv["mesh"] != mesh_i or pv["prim"] != prim_i:
                        continue
                    for lv, dd in target_delta(mesh_i, prim_i, slot).items():
                        if lv < len(pv["l2g"]):
                            g = pv["l2g"][lv]
                            d[g] = delta_lin(mesh_i, prim_i, lv, dd)
                add_morph(nm, d)
    if pmx_morphs:
        _l("表情：%d 个（已转成 PMX 顶点表情）" % len(pmx_morphs))

    # ---- 表示枠
    frames = []
    if pmx_bones:
        # items: (0=骨骼, 1=表情)
        frames.append({"name": "Root", "name_en": "Root", "special": 0,
                       "items": [(0, i) for i in range(len(pmx_bones))]})
    if pmx_morphs:
        frames.append({"name": "表情", "name_en": "Exp", "special": 1,
                       "items": [(1, i) for i in range(len(pmx_morphs))]})

    # ---- 模型信息
    meta = (vrm1 or vrm0 or {}).get("meta") or {}
    if is1:
        title = meta.get("name") or ""
        author = ", ".join(meta.get("authors") or [])
        license_txt = meta.get("licenseUrl") or ""
    else:
        title = meta.get("title") or ""
        author = meta.get("author") or ""
        license_txt = meta.get("otherLicenseUrl") or meta.get("licenseName") or ""
    comment = []
    if title:
        comment.append("VRM: %s" % title)
    if author:
        comment.append("author: %s" % author)
    if license_txt:
        comment.append("license: %s" % license_txt)
    comment.append("converted by vrm2pmx.py")

    model = pmxio.new_model(name or title or stem or "Model")
    model["name"] = name or title or stem or "Model"
    model["name_en"] = model["name"]
    model["comment"] = "\r\n".join(comment)
    model["comment_en"] = model["comment"]
    model["vertices"] = pmx_verts
    model["faces"] = pmx_faces
    model["textures"] = pmx_textures
    model["materials"] = pmx_materials
    model["bones"] = pmx_bones
    model["morphs"] = pmx_morphs
    model["frames"] = frames

    if not enable_edge:
        pmxio.strip_edges(model, force_double_sided=False)
    size = pmxio.write_pmx(model, pmx_path)
    _l("已写出 %s（%.2f MB）· 骨骼 %d · 材质 %d"
       % (os.path.basename(pmx_path), size / 1048576.0, len(pmx_bones),
          len(pmx_materials)), "ok")
    return {"vertices": len(pmx_verts), "tris": len(pmx_faces) // 3,
            "bones": len(pmx_bones), "materials": len(pmx_materials),
            "morphs": len(pmx_morphs), "bytes": size, "scale": scale}


def main(argv=None):
    ap = argparse.ArgumentParser(description="VRM → PMX（纯 Python）")
    ap.add_argument("vrm")
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--scale", default="auto", help="auto（归一到 20 单位）或具体倍率")
    ap.add_argument("--rotate", default="auto", choices=["auto", "none", "y180"])
    ap.add_argument("--flip-winding", action="store_true",
                    help="强制反转三角形缠绕顺序（默认自动判定）")
    ap.add_argument("--edge", action="store_true",
                    help="给材质开启 MMD 轮廓线（默认关闭）")
    ap.add_argument("--force-double-sided", action="store_true",
                    help="所有材质强制双面（出现镂空时的兜底手段）")
    ap.add_argument("--name", default=None)
    a = ap.parse_args(argv)
    out = a.out or (os.path.splitext(a.vrm)[0] + ".pmx")
    st = convert(a.vrm, out, scale_mode=a.scale, rotate=a.rotate,
                 reverse_winding=(True if a.flip_winding else "auto"),
                 name=a.name, enable_edge=a.edge,
                 force_double_sided=a.force_double_sided,
                 log=lambda m, t=None: print(m))
    print("完成：%s（%.2f MB）" % (out, st["bytes"] / 1048576.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
