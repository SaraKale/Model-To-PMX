# -*- coding: utf-8 -*-
"""UEFormat（.uemodel）读写库 —— 纯标准库。

`.uemodel` 不是某游戏的私有格式，而是 **UEFormat**：FortnitePorting / FModel /
CUE4Parse 用来在「UE 资源」和「Blender / UE 插件」之间交换模型的公开中间格式，
规范在 https://github.com/h4lfheart/UEFormat （docs/generic.md + docs/uemodel.md），
Blender 插件（io_scene_ueformat）与 UE 插件都是开源的。所以它**有统一标准**。

但标准有过一次结构性改版（version.py 里的 AttributeFormatRestructure = 10），
所以实际存在两套字节布局：

v <= 9（legacy）
    头部： "UEFORMAT" + FString Identifier + u8 FileVersion + FString ObjectName
           + bool bIsCompressed
    主体： 从当前位置一口气读到 EOF，每个「分块」是
               FString Name + i32 ArraySize + i32 ByteSize + ByteSize 字节负载
           ArraySize 是**负载内**数组的元素个数，写在分块头部；
           负载里不再重复数组长度。

v >= 10（AttributeFormatRestructure）
    头部： 同上，但在 ObjectName 之后多了 FString ObjectPath
    主体： FDataAttributeSet = i32 Count + Count × (FString Name + i32 ByteSize + 负载)
           数组长度写在负载内部的 TArray 前缀里（i32 Count + 元素）。

读取端按版本自动分派；写出端两版都能写。

坐标与单位（实测样本 + UE 约定）
    UE 是**左手系、Z 轴向上**，X 向前、Y 向右；1 单位 = 1 cm。
    MMD/PMX 是左手系、Y 轴向上、脸朝 -Z。两边手性相同（行列式 +1），
    所以只需要换轴、**不需要反绕序**（见 convert/uemodel2pmx.py）。

自检口径：`read_uemodel()` 结束时必须 `r.p == len(r.b)`，与 pmxio 的
`consumed == filesize` 一个路子；分块负载也逐个按 ByteSize 切片消费，
任何一段读偏都会在末尾暴露成「末尾字节数不符」。
"""

import os
import struct
import sys

MAGIC = b"UEFORMAT"
IDENT_MODEL = "UEMODEL"

VERSION_LATEST = 10          # AttributeFormatRestructure
V_SERIALIZE_BINORMAL = 1     # NORMALS 里开始带 binormalSign
V_MULTI_VCOLOR = 2           # VERTEXCOLORS 支持多套
V_LOD_RESTRUCTURE = 4        # LOD 变成 name + 独立大小
V_VIRTUAL_BONES = 5
V_MATERIAL_PATH = 6
V_ASSET_METADATA = 7
V_PRESERVE_TRANSFORM = 8     # BONES 带 scale
V_ATTR_RESTRUCTURE = 10      # 布局大改（见模块 docstring）


def _repair_version(v):
    """老版本写出来的 0 号版本其实等于 1（BinormalSign 已经在了）。"""
    return V_SERIALIZE_BINORMAL if v == 0 else v


# ------------------------------------------------------------------ 数据结构 --
class Bone(object):
    __slots__ = ("name", "parent", "pos", "rot", "scale")

    def __init__(self, name="", parent=-1, pos=(0.0, 0.0, 0.0),
                 rot=(0.0, 0.0, 0.0, 1.0), scale=(1.0, 1.0, 1.0)):
        self.name = name
        self.parent = parent
        self.pos = pos
        self.rot = rot
        self.scale = scale


class Socket(object):
    __slots__ = ("name", "bone", "pos", "rot", "scale")

    def __init__(self, name="", bone="", pos=(0.0, 0.0, 0.0),
                 rot=(0.0, 0.0, 0.0, 1.0), scale=(1.0, 1.0, 1.0)):
        self.name = name
        self.bone = bone
        self.pos = pos
        self.rot = rot
        self.scale = scale


class Skeleton(object):
    __slots__ = ("path", "bones", "sockets", "virtual_bones")

    def __init__(self):
        self.path = ""
        self.bones = []
        self.sockets = []
        self.virtual_bones = []          # [(source, target, virtual)]


class VertexColor(object):
    __slots__ = ("name", "data")         # data: [(r,g,b,a)] 0~255

    def __init__(self, name="COL0", data=None):
        self.name = name
        self.data = data or []


class Material(object):
    __slots__ = ("name", "path", "first_index", "num_faces")

    def __init__(self, name="", path="", first_index=0, num_faces=0):
        self.name = name
        self.path = path
        self.first_index = first_index     # 索引入缓冲（顶点索引）里的起点
        self.num_faces = num_faces         # 三角面数

    @property
    def first_face(self):
        return self.first_index // 3


class Weight(object):
    __slots__ = ("bone", "vertex", "weight")

    def __init__(self, bone=0, vertex=0, weight=0.0):
        self.bone = bone
        self.vertex = vertex
        self.weight = weight


class MorphDelta(object):
    __slots__ = ("pos", "normal", "vertex")

    def __init__(self, pos=(0.0, 0.0, 0.0), normal=(0.0, 0.0, 0.0), vertex=0):
        self.pos = pos
        self.normal = normal
        self.vertex = vertex


class Morph(object):
    __slots__ = ("name", "deltas")

    def __init__(self, name="", deltas=None):
        self.name = name
        self.deltas = deltas or []


class UVSet(object):
    __slots__ = ("name", "uvs")

    def __init__(self, name="UV0", uvs=None):
        self.name = name
        self.uvs = uvs or []


class Collision(object):
    __slots__ = ("name", "vertices", "indices", "faces")

    def __init__(self, name="", vertices=None, indices=None):
        self.name = name
        self.vertices = vertices or []
        self.indices = indices or []


class LOD(object):
    def __init__(self, name="LOD0"):
        self.name = name
        self.vertices = []        # [(x, y, z)]
        self.normals = []         # [(x, y, z)]
        self.binormals = []       # 每顶点 binormal sign（FNormal 的第一个 float）
        self.tangents = []        # [(x, y, z)]
        self.uvs = []             # [UVSet]
        self.indices = []         # 三角面顶点索引（每 3 个一面）
        self.colors = []          # [VertexColor]
        self.materials = []       # [Material]
        self.weights = []         # [Weight]（稀疏：一个顶点可能有多条）
        self.morphs = []          # [Morph]

    def stats(self):
        return {"verts": len(self.vertices), "tris": len(self.indices) // 3,
                "mats": len(self.materials), "weights": len(self.weights),
                "morphs": len(self.morphs), "uvsets": len(self.uvs)}


class UEModel(object):
    def __init__(self):
        self.identifier = IDENT_MODEL
        self.version = VERSION_LATEST
        self.name = ""
        self.path = ""
        self.compressed = False
        self.lods = []
        self.skeleton = Skeleton()
        self.collisions = []

    def stats(self):
        return {"version": self.version, "lods": len(self.lods),
                "bones": len(self.skeleton.bones),
                "sockets": len(self.skeleton.sockets),
                "collisions": len(self.collisions),
                "skinned": bool(self.lods and self.lods[0].weights)}


# --------------------------------------------------------------------- 读取 --
class _R(object):
    __slots__ = ("b", "p")

    def __init__(self, data):
        self.b = data
        self.p = 0

    def eof(self):
        return self.p >= len(self.b)

    def left(self):
        return len(self.b) - self.p

    def u8(self):
        v = self.b[self.p]
        self.p += 1
        return v

    def bool(self):
        return self.u8() != 0

    def i16(self):
        v = struct.unpack_from("<h", self.b, self.p)[0]
        self.p += 2
        return v

    def u16(self):
        v = struct.unpack_from("<H", self.b, self.p)[0]
        self.p += 2
        return v

    def i32(self):
        v = struct.unpack_from("<i", self.b, self.p)[0]
        self.p += 4
        return v

    def u32(self):
        v = struct.unpack_from("<I", self.b, self.p)[0]
        self.p += 4
        return v

    def f32(self):
        v = struct.unpack_from("<f", self.b, self.p)[0]
        self.p += 4
        return v

    def vec(self, n):
        v = struct.unpack_from("<%df" % n, self.b, self.p)
        self.p += 4 * n
        return v

    def ints(self, n):
        v = struct.unpack_from("<%dI" % n, self.b, self.p) if n > 0 else ()
        self.p += 4 * n
        return v

    def bytes(self, n):
        v = self.b[self.p:self.p + n]
        self.p += n
        return v

    def fstr(self):
        n = self.i32()
        if n <= 0:
            return ""
        raw = self.b[self.p:self.p + n]
        self.p += n
        return raw.rstrip(b"\x00").decode("utf-8", "replace")


def _chunks(r, version):
    """按版本迭代「分块」，产出 (name, array_size, payload_reader)。

    负载一律按声明字节数切出来单独给一个 reader —— 与官方 iter_chunks 的
    「读完 seek 到下一个块」等价，任何一段读偏都不会污染后面的分块。
    """
    if version >= V_ATTR_RESTRUCTURE:
        count = r.i32()
        for _ in range(count):
            name = r.fstr()
            size = r.i32()
            yield name, None, _R(r.bytes(max(0, size)))
    else:
        while not r.eof():
            name = r.fstr()
            arr = r.i32()
            size = r.i32()
            yield name, arr, _R(r.bytes(max(0, size)))


def _arr_len(payload, version, array_size):
    """取「数组元素个数」：v10 在负载里，v9 在分块头。"""
    if version >= V_ATTR_RESTRUCTURE:
        return payload.i32()
    return array_size if array_size is not None else 0


def _read_lod(r, version):
    lod = LOD(name=r.fstr())
    if version >= V_ATTR_RESTRUCTURE:
        body = r
    else:
        body = _R(r.bytes(max(0, r.i32())))

    for name, arr, pl in _chunks(body, version):
        if name == "VERTICES":
            n = _arr_len(pl, version, arr)
            lod.vertices = [tuple(pl.vec(3)) for _ in range(n)]
        elif name == "NORMALS":
            n = _arr_len(pl, version, arr)
            if version >= V_SERIALIZE_BINORMAL:
                for _ in range(n):
                    sign = pl.f32()
                    lod.binormals.append(sign)
                    lod.normals.append(tuple(pl.vec(3)))
            else:
                lod.normals = [tuple(pl.vec(3)) for _ in range(n)]
        elif name == "TANGENTS":
            n = _arr_len(pl, version, arr)
            lod.tangents = [tuple(pl.vec(3)) for _ in range(n)]
        elif name == "TEXCOORDS":
            n = _arr_len(pl, version, arr)
            for _ in range(n):
                # v10 起每个 UV 通道带名字，v9 没有
                uname = pl.fstr() if version >= V_ATTR_RESTRUCTURE else ""
                c = pl.i32()
                uv = [tuple(pl.vec(2)) for _ in range(max(0, c))]
                lod.uvs.append(UVSet(uname or ("UV%d" % len(lod.uvs)), uv))
        elif name == "INDICES":
            n = _arr_len(pl, version, arr)
            lod.indices = list(pl.ints(max(0, n)))
        elif name == "VERTEXCOLORS":
            n = _arr_len(pl, version, arr)
            for _ in range(n):
                vc = VertexColor(name="COL0")
                if version >= V_MULTI_VCOLOR:
                    vc.name = pl.fstr()
                c = pl.i32()
                raw = pl.bytes(max(0, c) * 4)
                vc.data = [tuple(raw[i * 4:i * 4 + 4]) for i in range(max(0, c))]
                lod.colors.append(vc)
        elif name == "MATERIALS":
            n = _arr_len(pl, version, arr)
            for _ in range(n):
                mat = Material(name=pl.fstr())
                if version >= V_MATERIAL_PATH:
                    mat.path = pl.fstr()
                mat.first_index = pl.i32()
                mat.num_faces = pl.i32()
                lod.materials.append(mat)
        elif name == "WEIGHTS":
            n = _arr_len(pl, version, arr)
            for _ in range(n):
                b = pl.u16() if version >= V_ATTR_RESTRUCTURE else pl.i16()
                lod.weights.append(Weight(b, pl.i32(), pl.f32()))
        elif name == "MORPHTARGETS":
            n = _arr_len(pl, version, arr)
            for _ in range(n):
                mo = Morph(name=pl.fstr())
                c = pl.i32()
                for _ in range(max(0, c)):
                    p = tuple(pl.vec(3))
                    nn = tuple(pl.vec(3))
                    mo.deltas.append(MorphDelta(p, nn, pl.i32()))
                lod.morphs.append(mo)
        # 未知分块按声明大小跳过（已经按 ByteSize 切片，天然安全）
    return lod


def _read_bones(payload, n, version):
    """骨骼数组：v10 带 scale；v9 不带。带自动回退以兼容不同写入器。"""
    with_scale = version >= V_ATTR_RESTRUCTURE
    bones = []
    try:
        for _ in range(n):
            b = Bone(name=payload.fstr())
            b.parent = payload.i32()
            b.pos = tuple(payload.vec(3))
            b.rot = tuple(payload.vec(4))
            b.scale = tuple(payload.vec(3)) if with_scale else ()
            bones.append(b)
    except struct.error:
        return None
    if payload.p != len(payload.b):
        return None
    if not with_scale:
        # 若按「无 scale」读却没正好吃到末尾，改按「有 scale」重来
        alt = _R(payload.b)
        bones2 = []
        try:
            for _ in range(n):
                b = Bone(name=alt.fstr())
                b.parent = alt.i32()
                b.pos = tuple(alt.vec(3))
                b.rot = tuple(alt.vec(4))
                b.scale = tuple(alt.vec(3))
                bones2.append(b)
        except struct.error:
            return bones
        if alt.p == len(alt.b):
            return bones2
    return bones


def _read_skeleton(r, version):
    sk = Skeleton()
    for name, arr, pl in _chunks(r, version):
        if name == "METADATA":
            sk.path = pl.fstr()
        elif name == "BONES":
            n = _arr_len(pl, version, arr)
            got = _read_bones(pl, max(0, n), version)
            if got is not None:
                sk.bones = got
        elif name == "SOCKETS":
            n = _arr_len(pl, version, arr)
            for _ in range(max(0, n)):
                s = Socket(name=pl.fstr(), bone=pl.fstr())
                s.pos = tuple(pl.vec(3))
                s.rot = tuple(pl.vec(4))
                s.scale = tuple(pl.vec(3))
                sk.sockets.append(s)
        elif name == "VIRTUALBONES":
            n = _arr_len(pl, version, arr)
            for _ in range(max(0, n)):
                sk.virtual_bones.append((pl.fstr(), pl.fstr(), pl.fstr()))
    return sk


def _read_collision(h, version):
    col = Collision(name=h.fstr())
    nv = h.i32()
    col.vertices = [tuple(h.vec(3)) for _ in range(max(0, nv))]
    ni = h.i32()
    col.indices = list(h.ints(max(0, ni)))
    return col


def _decompress_body(r, src):
    """读取压缩体头部并解压，返回未压缩的字节流（分块流）。

    支持 UEFormat 已用到的两种压缩：ZSTD（需 zstandard 第三方库）、
    GZIP（标准库 gzip）。压缩体头部布局见 parse_uemodel 中的说明。
    """
    import io
    cfmt = (r.fstr() or "").upper()
    uncompressed = r.i32()
    compressed = r.i32()
    payload = r.bytes(compressed) if compressed > 0 else r.left()

    if cfmt == "ZSTD":
        try:
            import zstandard as zstd
        except ImportError:
            raise ValueError("该 .uemodel 是 ZSTD 压缩体，但运行环境缺少 zstandard 库，"
                             "无法解压：%s" % src)
        dctx = zstd.ZstdDecompressor()
        try:
            if uncompressed > 0:
                return dctx.decompress(payload, uncompressed)
            # 帧内未记录原始大小：改用流式解压读完整帧
            with dctx.stream_reader(io.BytesIO(payload)) as reader:
                return reader.read()
        except Exception as e:
            raise ValueError("ZSTD 解压失败：%s（%s）" % (src, e))
    elif cfmt in ("GZIP", "GZIPSTREAM"):
        import gzip
        try:
            return gzip.decompress(payload)
        except Exception as e:
            raise ValueError("GZIP 解压失败：%s（%s）" % (src, e))
    else:
        raise ValueError("不支持的压缩格式 %r：%s" % (cfmt, src))


def read_uemodel(path, verbose=False):
    """读 .uemodel。返回 UEModel；末尾字节数不符直接抛 ValueError。"""
    with open(path, "rb") as f:
        data = f.read()
    return parse_uemodel(data, verbose=verbose, src=path)


def parse_uemodel(data, verbose=False, src=""):
    r = _R(data)
    if r.bytes(8) != MAGIC:
        raise ValueError("不是 UEFormat 文件（魔数不是 UEFORMAT）：%s" % src)

    m = UEModel()
    m.identifier = r.fstr()
    m.version = r.u8()
    m.name = r.fstr()
    if m.version >= V_ATTR_RESTRUCTURE:
        m.path = r.fstr()          # v10 起才有 ObjectPath
    m.compressed = r.bool()
    if m.compressed:
        # 压缩体头部紧跟在 bIsCompressed 之后（UEFormat 规范）：
        #   FString CompressionFormat + i32 UncompressedSize
        #   + i32 CompressedSize + CompressedData
        # 解压后得到的就是未压缩的「分块流」，从偏移 0 起按普通流程解析即可。
        raw = _decompress_body(r, src)
        r = _R(raw)

    if m.identifier not in ("UEMODEL", ""):
        raise ValueError("不是模型文件（Identifier=%r）：%s" % (m.identifier, src))
    if m.version > VERSION_LATEST:
        raise ValueError("UEFormat 版本 %d 高于本库支持的 %d：%s"
                         % (m.version, VERSION_LATEST, src))

    ver = _repair_version(m.version)
    for name, arr, pl in _chunks(r, ver):
        if name == "LODS":
            n = _arr_len(pl, ver, arr)
            for _ in range(max(0, n)):
                m.lods.append(_read_lod(pl, ver))
        elif name == "SKELETON":
            m.skeleton = _read_skeleton(pl, ver)
        elif name == "COLLISION":
            n = _arr_len(pl, ver, arr)
            for _ in range(max(0, n)):
                m.collisions.append(_read_collision(pl, ver))
        # 未知顶层分块忽略

    if r.p != len(r.b):
        raise ValueError("解析失步：消费 %d 字节，文件 %d 字节（差 %d）"
                         % (r.p, len(r.b), len(r.b) - r.p))
    if verbose:
        print_uemodel(m, src=src)
    return m


# --------------------------------------------------------------------- 写出 --
class _W(object):
    __slots__ = ("buf",)

    def __init__(self):
        self.buf = bytearray()

    def u8(self, v):
        self.buf.append(v & 0xFF)

    def bool(self, v):
        self.u8(1 if v else 0)

    def i16(self, v):
        self.buf += struct.pack("<h", int(v))

    def u16(self, v):
        self.buf += struct.pack("<H", int(v) & 0xFFFF)

    def i32(self, v):
        self.buf += struct.pack("<i", int(v))

    def u32(self, v):
        self.buf += struct.pack("<I", int(v))

    def f32(self, v):
        self.buf += struct.pack("<f", float(v))

    def vec(self, v):
        self.buf += struct.pack("<%df" % len(v), *[float(x) for x in v])

    def raw(self, b):
        self.buf += b

    def fstr(self, s):
        b = (s or "").encode("utf-8")
        self.i32(len(b))
        self.buf += b

    def get(self):
        return bytes(self.buf)


def _pack_chunk(name, array_size, payload, version):
    w = _W()
    w.fstr(name)
    if version < V_ATTR_RESTRUCTURE:
        w.i32(array_size if array_size is not None else 0)
    w.i32(len(payload))
    w.raw(payload)
    return w.get()


def _pack_attrset(items, version):
    """items: [(name, array_size, payload_bytes)]"""
    w = _W()
    if version >= V_ATTR_RESTRUCTURE:
        w.i32(len(items))
    for name, arr, payload in items:
        w.raw(_pack_chunk(name, arr, payload, version))
    return w.get()


def _pack_vec_array(vecs, version, width=3):
    w = _W()
    if version >= V_ATTR_RESTRUCTURE:
        w.i32(len(vecs))
    for v in vecs:
        w.vec(v)
    return w.get()


def _pack_indices(idx, version):
    w = _W()
    if version >= V_ATTR_RESTRUCTURE:
        w.i32(len(idx))
    for v in idx:
        w.u32(v)
    return w.get()


def _pack_lod(lod, version):
    w = _W()
    items = []
    nv = len(lod.vertices)

    items.append(("VERTICES", nv, _pack_vec_array(lod.vertices, version)))

    nrm = _W()
    if version >= V_ATTR_RESTRUCTURE:
        nrm.i32(nv)
    for i in range(nv):
        n = lod.normals[i] if i < len(lod.normals) else (0.0, 0.0, 1.0)
        sign = lod.binormals[i] if i < len(lod.binormals) else 1.0
        nrm.f32(sign)
        nrm.vec(n)
    if version < V_SERIALIZE_BINORMAL:
        # 极老版本（本库不产出）：只写向量
        old = _W()
        for i in range(nv):
            old.vec(lod.normals[i] if i < len(lod.normals) else (0.0, 1.0, 0.0))
        items.append(("NORMALS", nv, old.get()))
    else:
        items.append(("NORMALS", nv, nrm.get()))

    if lod.tangents:
        items.append(("TANGENTS", nv, _pack_vec_array(lod.tangents, version)))

    tex = _W()
    if version >= V_ATTR_RESTRUCTURE:
        tex.i32(len(lod.uvs))
    for i, st in enumerate(lod.uvs):
        if version >= V_ATTR_RESTRUCTURE:
            tex.fstr(st.name or ("UV%d" % i))
        tex.i32(len(st.uvs))
        for uv in st.uvs:
            tex.vec(uv)
    items.append(("TEXCOORDS", len(lod.uvs), tex.get()))

    items.append(("INDICES", len(lod.indices),
                  _pack_indices(lod.indices, version)))

    if lod.colors:
        vc = _W()
        if version >= V_ATTR_RESTRUCTURE:
            vc.i32(len(lod.colors))
        for st in lod.colors:
            if version >= V_MULTI_VCOLOR:
                vc.fstr(st.name or "COL0")
            vc.i32(len(st.data))
            for c in st.data:
                vc.u8(c[0] if len(c) > 0 else 255)
                vc.u8(c[1] if len(c) > 1 else 255)
                vc.u8(c[2] if len(c) > 2 else 255)
                vc.u8(c[3] if len(c) > 3 else 255)
        items.append(("VERTEXCOLORS", len(lod.colors), vc.get()))

    if lod.materials:
        mm = _W()
        if version >= V_ATTR_RESTRUCTURE:
            mm.i32(len(lod.materials))
        for mt in lod.materials:
            mm.fstr(mt.name)
            if version >= V_MATERIAL_PATH:
                mm.fstr(mt.path)
            mm.i32(mt.first_index)
            mm.i32(mt.num_faces)
        items.append(("MATERIALS", len(lod.materials), mm.get()))

    if lod.weights:
        wt = _W()
        if version >= V_ATTR_RESTRUCTURE:
            wt.i32(len(lod.weights))
        for x in lod.weights:
            if version >= V_ATTR_RESTRUCTURE:
                wt.u16(x.bone)
            else:
                wt.i16(x.bone)
            wt.i32(x.vertex)
            wt.f32(x.weight)
        items.append(("WEIGHTS", len(lod.weights), wt.get()))

    if lod.morphs:
        mo = _W()
        if version >= V_ATTR_RESTRUCTURE:
            mo.i32(len(lod.morphs))
        for m in lod.morphs:
            mo.fstr(m.name)
            mo.i32(len(m.deltas))
            for d in m.deltas:
                mo.vec(d.pos)
                mo.vec(d.normal)
                mo.i32(d.vertex)
        items.append(("MORPHTARGETS", len(lod.morphs), mo.get()))

    body = _pack_attrset(items, version)
    if version >= V_ATTR_RESTRUCTURE:
        # v10 的 UEModelLOD = FString Name + FDataAttributeSet（规范 docs/uemodel.md）：
        # 名字照写，但属性集**自带** i32 Count 定界，不再套一层 ByteSize。
        # 曾经漏写这里的名 → 读取端读了名，整条 LOD 流错位，末尾报「解析失步」。
        w.fstr(lod.name or "")
        w.raw(body)
    else:
        w.fstr(lod.name)
        w.i32(len(body))
        w.raw(body)
    return w.get()


def _pack_skeleton(sk, version):
    items = []
    if sk.path:
        w = _W()
        w.fstr(sk.path)
        items.append(("METADATA", 1, w.get()))

    bw = _W()
    if version >= V_ATTR_RESTRUCTURE:
        bw.i32(len(sk.bones))
    for b in sk.bones:
        bw.fstr(b.name)
        bw.i32(b.parent)
        bw.vec(b.pos)
        bw.vec(b.rot if len(b.rot) == 4 else (0.0, 0.0, 0.0, 1.0))
        if version >= V_ATTR_RESTRUCTURE:
            bw.vec(b.scale if len(b.scale) == 3 else (1.0, 1.0, 1.0))
    items.append(("BONES", len(sk.bones), bw.get()))

    if sk.sockets:
        sw = _W()
        if version >= V_ATTR_RESTRUCTURE:
            sw.i32(len(sk.sockets))
        for s in sk.sockets:
            sw.fstr(s.name)
            sw.fstr(s.bone)
            sw.vec(s.pos)
            sw.vec(s.rot)
            sw.vec(s.scale)
        items.append(("SOCKETS", len(sk.sockets), sw.get()))

    if sk.virtual_bones:
        vw = _W()
        if version >= V_ATTR_RESTRUCTURE:
            vw.i32(len(sk.virtual_bones))
        for t in sk.virtual_bones:
            vw.fstr(t[0])
            vw.fstr(t[1])
            vw.fstr(t[2])
        items.append(("VIRTUALBONES", len(sk.virtual_bones), vw.get()))

    return _pack_attrset(items, version)


def _pack_collision(col, version):
    w = _W()
    w.fstr(col.name)
    w.i32(len(col.vertices))
    for v in col.vertices:
        w.vec(v)
    w.i32(len(col.indices))
    for i in col.indices:
        w.i32(i)
    return w.get()


def build_uemodel_bytes(m, version=None):
    """把 UEModel 序列化成字节。version=None 用 m.version。"""
    ver = _repair_version(int(version if version else m.version))
    w = _W()
    w.raw(MAGIC)
    w.fstr(m.identifier or IDENT_MODEL)
    w.u8(ver)
    w.fstr(m.name)
    if ver >= V_ATTR_RESTRUCTURE:
        w.fstr(m.path or "")
    w.bool(False)

    items = []
    lw = _W()
    if ver >= V_ATTR_RESTRUCTURE:
        lw.i32(len(m.lods))
    for lod in m.lods:
        lw.raw(_pack_lod(lod, ver))
    items.append(("LODS", len(m.lods), lw.get()))

    items.append(("SKELETON", 1, _pack_skeleton(m.skeleton, ver)))

    if m.collisions:
        cw = _W()
        if ver >= V_ATTR_RESTRUCTURE:
            cw.i32(len(m.collisions))
        for col in m.collisions:
            cw.raw(_pack_collision(col, ver))
        items.append(("COLLISION", len(m.collisions), cw.get()))

    w.raw(_pack_attrset(items, ver))
    return w.get()


def write_uemodel(m, path, version=None):
    data = build_uemodel_bytes(m, version=version)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return len(data)


# ----------------------------------------------------------------- 文本输出 --
def print_uemodel(m, src=""):
    if src:
        print("文件：%s" % src)
    print("UEFormat v%d · %s · 名称 %s" % (m.version, m.identifier, m.name))
    if m.path:
        print("  资源路径：%s" % m.path)
    sk = m.skeleton
    print("  骨骼 %d · 插槽 %d · 虚拟骨 %d · 碰撞 %d"
          % (len(sk.bones), len(sk.sockets), len(sk.virtual_bones),
             len(m.collisions)))
    if sk.path:
        print("  骨骼资源：%s" % sk.path)
    for i, lod in enumerate(m.lods):
        st = lod.stats()
        print("  LOD[%d] %-8s 顶点 %d · 三角面 %d · 材质 %d · 权重 %d · 表情 %d · UV %d"
              % (i, lod.name, st["verts"], st["tris"], st["mats"],
                 st["weights"], st["morphs"], st["uvsets"]))
        for mt in lod.materials[:40]:
            print("      材质 %-42s 面 %6d 起 %d"
                  % (mt.name[:42], mt.num_faces, mt.first_index))
        if len(lod.materials) > 40:
            print("      …另有 %d 个材质" % (len(lod.materials) - 40))
        for mo in lod.morphs[:40]:
            print("      表情 %-30s 顶点 %d" % (mo.name[:30], len(mo.deltas)))
        if len(lod.morphs) > 40:
            print("      …另有 %d 个表情" % (len(lod.morphs) - 40))
    if sk.bones:
        print("  前 12 根骨骼：")
        for b in sk.bones[:12]:
            print("      %-28s parent=%-4d pos=(%.3f, %.3f, %.3f)"
                  % (b.name[:28], b.parent, b.pos[0], b.pos[1], b.pos[2]))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("用法：python formats/uemodelio.py <model.uemodel> [--roundtrip]")
        return 1
    path = argv[0]
    m = read_uemodel(path, verbose=True)
    if "--roundtrip" in argv:
        for ver in (9, 10):
            tmp = os.path.join(os.path.dirname(os.path.abspath(path)), "_rt_v%d.uemodel" % ver)
            write_uemodel(m, tmp, version=ver)
            back = read_uemodel(tmp)
            ok = (len(back.lods) == len(m.lods)
                  and back.skeleton.bones and len(back.skeleton.bones) == len(m.skeleton.bones))
            print("往返 v%d：%d 字节 → %s" % (ver, os.path.getsize(tmp),
                                          "一致" if ok else "不一致"))
            os.remove(tmp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
