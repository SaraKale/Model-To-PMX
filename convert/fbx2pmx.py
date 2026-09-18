"""fbx2pmx.py - convert a binary FBX (7.x) into a PMX 2.0 model.

Standalone: no Autodesk FBX SDK, no Blender, no mmd_tools.

What it maps:
    Geometry  -> PMX vertices + triangles
    Model(Mesh)  material link -> PMX materials (one draw segment per mesh)
    Deformer/Skin + Cluster    -> PMX bone weights (BDEF1/2/4)
    Model(LimbNode) hierarchy  -> PMX bones
    LayerElementUV / Normal    -> PMX UV0 / vertex normals
    Deformer/BlendShape chain  -> PMX vertex morphs (表情)
        Geometry -> BlendShape -> BlendShapeChannel -> Shape, where each Shape
        holds Indexes (affected control points) + Vertices (relative position
        deltas).  The morph panel (眉/目/口/その他) is inferred from the channel
        name; every morph is written as PMX kind=1 (頂点).

What it does NOT and cannot invent: physics rigid bodies / joints.  Texture
references ARE extracted when the FBX links them via Texture/Video nodes; the
actual image files are copied into ``<out>/textures/`` only if they can be found
on disk (the FBX often stores an absolute game path).

Usage:
    python fbx2pmx.py "model.fbx" -o "model.pmx"
    python fbx2pmx.py "model.fbx" --info
    python fbx2pmx.py "model.fbx" --no-morphs     # skip blend shapes
    python fbx2pmx.py "model.fbx" --remove-alpha  # drop the alpha channel of textures

Texture alpha: game FBX textures almost always carry an alpha channel.  Pass
``--remove-alpha`` to write the copied images as opaque RGB (alpha dropped),
which avoids MMD showing unwanted transparency on clothes/hair.  Stripping is
done with Pillow when available, otherwise with a small built-in PNG/TGA
reader so the feature still works in a pure-stdlib environment.
"""
import argparse
import math
import os
import shutil
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fbx_reader as fbx


# ------------------------------------------------------------------ math ----

def m_ident():
    return [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0]


def m_mul(a, b):
    o = [0.0] * 16
    for i in range(4):
        ai = i * 4
        for j in range(4):
            o[ai + j] = (a[ai] * b[j] + a[ai + 1] * b[4 + j] +
                         a[ai + 2] * b[8 + j] + a[ai + 3] * b[12 + j])
    return o


def m_point(m, p):
    x, y, z = p
    return (m[0] * x + m[1] * y + m[2] * z + m[3],
            m[4] * x + m[5] * y + m[6] * z + m[7],
            m[8] * x + m[9] * y + m[10] * z + m[11])


def m_dir(m, p):
    x, y, z = p
    return (m[0] * x + m[1] * y + m[2] * z,
            m[4] * x + m[5] * y + m[6] * z,
            m[8] * x + m[9] * y + m[10] * z)


def m_translation(m):
    return (m[3], m[7], m[11])


def m_trs(t, rot_deg, s):
    rx, ry, rz = (math.radians(v) for v in rot_deg)
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)
    rx_m = [1, 0, 0, 0, 0, cx, -sx, 0, 0, sx, cx, 0, 0, 0, 0, 1]
    ry_m = [cy, 0, sy, 0, 0, 1, 0, 0, -sy, 0, cy, 0, 0, 0, 0, 1]
    rz_m = [cz, -sz, 0, 0, sz, cz, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
    r = m_mul(rz_m, m_mul(ry_m, rx_m))
    for i in range(3):
        for j in range(3):
            r[i * 4 + j] *= s[j]
    r[3], r[7], r[11] = t
    return r


def parse_matrix_str(s):
    vals = [float(x) for x in s.strip().strip("[]").split(",")]
    return vals if len(vals) == 16 else m_ident()


def m_rot(rot_deg):
    """欧拉角（度）→ 纯旋转矩阵，与 m_trs 同一套约定（行向量、Rz·Ry·Rx）。

    注意不要改成 Rx·Ry·Rz：实测同一份 FBX 下两种顺序的贴合度差距极大
    （Rz·Ry·Rx 中位 5.67cm vs Rx·Ry·Rz 中位 18.09cm），当前这个才是对的。
    """
    return m_trs([0.0, 0.0, 0.0], rot_deg, [1.0, 1.0, 1.0])


def m_rot_inv(rot_deg):
    """纯旋转矩阵的逆（3×3 转置）。FBX 里的 Rpost⁻¹ / Rp⁻¹ 需要它。"""
    r = m_rot(rot_deg)
    o = list(r)
    for i in range(3):
        for j in range(3):
            o[j * 4 + i] = r[i * 4 + j]
    return o


def m_translate(v):
    m = m_ident()
    m[3], m[7], m[11] = v
    return m


def m_scale(s):
    m = m_ident()
    m[0], m[5], m[10] = s
    return m


# ------------------------------------------------------------- fbx scene ----

class Model:
    """A FBX Model node (mesh, bone, root ...)."""

    __slots__ = ("id", "name", "cls", "local", "world", "parent", "children")

    def __init__(self, nid, name, cls):
        self.id = nid
        self.name = name
        self.cls = cls
        self.local = m_ident()
        self.world = m_ident()
        self.parent = None
        self.children = []


def _clean_name(s):
    return s.split("\x00")[0]


def _ns_strip(s):
    """剥掉 FBX 的 `Class::name` 命名空间前缀。

    Autodesk 自家的导出器一律写 `"SubDeformer::Smile"` / `"Shape::Smile"`，
    而 FBX SDK 的 GetName() 会自动剥前缀、Blender 也会剥；本工程的解析器是
    直接读字段原文，所以这里补一次，免得表情名/材质名带一截 `Shape::`。
    """
    return s.split("::")[-1] if "::" in s else s


class Scene:
    def __init__(self, path):
        self.path = path
        self.ver, top = fbx.load(path)
        root = fbx.FbxNode("root", [], top)
        objs = root.get("Objects")
        self.objects = objs.children
        self.byid = {}
        for c in self.objects:
            if c.props and isinstance(c.props[0], int):
                self.byid[c.props[0]] = c

        # connections:  ['OO', child_id, parent_id]
        self.parents = {}
        for c in root.get("Connections").children:
            if len(c.props) >= 3:
                self.parents.setdefault(c.props[1], []).append(c.props[2])

        self._build_models()
        self._resolve_links()
        self._build_textures()
        self._build_morphs()

    # -- helpers
    def _props70(self, node):
        out = {}
        p70 = node.get("Properties70")
        if p70:
            for pr in p70.children:
                if len(pr.props) >= 5:
                    out[pr.props[0]] = pr.props[4:]
        return out

    def _local_matrix(self, node):
        """节点局部矩阵，按 FBX SDK 的 EvaluateLocalTransform 完整合成：

            T · Roff · Rp · Rpre · R · Rpost⁻¹ · Rp⁻¹ · Soff · Sp · S · Sp⁻¹

        这里最容易漏掉的是 **PreRotation**，而且漏了不会立刻露馅：节点自身的
        平移在这个式子最左边，不受自己 PreRotation 影响，所以单看一根骨骼的
        位置是正常的；但它的**所有后代**都活在少了一个 Rpre 的坐标系里，整条
        骨链会歪掉/翻转。实测 "Crimson Rumor" 这个包 385 个节点里有 185 个带
        非零 PreRotation（不少正好是 180°），漏掉它会让 83% 的骨骼偏位（最大
        106cm）——衣服/发饰的骨链会从"垂在身侧"变成"横着翘到体外"，看起来就像
        骨骼被倒过来了。
        判据（顶点到其主骨骼线段的距离，越小越贴合）：
            只 T·R·S：中位 11.99cm / 平均 20.92cm
            带 PreRotation：中位  5.67cm / 平均  6.12cm
        """
        d = self._props70(node)

        def v3(key):
            v = d.get(key)
            return [float(x) for x in v[:3]] if v else None

        t = v3("Lcl Translation") or [0.0, 0.0, 0.0]
        r = v3("Lcl Rotation") or [0.0, 0.0, 0.0]
        s = v3("Lcl Scaling") or [1.0, 1.0, 1.0]
        rp = v3("RotationPivot")
        pre = v3("PreRotation")
        post = v3("PostRotation")
        roff = v3("RotationOffset")
        sp = v3("ScalingPivot")
        soff = v3("ScalingOffset")

        m = m_translate(t)
        if roff:
            m = m_mul(m, m_translate(roff))
        if rp:
            m = m_mul(m, m_rot(rp))
        if pre:
            m = m_mul(m, m_rot(pre))
        m = m_mul(m, m_rot(r))
        if post:
            m = m_mul(m, m_rot_inv(post))
        if rp:
            m = m_mul(m, m_rot_inv(rp))
        if soff:
            m = m_mul(m, m_translate(soff))
        if sp:
            m = m_mul(m, m_rot(sp))
        m = m_mul(m, m_scale(s))
        if sp:
            m = m_mul(m, m_rot_inv(sp))
        return m

    def _build_models(self):
        self.models = {}
        for c in self.objects:
            if c.name != "Model":
                continue
            nid = c.props[0]
            m = Model(nid, _clean_name(c.props[1]), c.props[2])
            m.local = self._local_matrix(c)
            self.models[nid] = m
        for nid, m in self.models.items():
            for pid in self.parents.get(nid, []):
                p = self.models.get(pid)
                if p is not None and m.parent is None:
                    m.parent = p
                    p.children.append(m)
        for m in self.models.values():
            if m.parent is None:
                self._calc_world(m, m_ident())

    def _calc_world(self, m, parent_world):
        m.world = m_mul(parent_world, m.local)
        for c in m.children:
            self._calc_world(c, m.world)

    def _resolve_links(self):
        """child_id -> parent_id lists turned into typed dictionaries."""
        self.geometry_model = {}
        self.model_material = {}        # model_id -> first material id (legacy)
        self.model_materials = {}       # model_id -> [material id, ...] (ordered)
        self.geometry_materials = {}    # geometry_id -> [material id, ...]
        self.geometry_skin = {}
        self.cluster_skin = {}
        self.cluster_bone = {}
        self.texture_materials = {}     # material_id -> [texture id, ...] (ordered)
        self.texture_video = {}         # texture_id -> video_id (embedded name)

        for child, plist in self.parents.items():
            cn = self.byid.get(child)
            if cn is None:
                continue
            for pid in plist:
                pn = self.byid.get(pid)
                if pn is None:
                    continue
                if cn.name == "Geometry" and pn.name == "Model":
                    self.geometry_model[child] = pid
                elif cn.name == "Material" and pn.name == "Model":
                    self.model_material[pid] = child
                    self.model_materials.setdefault(pid, []).append(child)
                elif cn.name == "Material" and pn.name == "Geometry":
                    self.geometry_materials.setdefault(pid, []).append(child)
                elif cn.name == "Deformer" and pn.name == "Geometry":
                    if len(cn.props) > 2 and cn.props[2] == "Skin":
                        self.geometry_skin[pid] = child
                elif cn.name == "Deformer" and pn.name == "Deformer":
                    if len(cn.props) > 2 and cn.props[2] == "Cluster":
                        self.cluster_skin[child] = pid
                elif cn.name == "Deformer" and pn.name == "Model":
                    # FBX SDK / Blender exporter: C:"OO", <Cluster>, <Node(bone)>
                    if len(cn.props) > 2 and cn.props[2] == "Cluster":
                        self.cluster_bone[child] = pid
                elif cn.name == "Model" and pn.name == "Deformer":
                    # a few exporters reverse it: C:"OO", <Node(bone)>, <Cluster>
                    if len(pn.props) > 2 and pn.props[2] == "Cluster":
                        self.cluster_bone[pid] = child
                elif cn.name == "Texture" and pn.name == "Material":
                    self.texture_materials.setdefault(pid, []).append(child)
                elif cn.name == "Material" and pn.name == "Texture":
                    self.texture_materials.setdefault(cn.props[0], []).append(pid)
                elif cn.name == "Texture" and pn.name == "Video":
                    self.texture_video[child] = pid
                elif cn.name == "Video" and pn.name == "Texture":
                    self.texture_video[pid] = child


# ----------------------------------------------------------------- pmx ------

    def _geom_matrix(self, node):
        """Geometric transform of a Model node (applied to geometry, not children)."""
        d = self._props70(node)
        t = d.get("GeometricTranslation", [0.0, 0.0, 0.0])
        r = d.get("GeometricRotation", [0.0, 0.0, 0.0])
        s = d.get("GeometricScaling", [1.0, 1.0, 1.0])
        return m_trs([float(x) for x in t[:3]],
                     [float(x) for x in r[:3]],
                     [float(x) for x in s[:3]])

    def _build_textures(self):
        """Collect Texture nodes and their file names.

        FBX stores the image either as an embedded Video (rare here) or, as in
        game exports, as an *external* file referenced by RelativeFilename /
        FileName on the Texture (or its linked Video) node.  We only need the
        basename - the actual file is copied at convert() time if it exists.
        """
        self.textures = {}          # texture_id -> {"name": basename, "rel": path}
        for c in self.objects:
            if c.name != "Texture":
                continue
            tid = c.props[0]
            d = self._props70(c)
            rel = None
            for key in ("RelativeFilename", "FileName"):
                v = d.get(key)
                if v:
                    rel = v[0] if isinstance(v, list) else v
                    break
            if not rel:
                vid = self.texture_video.get(tid)
                if vid is not None:
                    vn = self.byid.get(vid)
                    if vn is not None:
                        dv = self._props70(vn)
                        for key in ("RelativeFilename", "FileName"):
                            v = dv.get(key)
                            if v:
                                rel = v[0] if isinstance(v, list) else v
                                break
            name = os.path.basename(rel) if rel else _clean_name(c.props[1])
            name = name.split("\x00")[0]
            self.textures[tid] = {"name": name, "rel": rel}

    def _build_morphs(self):
        """Collect FBX blend shapes into morph descriptors.

        FBX chain: Geometry -> BlendShape -> BlendShapeChannel -> Shape.
        Each Shape carries ``Indexes`` (the affected control points) and
        ``Vertices`` (relative position *deltas*, 3 floats per control point,
        in the same order as Indexes).  We verified against this file that the
        stored values are deltas (not absolute positions): the base eyebrow
        vertex sits at y~1.5 while a morph target lists y~-0.004, so subtracting
        nothing yields the intended small displacement.

        A morph is keyed by the owning geometry id so convert() can apply the
        same mesh transform used for the base vertices.
        """
        self.morphs = []          # [{name, geo_id, indexes, deltas}]
        bs_geo = {}               # blendshape_id -> geometry_id
        channels = {}             # blendshape_id -> [channel_id]
        chan_shape = {}           # channel_id -> shape_node_id

        for child, plist in self.parents.items():
            cn = self.byid.get(child)
            if cn is None:
                continue
            cls = (cn.props[2] if (cn.name in ("Deformer", "Geometry")
                                   and len(cn.props) > 2) else "")
            if cn.name == "Deformer" and cls == "BlendShape":
                for pid in plist:
                    if pid in self.geometry_model:   # only real mesh geometries
                        bs_geo[child] = pid
            elif cn.name == "Deformer" and cls == "BlendShapeChannel":
                for pid in plist:
                    pn = self.byid.get(pid)
                    if pn is not None and len(pn.props) > 2 \
                            and pn.props[2] == "BlendShape":
                        channels.setdefault(pid, []).append(child)
            elif cn.name == "Geometry" and cls == "Shape":
                for pid in plist:
                    pn = self.byid.get(pid)
                    if pn is not None and len(pn.props) > 2 \
                            and pn.props[2] == "BlendShapeChannel":
                        chan_shape[pid] = child      # channel_id -> shape_id

        for bsid, gid in bs_geo.items():
            for chid in channels.get(bsid, []):
                shid = chan_shape.get(chid)
                if shid is None:
                    continue
                sh = self.byid.get(shid)
                if sh is None:
                    continue
                idxn = sh.get("Indexes")
                vn = sh.get("Vertices")
                if idxn is None or vn is None:
                    continue
                indexes = idxn.props[0]
                deltas = vn.props[0]
                if not indexes or len(deltas) < 3 * len(indexes):
                    continue
                name = _ns_strip(_clean_name(sh.props[1])) \
                    or _ns_strip(_clean_name(self.byid[chid].props[1]))
                self.morphs.append({"name": name, "geo_id": gid,
                                    "indexes": indexes, "deltas": deltas})


class PmxWriter:
    #: 文本编码：0 = UTF-16LE（MMD 只认这一种），1 = UTF-8
    ENC = 0

    def __init__(self, indices=(2, 1, 2, 2, 1, 1)):
        self.buf = bytearray()
        self.vi, self.ti, self.mi, self.bi, self.moi, self.ri = indices

    # -- primitives
    def i32(self, v):
        self.buf += struct.pack("<i", v)

    def f32(self, v):
        self.buf += struct.pack("<f", v)

    def text(self, s):
        # MMD 不支持 UTF-8 的 PMX（原版提示：MMDではエンコード方式がUTF16の
        # PMXファイルしか読み込めません），所以这里必须写 UTF-16LE。
        b = s.encode("utf-16-le" if self.ENC == 0 else "utf-8")
        self.i32(len(b))
        self.buf += b

    def idx(self, v, size):
        if size == 1:
            self.buf += struct.pack("<b", v)
        elif size == 2:
            self.buf += struct.pack("<h", v)
        else:
            self.i32(v)

    def uidx(self, v, size):
        if size == 1:
            self.buf += struct.pack("<B", v)
        elif size == 2:
            self.buf += struct.pack("<H", v)
        else:
            self.buf += struct.pack("<I", v)

    def header(self, name, name_en, comment, comment_en):
        self.buf += b"PMX "
        self.f32(2.0)
        self.buf += bytes([8])
        # globals: [文本编码, 追加UV, 顶点/贴图/材质/骨骼/表情/刚体索引宽度]
        self.buf += bytes([self.ENC, 0, self.vi, self.ti, self.mi, self.bi,
                           self.moi, self.ri])
        self.text(name)
        self.text(name_en)
        self.text(comment)
        self.text(comment_en)


def pick_index_size(count, signed=True):
    if signed:
        if count <= 127:
            return 1
        if count <= 32767:
            return 2
        return 4
    if count <= 255:
        return 1
    if count <= 65535:
        return 2
    return 4


# ------------------------------------------------------------- converter ----

def _cp_index(pvi, i):
    """FBX PolygonVertexIndex -> actual control point index (last index is negative)."""
    v = pvi[i]
    return ~v if v < 0 else v


def sample_uv(node, pvi):
    """Return a per-corner list of (u, v) indexed by polygon-vertex position."""
    if node is None or pvi is None:
        return None
    uv = node.get("UV")
    if uv is None:
        return None
    table = uv.props[0]
    idn = node.get("UVIndex")
    mir = node.get("MappingInformationType")
    rir = node.get("ReferenceInformationType")
    mir_s = mir.props[0] if mir is not None else ""
    rir_s = rir.props[0] if rir is not None else ""
    n = len(pvi)
    if mir_s == "ByPolygonVertex" and rir_s == "IndexToDirect" and idn is not None:
        ids = idn.props[0]
        return [(table[i * 2], table[i * 2 + 1]) for i in ids]
    if mir_s == "ByPolygonVertex" and (rir_s == "Direct" or idn is None):
        return [(table[i * 2], table[i * 2 + 1]) for i in range(n)]
    # ByControlPoint / ByVertice: the per-corner UV comes from the control point of that corner
    if idn is not None and rir_s == "IndexToDirect":
        ids = idn.props[0]
        return [(table[ids[_cp_index(pvi, i)] * 2], table[ids[_cp_index(pvi, i)] * 2 + 1])
                for i in range(n)]
    return [(table[_cp_index(pvi, i) * 2], table[_cp_index(pvi, i) * 2 + 1])
            for i in range(n)]


def sample_normals(node, pvi):
    """Return a per-corner list of (nx, ny, nz) indexed by polygon-vertex position."""
    if node is None or pvi is None:
        return None
    nrm = node.get("Normals")
    if nrm is None:
        return None
    table = nrm.props[0]
    idn = node.get("NormalsIndex")
    mir = node.get("MappingInformationType")
    rir = node.get("ReferenceInformationType")
    mir_s = mir.props[0] if mir is not None else ""
    rir_s = rir.props[0] if rir is not None else ""
    n = len(pvi)
    if mir_s == "ByPolygonVertex" and idn is not None:
        ids = idn.props[0]
        return [(table[i * 3], table[i * 3 + 1], table[i * 3 + 2]) for i in ids]
    if mir_s == "ByPolygonVertex" and rir_s == "Direct":
        return [(table[i * 3], table[i * 3 + 1], table[i * 3 + 2]) for i in range(n)]
    if idn is not None and rir_s == "IndexToDirect":
        ids = idn.props[0]
        return [(table[ids[_cp_index(pvi, i)] * 3], table[ids[_cp_index(pvi, i)] * 3 + 1],
                 table[ids[_cp_index(pvi, i)] * 3 + 2]) for i in range(n)]
    return [(table[_cp_index(pvi, i) * 3], table[_cp_index(pvi, i) * 3 + 1],
             table[_cp_index(pvi, i) * 3 + 2]) for i in range(n)]


def triangulate(pvi):
    """FBX polygon list -> list of ((control_point, corner_position), x3).

    The second element matters: LayerElementUV / LayerElementNormal use
    ByPolygonVertex mapping, so their lookup index is the position inside the
    PolygonVertexIndex array, not the control-point index.
    """
    tris = []
    cur = []
    for pos, v in enumerate(pvi):
        idx = ~v if v < 0 else v
        cur.append((idx, pos))
        if v < 0:
            for i in range(1, len(cur) - 1):
                tris.append((cur[0], cur[i], cur[i + 1]))
            cur = []
    return tris


def polygon_starts(pvi):
    """Index into the pvi array where each polygon begins (for per-poly data)."""
    starts = []
    start = 0
    for i, v in enumerate(pvi):
        if v < 0:
            starts.append(start)
            start = i + 1
    return starts


def _mesh_materials(g, model, scene):
    """Ordered list of material node ids used by this geometry/model.

    FBX stores the material assignment either as Connections from the material
    to the model (or to the geometry), and optionally refines it per-polygon via
    a LayerElementMaterial.  The per-polygon indices reference this ordered list.
    """
    mats = list(scene.model_materials.get(model.id, []))
    if not mats:
        mats = list(scene.geometry_materials.get(g.props[0], []))
    if not mats:
        m = scene.model_material.get(model.id)
        if m is not None:
            mats = [m]
    if not mats:
        mats = [None]
    return mats


def _poly_material_index(g, pvi, nmats):
    """Per-triangle material index, or None if there is no LayerElementMaterial."""
    le = g.get("LayerElementMaterial")
    if le is None:
        return None
    midx = le.get("Materials")
    if midx is None:
        return None
    ids = midx.props[0]
    mir = le.get("MappingInformationType")
    rir = le.get("ReferenceInformationType")
    mir_s = mir.props[0] if mir is not None else ""
    rir_s = rir.props[0] if rir is not None else ""
    poly_lens = []
    cur = 0
    for v in pvi:
        cur += 1
        if v < 0:
            poly_lens.append(cur)
            cur = 0
    tri_mat = []
    if mir_s == "ByPolygon":
        for pi, pl in enumerate(poly_lens):
            m = ids[pi] if pi < len(ids) else 0
            for _ in range(max(0, pl - 2)):
                tri_mat.append(m)
    elif mir_s == "ByPolygonVertex":
        corner = 0
        for pl in poly_lens:
            m = ids[corner] if corner < len(ids) else 0
            for _ in range(max(0, pl - 2)):
                tri_mat.append(m)
            corner += pl
    else:
        return None
    nmats = max(1, nmats)
    return [max(0, min(nmats - 1, m)) for m in tri_mat]


def _find_file(root, basename, limit=60000):
    """Bounded recursive search for a file by basename under root (returns path)."""
    count = 0
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = os.scandir(d)
        except OSError:
            continue
        dirs = []
        for e in entries:
            count += 1
            if count > limit:
                return None
            if e.is_dir():
                dirs.append(e.path)
            elif e.name == basename:
                return e.path
        stack.extend(dirs)
    return None


# ----------------------------------------------------------------- alpha ----
# Remove the alpha channel from a texture image.  Returns one of:
#   "stripped"  - had alpha, now written as opaque RGB
#   "no_alpha"  - no alpha (copied verbatim)
#   False       - decode/encode failed; the caller copies the file as-is
# Pillow is preferred; a tiny built-in PNG/TGA reader is the fallback so the
# feature works even when Pillow is not installed (the GUI runs on plain Python).

def _strip_alpha(src, dst):
    """Try Pillow first; fall back to the pure-stdlib reader."""
    try:
        from PIL import Image
        im = Image.open(src)
        has_alpha = (im.mode in ("RGBA", "LA", "PA")
                     or (im.mode == "P" and "transparency" in im.info))
        if has_alpha:
            im.convert("RGB").save(dst)
            return "stripped"
        shutil.copyfile(src, dst)
        return "no_alpha"
    except ImportError:
        pass
    except Exception:
        return False
    return _strip_alpha_pure(src, dst)


def _strip_alpha_pure(src, dst):
    """Pure-stdlib alpha strip for PNG and TGA (the formats MMD actually uses)."""
    try:
        with open(src, "rb") as f:
            head = f.read(18)
    except OSError:
        return False
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return _strip_png_alpha(src, dst)
    # TGA has no magic; recognise it by a plausible header.
    if len(head) >= 18 and head[2] in (1, 2, 3, 9, 10, 11) \
            and head[16] in (16, 24, 32):
        return _strip_tga_alpha(src, dst)
    # Unknown / unsupported format: keep the original, just relocate it.
    try:
        shutil.copyfile(src, dst)
        return "no_alpha"
    except OSError:
        return False


def _png_chunk(typ, data):
    import zlib
    return (struct.pack(">I", len(data)) + typ + data
            + struct.pack(">I", zlib.crc32(typ + data) & 0xffffffff))


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _png_unfilter(ftype, line, prev, bpp):
    """Undo a PNG scanline filter in place (assumes 8-bit)."""
    if ftype == 0:
        return
    n = len(line)
    for i in range(n):
        a = line[i - bpp] if i >= bpp else 0
        b = prev[i]
        c = prev[i - bpp] if i >= bpp else 0
        if ftype == 1:
            line[i] = (line[i] + a) & 0xff
        elif ftype == 2:
            line[i] = (line[i] + b) & 0xff
        elif ftype == 3:
            line[i] = (line[i] + ((a + b) >> 1)) & 0xff
        elif ftype == 4:
            line[i] = (line[i] + _paeth(a, b, c)) & 0xff


def _strip_png_alpha(src, dst):
    import zlib
    try:
        with open(src, "rb") as f:
            data = f.read()
    except OSError:
        return False
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return False
    pos = 8
    width = height = bit_depth = color_type = interlace = None
    idat = bytearray()
    plte = None
    other = []
    while pos + 8 <= len(data):
        ln = struct.unpack(">I", data[pos:pos + 4])[0]
        typ = data[pos + 4:pos + 8]
        cdata = data[pos + 8:pos + 8 + ln]
        if typ == b"IHDR" and len(cdata) >= 13:
            width, height, bit_depth, color_type, _comp, _filt, interlace = \
                struct.unpack(">IIBBBBB", cdata[:13])
        elif typ == b"IDAT":
            idat += cdata
        elif typ == b"PLTE":
            plte = cdata
        elif typ == b"tRNS":
            pass  # drop transparency chunk
        elif typ == b"IEND":
            pass  # we append our own IEND last
        else:
            other.append((typ, cdata))
        pos += 12 + ln
    if width is None or interlace or bit_depth != 8 \
            or color_type not in (0, 2, 3, 4, 6):
        try:
            shutil.copyfile(src, dst)
        except OSError:
            return False
        return "no_alpha"
    raw = zlib.decompress(bytes(idat))
    ch_in = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
    stride = width * ch_in
    prev = bytearray(stride)
    out = bytearray()
    p = 0
    for _ in range(height):
        ftype = raw[p]
        p += 1
        line = bytearray(raw[p:p + stride])
        p += stride
        _png_unfilter(ftype, line, prev, ch_in)
        out += line
        prev = line
    if color_type == 6:
        new_ct, ch_out = 2, 3
        new = bytearray()
        for y in range(height):
            base = y * stride
            for x in range(width):
                i = base + x * 4
                new += bytes((out[i], out[i + 1], out[i + 2]))
    elif color_type == 4:
        new_ct, ch_out = 0, 1
        new = bytearray()
        for y in range(height):
            base = y * stride
            for x in range(width):
                new += bytes((out[base + x * 2],))
    elif color_type == 3:
        if plte is None:
            try:
                shutil.copyfile(src, dst)
            except OSError:
                return False
            return "no_alpha"
        new_ct, ch_out = 2, 3
        pal = {i: (plte[i * 3], plte[i * 3 + 1], plte[i * 3 + 2])
               for i in range(len(plte) // 3)}
        new = bytearray()
        for y in range(height):
            base = y * stride
            for x in range(width):
                r, g, b = pal.get(out[base + x], (0, 0, 0))
                new += bytes((r, g, b))
    else:  # 0 or 2: already opaque
        try:
            shutil.copyfile(src, dst)
        except OSError:
            return False
        return "no_alpha"
    nstride = width * ch_out
    new_raw = bytearray()
    for y in range(height):
        new_raw.append(0)
        new_raw += new[y * nstride:(y + 1) * nstride]
    comp = zlib.compress(bytes(new_raw), 9)
    buf = bytearray(b"\x89PNG\r\n\x1a\n")
    buf += _png_chunk(b"IHDR",
                      struct.pack(">IIBBBBB", width, height, 8, new_ct, 0, 0, 0))
    for typ, cdata in other:
        buf += _png_chunk(typ, cdata)
    buf += _png_chunk(b"IDAT", comp)
    buf += _png_chunk(b"IEND", b"")
    try:
        with open(dst, "wb") as f:
            f.write(bytes(buf))
    except OSError:
        return False
    return "stripped"


def _tga_decode_rle(pix, width, height, depth):
    bpp = depth // 8
    out = bytearray()
    count = width * height
    i, n = 0, len(pix)
    while len(out) < count * bpp and i < n:
        h = pix[i]
        i += 1
        run = (h & 0x7f) + 1
        if h & 0x80:
            chunk = pix[i:i + bpp]
            i += bpp
            for _ in range(run):
                out += chunk
        else:
            for _ in range(run):
                out += pix[i:i + bpp]
                i += bpp
    return out


def _strip_tga_alpha(src, dst):
    import struct as _s
    try:
        with open(src, "rb") as f:
            data = f.read()
    except OSError:
        return False
    if len(data) < 18:
        return False
    id_len = data[0]
    cmap_type = data[1]
    img_type = data[2]
    if img_type not in (1, 2, 3, 9, 10, 11) or data[16] not in (16, 24, 32):
        try:
            shutil.copyfile(src, dst)
        except OSError:
            return False
        return "no_alpha"
    width, height = _s.unpack("<HH", data[12:16])
    depth = data[16]
    desc = data[17]
    if width <= 0 or height <= 0 or width > 8192 or height > 8192:
        try:
            shutil.copyfile(src, dst)
        except OSError:
            return False
        return "no_alpha"
    cmap_len = _s.unpack("<H", data[3:5])[0]
    cmap_entry = data[7]
    off = 18 + id_len
    if cmap_type:
        off += cmap_len * (cmap_entry // 8)
    pix = bytearray(data[off:])
    if img_type in (9, 10, 11):
        pix = _tga_decode_rle(pix, width, height, depth)
    bpp_in = depth // 8
    out = bytearray()
    n = width * height
    had_alpha = depth in (16, 32)
    for i in range(n):
        base = i * bpp_in
        if depth == 32:
            b, g, r, _a = pix[base], pix[base + 1], pix[base + 2], pix[base + 3]
        elif depth == 24:
            b, g, r = pix[base], pix[base + 1], pix[base + 2]
        else:  # 16-bit 1555/555 -> drop the alpha bit, scale to 8-bit
            v = pix[base] | (pix[base + 1] << 8)
            r = (v >> 10) & 0x1f
            g = (v >> 5) & 0x1f
            b = v & 0x1f
            r = (r * 255 + 15) // 31
            g = (g * 255 + 15) // 31
            b = (b * 255 + 15) // 31
        out += bytes((b, g, r))
    hdr = bytearray(18)
    hdr[2] = 2  # uncompressed truecolour
    hdr[12:14] = _s.pack("<H", width)
    hdr[14:16] = _s.pack("<H", height)
    hdr[16] = 24
    hdr[17] = desc  # preserve top/bottom origin bit
    try:
        with open(dst, "wb") as f:
            f.write(bytes(hdr))
            f.write(bytes(out))
    except OSError:
        return False
    return "stripped" if had_alpha else "no_alpha"


def _copy_textures(tex_list, fbx_path, out_dir, verbose, remove_alpha=False):
    """Locate each texture image on disk and copy it into ``out_dir/textures/``.

    Returns the set of basenames that were copied.  Files referenced by the FBX
    but missing locally (the FBX usually stores an absolute game path like
    ``G:\\...``) are simply skipped - the PMX still references them so the user
    can drop the images into ``textures/`` later.
    """
    if not tex_list:
        return set()
    import shutil
    fbx_dir = os.path.dirname(os.path.abspath(fbx_path))
    parent = os.path.dirname(fbx_dir)
    search_dirs = [fbx_dir,
                   os.path.join(fbx_dir, "textures"),
                   os.path.join(fbx_dir, "tex"),
                   parent]
    found = {}
    for bn in tex_list:
        for d in search_dirs:
            if not d:
                continue
            p = os.path.join(d, bn)
            if os.path.isfile(p):
                found[bn] = p
                break
        if bn in found:
            continue
        for d in (fbx_dir, parent):
            if not d or bn in found:
                continue
            hit = _find_file(d, bn)
            if hit:
                found[bn] = hit
                break
    copied = set()
    alpha_stripped = 0
    if found:
        tdir = os.path.join(out_dir, "textures")
        os.makedirs(tdir, exist_ok=True)
        for bn, src in found.items():
            dst = os.path.join(tdir, bn)
            if remove_alpha:
                res = _strip_alpha(src, dst)
                if res == "stripped":
                    alpha_stripped += 1
                elif res is False:
                    try:
                        shutil.copyfile(src, dst)
                    except OSError:
                        continue
                copied.add(bn)
                continue
            try:
                shutil.copyfile(src, os.path.join(tdir, bn))
                copied.add(bn)
            except OSError:
                pass
    if verbose and tex_list:
        if copied:
            print("  textures: copied %d / %d image file(s) into textures/"
                  % (len(copied), len(tex_list)))
        if remove_alpha:
            print("  textures: 去除透明通道 %d 张（已成不透明 RGB）"
                  % alpha_stripped)
        if len(copied) < len(tex_list):
            print("  textures: %d referenced but not found on disk "
                  "(drop them into textures/ later)"
                  % (len(tex_list) - len(copied)))
    return copied


def convert(scene, out_path, scale_mode="mmd", flip_z=True, verbose=True, name=None,
            center=True, morphs=True, max_morphs=None, remove_alpha=False):
    # 确保输出目录存在（CLI 直接调用时尤其容易漏建）
    _odir = os.path.dirname(os.path.abspath(out_path))
    try:
        os.makedirs(_odir, exist_ok=True)
    except OSError as e:
        raise OSError("无法创建输出目录 %s：%s" % (_odir, e))

    # ---- gather meshes in a stable order
    geo_ids = [nid for nid, n in scene.byid.items()
               if n.name == "Geometry" and nid in scene.geometry_model]
    geo_ids.sort(key=lambda i: scene.byid[i].props[1])

    # ---- bone order = model order for LimbNodes (parents first)
    bones = [m for m in scene.models.values() if m.cls == "LimbNode"]
    bones.sort(key=lambda m: _bone_depth(m))
    if not bones:
        # No skeletal nodes: synthesise a root so skinned/weighted vertices
        # still have a valid bone to bind to (otherwise the PMX is invalid).
        root = Model(0, "Root", "LimbNode")
        bones = [root]
    bone_index = {m.id: i for i, m in enumerate(bones)}

    # ---- per-mesh data
    mesh_data = []
    for gid in geo_ids:
        g = scene.byid[gid]
        mid = scene.geometry_model[gid]
        model = scene.models[mid]
        verts = g.get("Vertices").props[0]
        pvi = g.get("PolygonVertexIndex").props[0]
        nverts = len(verts) // 3
        tris = triangulate(pvi)
        uv = sample_uv(g.get("LayerElementUV"), pvi)
        nrm = sample_normals(g.get("LayerElementNormal"), pvi)
        mats = _mesh_materials(g, model, scene)
        poly_mat = _poly_material_index(g, pvi, len(mats))
        mesh_data.append({
            "geo": g, "model": model, "verts": verts, "tris": tris,
            "uv": uv, "nrm": nrm, "nverts": nverts, "pvi": pvi,
            "mats": mats, "poly_mat": poly_mat,
            "geom": scene._geom_matrix(scene.byid.get(mid)),
        })

    # ---- scale: MMD units put a character at roughly 20 units tall
    if scale_mode == "mmd":
        # 用第一个顶点初始化，避免模型整体浮空（y 全为正）时把 ymin 误当 0 导致缩放算错
        first = True
        ymax = ymin = 0.0
        for md in mesh_data:
            w = md["model"].world
            v = md["verts"]
            for i in range(md["nverts"]):
                y = m_point(w, v[i * 3:i * 3 + 3])[1]
                if first:
                    ymax = ymin = y
                    first = False
                else:
                    ymax = max(ymax, y)
                    ymin = min(ymin, y)
        height = ymax - ymin
        scale = (20.0 / height) if height > 1e-6 else 1.0
    else:
        scale = float(scale_mode)

    def xf(p):
        x, y, z = p
        z = -z if flip_z else z
        return (x * scale, y * scale, z * scale)

    # ---- build vertices
    pmx_verts = []          # (pos, nrm, uv, weights[(bone, w)])
    pmx_tris = []           # (a, b, c)
    segments = []           # (material_id, tri_start, tri_count, mesh_index)
    # (geometry_id, control_point) -> [pmx vertex indices]  (used for morphs)
    geo_cp_to_pmx = {}
    # geometry id -> full transform (world * geometric) for that mesh
    geo_w = {}

    weights_of_geo = {}
    if verbose:
        print("links: geo->model=%d geo->skin=%d cluster->skin=%d cluster->bone=%d"
              % (len(scene.geometry_model), len(scene.geometry_skin),
                 len(scene.cluster_skin), len(scene.cluster_bone)))
    for gid in geo_ids:
        skin = scene.geometry_skin.get(gid)
        acc = {}
        if skin is not None:
            for cid, sid in scene.cluster_skin.items():
                if sid != skin:
                    continue
                bind = scene.cluster_bone.get(cid)
                if bind is None or bind not in bone_index:
                    continue
                cl = scene.byid[cid]
                idn = cl.get("Indexes")
                wn = cl.get("Weights")
                if idn is None or wn is None:
                    continue
                for vi, wt in zip(idn.props[0], wn.props[0]):
                    if wt <= 0.0:
                        continue
                    acc.setdefault(vi, []).append((bind, float(wt)))
        weights_of_geo[gid] = acc

    vbase = 0
    mat_entries = []          # material node id (or None) for each draw segment
    for md in mesh_data:
        gid = md["geo"].props[0]
        model = md["model"]
        # full transform for geometry-space vertices = world * geometric
        w = m_mul(model.world, md["geom"])
        geo_w[gid] = w
        v = md["verts"]
        uv = md["uv"]
        nrm = md["nrm"]
        pvi = md["pvi"]
        acc = weights_of_geo[gid]
        mats = md["mats"]
        poly_mat = md["poly_mat"]

        # Build vertices (deduplicated across the whole mesh) and tag every
        # triangle with its material index.  Triangles are then emitted grouped
        # by material so that each PMX draw segment's faces stay contiguous in
        # the index buffer (required by the format).
        key_map = {}
        mesh_tris = []         # list of (mat_idx, (a, b, c))
        for ti, tri in enumerate(md["tris"]):
            mat_idx = poly_mat[ti] if poly_mat is not None else 0
            out = []
            for cp, corner in tri:
                nval = nrm[corner] if (nrm is not None and corner < len(nrm)) else None
                uval = uv[corner] if (uv is not None and corner < len(uv)) else (0.0, 0.0)
                k = (cp, round(uval[0], 5), round(uval[1], 5))
                if nval is not None:
                    k += (round(nval[0], 4), round(nval[1], 4), round(nval[2], 4))
                j = key_map.get(k)
                if j is None:
                    ppos = xf(m_point(w, v[cp * 3:cp * 3 + 3]))
                    if nval is not None:
                        d = m_dir(w, nval)
                        ln = math.sqrt(d[0] ** 2 + d[1] ** 2 + d[2] ** 2) or 1.0
                        d = (d[0] / ln, d[1] / ln, d[2] / ln)
                        nd = (d[0], d[1], -d[2] if flip_z else d[2])
                    else:
                        nd = (0.0, 1.0, 0.0)
                    wl = sorted(acc.get(cp, []), key=lambda t: -t[1])[:4]
                    if not wl:
                        wl = [(next(iter(bone_index)), 1.0)]
                    tot = sum(x[1] for x in wl) or 1.0
                    wl = [(b, wt / tot) for b, wt in wl]
                    j = len(pmx_verts)
                    key_map[k] = j
                    pmx_verts.append((ppos, nd, (uval[0], 1.0 - uval[1]), wl))
                    geo_cp_to_pmx.setdefault((gid, cp), []).append(j)
                out.append(j)
            mesh_tris.append((mat_idx, (out[0], out[1], out[2])))

        tri_before = len(pmx_tris)
        for mi, mat_id in enumerate(mats):
            grp = [t for (m, t) in mesh_tris if m == mi]
            if not grp:
                continue
            mi_idx = len(mat_entries)
            mat_entries.append(mat_id)
            start = len(pmx_tris)
            pmx_tris.extend(grp)
            segments.append((mi_idx, start, len(grp), model.name))
        if verbose:
            print("  %-22s verts=%-6d tris=%-6d mats=%d skin=%s"
                  % (model.name, len(pmx_verts) - vbase,
                     len(pmx_tris) - tri_before, len(mats),
                     "yes" if acc else "no"))
        vbase = len(pmx_verts)

    # ---- winding check: geometric face normal must agree with the vertex
    # normals we imported, otherwise MMD would render the model inside-out.
    agree = 0.0
    for a, b, c in pmx_tris[:8000]:
        pa, pb, pc = pmx_verts[a][0], pmx_verts[b][0], pmx_verts[c][0]
        ux, uy, uz = pb[0] - pa[0], pb[1] - pa[1], pb[2] - pa[2]
        vx, vy, vz = pc[0] - pa[0], pc[1] - pa[1], pc[2] - pa[2]
        fx, fy, fz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        fl = math.sqrt(fx * fx + fy * fy + fz * fz) or 1.0
        na = pmx_verts[a][1]
        agree += (fx * na[0] + fy * na[1] + fz * na[2]) / fl
    flipped = agree < 0
    if flipped:
        pmx_tris = [(a, c, b) for a, b, c in pmx_tris]
    if verbose:
        print("  winding: face/vertex normal agreement=%.1f -> %s"
              % (agree, "reversed" if flipped else "kept"))

    # ---- textures: collect one diffuse texture per material, build global list
    tex_list = []                 # ordered unique basenames
    tex_name_to_idx = {}
    tex_index_of_mat = {}         # material node id -> texture index (or None)
    for si, (mi_idx, start, cnt, mname) in enumerate(segments):
        mat_node_id = mat_entries[si] if si < len(mat_entries) else None
        if mat_node_id is None:
            continue
        tids = scene.texture_materials.get(mat_node_id, [])
        basename = None
        for tid in tids:
            t = scene.textures.get(tid)
            if t and "diffuse" in t["name"].lower():
                basename = t["name"]
                break
        if basename is None:
            for tid in tids:
                t = scene.textures.get(tid)
                if t:
                    basename = t["name"]
                    break
        if basename:
            if basename not in tex_name_to_idx:
                tex_name_to_idx[basename] = len(tex_list)
                tex_list.append(basename)
            tex_index_of_mat[mat_node_id] = tex_name_to_idx[basename]
    # copy image files next to the pmx when they can be found locally
    _copy_textures(tex_list, scene.path, _odir, verbose, remove_alpha=remove_alpha)

    # ---- index widths depend on the final vertex / triangle / material / texture counts
    # (multi-material meshes raise the material count above the per-mesh guess)
    nv = len(pmx_verts)
    nt = len(pmx_tris)
    vi_size = pick_index_size(nv, signed=False)
    ti_size = pick_index_size(max(len(tex_list), 1) + 1, signed=True)
    mi_size = pick_index_size(max(len(segments), 1), signed=True)
    bi_size = pick_index_size(max(len(bones), 1) + 1, signed=True)

    pw = PmxWriter((vi_size, ti_size, mi_size, bi_size, 1, 1))

    # ---- bone names / positions are taken in the SAME pmx space
    bpos = {}
    for m in bones:
        node = scene.byid.get(m.id)
        bw = m_mul(m.world, scene._geom_matrix(node) if node else m_ident())
        bpos[m.id] = xf(m_translation(bw))

    # ---- optional centering / grounding (MMD convention: origin-centered, feet at y=0)
    if center and nv:
        xs = [p[0] for p, _, _, _ in pmx_verts]
        ys = [p[1] for p, _, _, _ in pmx_verts]
        zs = [p[2] for p, _, _, _ in pmx_verts]
        off = (-(min(xs) + max(xs)) / 2.0, -min(ys), -(min(zs) + max(zs)) / 2.0)
        pmx_verts = [((p[0] + off[0], p[1] + off[1], p[2] + off[2]), n, uv, wl)
                     for p, n, uv, wl in pmx_verts]
        for k in bpos:
            x, y, z = bpos[k]
            bpos[k] = (x + off[0], y + off[1], z + off[2])
        if verbose:
            print("  centered: offset=(%.2f, %.2f, %.2f)" % off)

    model_name = (name or os.path.splitext(os.path.basename(out_path))[0]).strip()
    if not model_name:
        model_name = "Model"
    pw.header(model_name, model_name,
              "Converted from FBX by fbx2pmx.py",
              "Converted from FBX by fbx2pmx.py")

    # vertices
    pw.i32(nv)
    for pos, nd, uv, wl in pmx_verts:
        pw.f32(pos[0]); pw.f32(pos[1]); pw.f32(pos[2])
        pw.f32(nd[0]); pw.f32(nd[1]); pw.f32(nd[2])
        pw.f32(uv[0]); pw.f32(uv[1])
        n = len(wl)
        if n == 1:
            pw.buf += bytes([0])
            pw.idx(bone_index[wl[0][0]], bi_size)
            wsum = 1.0
        elif n == 2:
            pw.buf += bytes([1])
            pw.idx(bone_index[wl[0][0]], bi_size)
            pw.idx(bone_index[wl[1][0]], bi_size)
            pw.f32(wl[0][1])
            wsum = 1.0
        else:
            while len(wl) < 4:
                wl.append((wl[-1][0], 0.0))     # pad with zero weight
            pw.buf += bytes([2])
            for b, _ in wl:
                pw.idx(bone_index[b], bi_size)
            for _, wt in wl:
                pw.f32(wt)
            wsum = 1.0
        # 顶点 edge scale：写 0 去除 MMD 的黑色轮廓线
        pw.f32(0.0)

    # faces
    pw.i32(nt * 3)
    for tri in pmx_tris:
        pw.uidx(tri[0], vi_size)
        pw.uidx(tri[1], vi_size)
        pw.uidx(tri[2], vi_size)

    # textures
    pw.i32(len(tex_list))
    for bn in tex_list:
        pw.text("textures\\" + bn)

    # materials (one PMX material per draw segment, in segment order)
    pw.i32(len(segments))
    for si, (mi_idx, start, cnt, mname) in enumerate(segments):
        mat_node_id = mat_entries[si] if si < len(mat_entries) else None
        mat = scene.byid.get(mat_node_id) if mat_node_id else None
        mlabel = _clean_name(mat.props[1]) if mat else (mname or ("Material%d" % si))
        diffuse = [1.0, 1.0, 1.0, 1.0]
        if mat is not None:
            dc = mat.get("DiffuseColor")
            if dc is not None and len(dc.props) >= 3:
                diffuse = [float(dc.props[0]), float(dc.props[1]),
                           float(dc.props[2]), 1.0]
        pw.text(mlabel); pw.text(mlabel)
        for v4 in diffuse:
            pw.f32(v4)
        pw.f32(0.0); pw.f32(0.0); pw.f32(0.0)          # specular
        pw.f32(0.0)                                     # shininess
        pw.f32(0.5); pw.f32(0.5); pw.f32(0.5)          # ambient (按需求默认 0.5 0.5 0.5)
        # 关闭轮廓线（0x10），保留双面+地面阴影+自身阴影贴图
        pw.buf += bytes([0x01 | 0x02 | 0x04])
        pw.f32(0.0); pw.f32(0.0); pw.f32(0.0); pw.f32(0.0)   # edge colour（alpha=0 彻底去黑边）
        pw.f32(0.0)                                     # edge size
        tex_idx = tex_index_of_mat.get(mat_node_id, -1) if mat_node_id else -1
        pw.idx(tex_idx, ti_size)                        # texture
        pw.idx(-1, ti_size)                             # sphere
        pw.buf += bytes([0])                            # sphere mode
        pw.buf += bytes([1])                            # toon: internal
        pw.buf += bytes([0])                            # toon 0
        pw.text("")
        pw.i32(cnt * 3)

    # bones
    pw.i32(len(bones))
    for m in bones:
        pw.text(m.name); pw.text(m.name)
        p = bpos[m.id]
        pw.f32(p[0]); pw.f32(p[1]); pw.f32(p[2])
        par = m.parent
        while par is not None and par.id not in bone_index:
            par = par.parent
        pw.idx(bone_index[par.id] if par else -1, bi_size)
        pw.i32(0)                                       # deform layer
        child_bone = next((c for c in m.children if c.id in bone_index), None)
        bflag = 0x0002 | 0x0004 | 0x0008 | 0x0010       # rot/move/visible/operable
        if child_bone is not None:
            bflag |= 0x0001                             # tail stored as bone index
        pw.buf += struct.pack("<H", bflag)              # bone flags are u16
        if bflag & 0x0001:
            pw.idx(bone_index[child_bone.id], bi_size)
        else:
            pw.f32(0.0); pw.f32(0.5); pw.f32(0.0)
        # (no rotatable/movable axis flags, no IK block)

    # ---- morphs: FBX blend shapes -> PMX vertex morphs (表情)
    written_morphs = 0
    if morphs and getattr(scene, "morphs", None):
        morph_list = []
        for m in scene.morphs:
            gid = m["geo_id"]
            w = geo_w.get(gid)
            if w is None:
                continue
            indexes = m["indexes"]
            deltas = m["deltas"]
            # 自动检测 Shape.Vertices 是「相对偏移」还是「绝对坐标」：不同导出器写法不同。
            # 相对偏移写法下 shape 值本身即位移（量级小）；绝对坐标写法下 shape 与 base 同量级（大），
            # 此时真正的位移应为 shape − base。实测 Kiana 为相对偏移写法。
            base_node = scene.byid.get(gid)
            base = base_node.get("Vertices").props[0] if base_node else None
            is_relative = True
            if base is not None and indexes:
                rel_sum = abs_sum = 0.0
                for k in range(min(len(indexes), 64)):
                    cp = indexes[k]
                    dx, dy, dz = deltas[k * 3], deltas[k * 3 + 1], deltas[k * 3 + 2]
                    rel_sum += abs(dx) + abs(dy) + abs(dz)
                    bx, by, bz = base[cp * 3], base[cp * 3 + 1], base[cp * 3 + 2]
                    abs_sum += abs(dx - bx) + abs(dy - by) + abs(dz - bz)
                is_relative = rel_sum <= abs_sum
            offsets = []
            for k in range(len(indexes)):
                cp = indexes[k]
                dx = deltas[k * 3]
                dy = deltas[k * 3 + 1]
                dz = deltas[k * 3 + 2]
                if not is_relative and base is not None:
                    bx, by, bz = base[cp * 3], base[cp * 3 + 1], base[cp * 3 + 2]
                    dx -= bx; dy -= by; dz -= bz
                off = xf(m_dir(w, (dx, dy, dz)))   # same linear transform as base
                for vidx in geo_cp_to_pmx.get((gid, cp), []):
                    offsets.append((vidx, off))
            if not offsets:
                continue
            nm = m["name"]
            low = nm.lower()
            # panel: which FACS tab the morph shows in (1=眉 2=目 3=口 4=その他)
            if low.startswith("eyebrow") or "brow" in low:
                panel = 1
            elif low.startswith("eye") or "eyeshape" in low:
                panel = 2
            elif low.startswith("mouth"):
                panel = 3
            else:
                panel = 4
            kind = 1                    # 1 = 頂点 (vertex) morph; PMX type, not FACS tab
            morph_list.append({"name": nm, "name_en": nm,
                               "panel": panel, "kind": kind,
                               "offsets": offsets})
        if max_morphs is not None and len(morph_list) > max_morphs:
            if verbose:
                print("  morphs: capping %d -> %d (max_morphs)"
                      % (len(morph_list), max_morphs))
            morph_list = morph_list[:max_morphs]
        if verbose:
            print("  morphs: wrote %d vertex morph(s) from %d blend shape(s)"
                  % (len(morph_list), len(scene.morphs)))
        written_morphs = len(morph_list)
        pw.i32(len(morph_list))
        for mo in morph_list:
            pw.text(mo["name"]); pw.text(mo["name_en"])
            pw.buf += bytes([mo["panel"]])
            pw.buf += bytes([mo["kind"]])
            pw.i32(len(mo["offsets"]))
            for vidx, off in mo["offsets"]:
                pw.uidx(vidx, vi_size)
                pw.f32(off[0]); pw.f32(off[1]); pw.f32(off[2])
    else:
        pw.i32(0)

    # display frames: one, listing every bone
    pw.i32(1)
    pw.text("Root"); pw.text("Root")
    pw.buf += bytes([1])
    pw.i32(len(bones))
    for m in bones:
        pw.buf += bytes([0])            # 要素対象 0 = ボーン
        pw.idx(bone_index[m.id], bi_size)

    # rigid bodies / joints
    pw.i32(0)
    pw.i32(0)

    with open(out_path, "wb") as f:
        f.write(pw.buf)
    return {"verts": nv, "tris": nt, "bones": len(bones),
            "materials": len(segments), "bytes": len(pw.buf),
            "morphs": written_morphs, "scale": scale, "vi_size": vi_size}


def _bone_depth(m):
    d = 0
    p = m.parent
    while p is not None:
        d += 1
        p = p.parent
    return (d, m.name)


# ------------------------------------------------------------------ cli -----

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("fbx")
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--scale", default="mmd",
                    help="'mmd' (auto, ~20 units tall), 'raw' (1.0), or a number")
    ap.add_argument("--no-flip-z", action="store_true",
                    help="keep FBX handedness (default converts to MMD)")
    ap.add_argument("--no-center", action="store_true",
                    help="do not re-center/ground the model (keep FBX world position)")
    ap.add_argument("--no-morphs", action="store_true",
                    help="do not export FBX blend shapes as PMX morphs (表情)")
    ap.add_argument("--remove-alpha", action="store_true",
                    help="drop the alpha channel of copied textures (write opaque RGB)")
    ap.add_argument("--max-morphs", type=int, default=None,
                    help="cap the number of exported morphs (default: no limit)")
    ap.add_argument("--info", action="store_true")
    a = ap.parse_args()

    scene = Scene(a.fbx)
    print("FBX version %d, %d objects" % (scene.ver, len(scene.objects)))
    print("models=%d (bones=%d)  geometry=%d  materials=%d"
          % (len(scene.models),
             sum(1 for m in scene.models.values() if m.cls == "LimbNode"),
             sum(1 for n in scene.objects if n.name == "Geometry"),
             sum(1 for n in scene.objects if n.name == "Material")))
    if a.info:
        return 0

    out = a.out or (os.path.splitext(a.fbx)[0] + ".pmx")
    scale = a.scale
    if scale not in ("mmd", "raw"):
        try:
            scale = float(scale)
        except ValueError:
            scale = "mmd"
    if scale == "raw":
        scale = 1.0
    st = convert(scene, out, scale_mode=scale, flip_z=not a.no_flip_z,
                 center=not a.no_center, morphs=not a.no_morphs,
                 max_morphs=a.max_morphs, remove_alpha=a.remove_alpha,
                 name=os.path.splitext(os.path.basename(a.fbx))[0])
    print("\nwrote %s" % out)
    print("  vertices=%d  triangles=%d  bones=%d  materials=%d  morphs=%d  scale=%.4f"
          % (st["verts"], st["tris"], st["bones"], st["materials"],
             st["morphs"], st["scale"]))
    print("  size=%.2f MB  (vertex index width %d bytes)"
          % (st["bytes"] / 1048576, st["vi_size"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
