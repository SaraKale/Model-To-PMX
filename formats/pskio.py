# -*- coding: utf-8 -*-
"""PSK / PSKX 读取器（纯 Python 标准库，不需要 Blender / UE / FBX SDK）。

格式来源：Unreal Engine 的 ActorX 交换格式（.psk）及其扩展（.pskx）。
二进制布局以 DarklightGames/io_scene_psk_psa 的 psk_psa_py 为准，
并由本项目实测的样本文件（含 FACE3200 / VERTEXCOLOR / MRPHINFO / MRPHDATA
等扩展块）逐字段复核过。

文件结构
--------
整份文件是「块（section）」的线性序列，每个块：

    struct Section { char name[20]; int32 type_flags; int32 data_size;
                     int32 data_count; }        // 共 32 字节
    紧接 data_size * data_count 字节的数据

块名与记录布局（offset 都是相对记录开头）：

    PNTS0000    Vector3            12  x,y,z               顶点（绑定姿势，物体空间）
    VTXW0000    Wedge16 / Wedge32  16  uint32 point_index; float u, v;
                                       uint32 material_index
    FACE0000    Face16             12  uint16 i[3]; uint8 mat; uint8 aux;
                                       int32 smoothing
    FACE3200    Face32             18  uint32 i[3]; uint8 mat; uint8 aux;
                                       int32 smoothing（紧凑排列）
    MATT0000    Material           88  char name[64]; int32 ×6
    REFSKELT    Bone              120  char name[64]; int32 flags;
                                       int32 children_count; int32 parent_index;
                                       Quat rotation(x,y,z,w); Vector3 location;
                                       float length; Vector3 size
    RAWWEIGHTS  Weight             12  float weight; int32 point_index;
                                       int32 bone_index
    VTXNORMS    Vector3            12  顶点法线
    VERTEXCOLOR Color               4  r,g,b,a（各 1 字节）
    EXTRAUVS*   Vector2             8  附加 UV（pskx，可有多套）
    MRPHINFO    MorphInfo          68  char name[64]; int32 vertex_count
    MRPHDATA    MorphData          28  Vector3 position_delta;
                                       Vector3 tangent_z_delta; int32 point_index

两个实测坑（都已处理）：
1) 部分游戏导出的文件在真正的 PSK 数据前面多写了 32 字节外层头（又是一个
   ACTRHEAD，type_flags 位置放的是版本号 20220723）。按块序列顺序读即可自然
   跳过：外层 ACTRHEAD 的 data_count=0，读完就落到第二个 ACTRHEAD 上。
2) 四元数要取共轭（(x,y,z,w) → 取 (x,y,z) 的负号）后才是引擎常用右手系下
   的旋转；不共轭的话骨骼世界坐标会整条链歪出去（实测头骨能偏 30 多个单位）。
"""
import struct

__all__ = ["Psk", "PskBone", "PskMaterial", "PskMorph", "read_psk",
           "bone_world", "quat_to_matrix"]

# ------------------------------------------------------------------ data ---
class PskBone(object):
    __slots__ = ("name", "flags", "children_count", "parent_index",
                 "rotation", "location", "length", "size")

    def __init__(self, name, flags, children_count, parent_index,
                 rotation, location, length, size):
        self.name = name
        self.flags = flags
        self.children_count = children_count
        self.parent_index = parent_index
        self.rotation = rotation      # (x, y, z, w)
        self.location = location      # (x, y, z)
        self.length = length
        self.size = size              # (x, y, z)


class PskMaterial(object):
    __slots__ = ("name", "texture_index", "poly_flags", "aux_material",
                 "aux_flags", "lod_bias", "lod_style")

    def __init__(self, name, texture_index, poly_flags, aux_material,
                 aux_flags, lod_bias, lod_style):
        self.name = name
        self.texture_index = texture_index
        self.poly_flags = poly_flags
        self.aux_material = aux_material
        self.aux_flags = aux_flags
        self.lod_bias = lod_bias
        self.lod_style = lod_style


class PskMorph(object):
    """一个表情目标：名字 + 顶点位移列表 [(point_index, (dx,dy,dz)), ...]。"""
    __slots__ = ("name", "offsets")

    def __init__(self, name, offsets):
        self.name = name
        self.offsets = offsets


class Psk(object):
    def __init__(self):
        self.points = []            # [(x,y,z), ...]
        self.wedges = []            # [(point_index, u, v, material_index), ...]
        self.faces = []             # [((i0,i1,i2), material_index), ...]
        self.materials = []         # [PskMaterial, ...]
        self.bones = []             # [PskBone, ...]
        self.weights = []           # [(weight, point_index, bone_index), ...]
        self.normals = []           # [(x,y,z), ...]
        self.colors = []            # [(r,g,b,a), ...]
        self.extra_uvs = []         # [[(u,v), ...], ...]
        self.morphs = []            # [PskMorph, ...]
        self.face32 = False         # 索引用的是 FACE3200（32 位）还是 FACE0000

    @property
    def has_normals(self):
        return bool(self.normals)

    @property
    def has_morphs(self):
        return bool(self.morphs)


# ------------------------------------------------------------------ math ---
def quat_to_matrix(q):
    """(x, y, z, w) → 3×3 旋转矩阵。

    列向量约定：v' = M · v（与 bone_world / _mvec / _mmul 一致，别写成转置）。
    校验：绕 Z 轴 +90° 的四元数 (0,0,0.7071,0.7071) 应把 (1,0,0) 变成 (0,1,0)。
    """
    x, y, z, w = q
    return ((1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)),
            (2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)),
            (2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)))


def _mmul(a, b):
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3))
                       for j in range(3)) for i in range(3))


def _mvec(m, v):
    """列向量：M · v。"""
    return tuple(sum(m[i][k] * v[k] for k in range(3)) for i in range(3))


def bone_world(psk):
    """算每根骨骼的世界（物体空间）旋转矩阵与位置。

    PSK 的 rotation/location 都是**相对父骨骼**的局部值，世界值沿父链累乘。
    四元数先取共轭再转成矩阵（见文件头注释第 2 点）。

    返回 (world_rot, world_pos)，都是按骨骼下标索引的列表。
    """
    bones = psk.bones
    n = len(bones)
    wrot = [None] * n
    wpos = [None] * n
    state = [0] * n                      # 0 未算 / 1 计算中 / 2 已完成

    def calc(i, depth=0):
        if state[i] == 2 or depth > 512:
            return
        if state[i] == 1:                # 环：当成根，别死循环
            x, y, z, w = bones[i].rotation
            wrot[i] = quat_to_matrix((-x, -y, -z, w))
            wpos[i] = tuple(bones[i].location)
            state[i] = 2
            return
        state[i] = 1
        b = bones[i]
        x, y, z, w = b.rotation
        r = quat_to_matrix((-x, -y, -z, w))
        p = b.parent_index
        if p < 0 or p >= n or p == i:
            wrot[i] = r
            wpos[i] = tuple(b.location)
        else:
            calc(p, depth + 1)
            pr, pt = wrot[p], wpos[p]
            if pr is None:
                wrot[i] = r
                wpos[i] = tuple(b.location)
            else:
                wrot[i] = _mmul(pr, r)
                t = _mvec(pr, b.location)
                wpos[i] = (pt[0] + t[0], pt[1] + t[1], pt[2] + t[2])
        state[i] = 2

    for i in range(n):
        calc(i)
    for i in range(n):
        if wrot[i] is None:
            x, y, z, w = bones[i].rotation
            wrot[i] = quat_to_matrix((-x, -y, -z, w))
            wpos[i] = tuple(bones[i].location)
    return wrot, wpos


# ----------------------------------------------------------------- reader ---
def _cstr(raw):
    return raw.split(b"\x00")[0].decode("ascii", "replace")


def read_psk(path):
    """读 .psk / .pskx，返回 Psk。"""
    with open(path, "rb") as fp:
        data = fp.read()
    psk = Psk()
    off = 0
    total = len(data)
    morph_infos = []                     # [(name, vertex_count), ...]
    morph_raw = []                       # [(delta, tangent_z, point_index), ...]

    while off + 32 <= total:
        name = data[off:off + 20].split(b"\x00")[0]
        if not name or not all(32 < c < 127 for c in name):
            break                        # 不是块头了（尾部填充 / 未知数据）
        try:
            _flag, dsize, dcount = struct.unpack_from("<iii", data, off + 20)
        except struct.error:
            break
        body = off + 32
        nbytes = dsize * dcount
        # 注意：ACTRHEAD 这类「空块」的 data_size/data_count 都是 0，不能因此
        # 判为块头不可信就中断，否则一上来就什么都读不到。
        if dsize < 0 or dcount < 0 or body + nbytes > total:
            break                        # 块头不可信，停在这里
        nm = name.decode("ascii")

        if nm == "PNTS0000" and dsize >= 12:
            psk.points = [struct.unpack_from("<3f", data, body + i * dsize)
                          for i in range(dcount)]
        elif nm == "VTXW0000":
            if dsize >= 16:
                psk.wedges = [struct.unpack_from("<IffI", data, body + i * dsize)
                              for i in range(dcount)]
            elif dsize >= 12:            # 老式 16 位 point_index
                for i in range(dcount):
                    pi = struct.unpack_from("<H", data, body + i * dsize)[0]
                    u, v = struct.unpack_from("<ff", data, body + i * dsize + 4)
                    psk.wedges.append((pi, u, v, 0))
        elif nm == "FACE0000" and dsize >= 12:
            for i in range(dcount):
                o = body + i * dsize
                a, b, c = struct.unpack_from("<3H", data, o)
                psk.faces.append(((a, b, c), data[o + 6]))
            psk.face32 = False
        elif nm == "FACE3200" and dsize >= 18:
            for i in range(dcount):
                o = body + i * dsize
                a, b, c = struct.unpack_from("<3I", data, o)
                psk.faces.append(((a, b, c), data[o + 12]))
            psk.face32 = True
        elif nm == "MATT0000" and dsize >= 88:
            for i in range(dcount):
                o = body + i * dsize
                psk.materials.append(PskMaterial(
                    _cstr(data[o:o + 64]),
                    *struct.unpack_from("<6i", data, o + 64)))
        elif nm == "REFSKELT" and dsize >= 120:
            for i in range(dcount):
                o = body + i * dsize
                psk.bones.append(PskBone(
                    _cstr(data[o:o + 64]),
                    *struct.unpack_from("<3i", data, o + 64),
                    rotation=struct.unpack_from("<4f", data, o + 76),
                    location=struct.unpack_from("<3f", data, o + 92),
                    length=struct.unpack_from("<f", data, o + 104)[0],
                    size=struct.unpack_from("<3f", data, o + 108)))
        elif nm == "RAWWEIGHTS" and dsize >= 12:
            psk.weights = [struct.unpack_from("<fii", data, body + i * dsize)
                           for i in range(dcount)]
        elif nm == "VTXNORMS" and dsize >= 12:
            psk.normals = [struct.unpack_from("<3f", data, body + i * dsize)
                           for i in range(dcount)]
        elif nm == "VERTEXCOLOR" and dsize >= 4:
            psk.colors = [tuple(data[body + i * dsize: body + i * dsize + 4])
                          for i in range(dcount)]
        elif nm == "MRPHINFO" and dsize >= 68:
            for i in range(dcount):
                o = body + i * dsize
                morph_infos.append((_cstr(data[o:o + 64]),
                                    struct.unpack_from("<i", data, o + 64)[0]))
        elif nm == "MRPHDATA" and dsize >= 28:
            for i in range(dcount):
                o = body + i * dsize
                morph_raw.append((struct.unpack_from("<3f", data, o),
                                  struct.unpack_from("<3f", data, o + 12),
                                  struct.unpack_from("<i", data, o + 24)[0]))
        elif nm.startswith("EXTRAUVS") and dsize >= 8:
            psk.extra_uvs.append([struct.unpack_from("<2f", data,
                                                     body + i * dsize)
                                  for i in range(dcount)])
        # 其余块（含 ACTRHEAD）直接按大小跳过
        off = body + nbytes

    # 16 位 point_index 的老工具会把高 16 位写成垃圾；点数还装得下 16 位时
    # 统一截掉高位（与 psk_psa_py 的 read_psk 同口径）。
    if psk.points and len(psk.points) <= 65536:
        psk.wedges = [(w[0] & 0xFFFF, w[1], w[2], w[3]) for w in psk.wedges]

    # MRPHDATA 是按 MRPHINFO 的顺序连续排布的：第 i 个表情吃前 vertex_count 条。
    if morph_infos and morph_raw:
        pos = 0
        for name, cnt in morph_infos:
            chunk = morph_raw[pos:pos + max(0, cnt)]
            pos += max(0, cnt)
            offs = [(pi, d) for d, _tz, pi in chunk
                    if abs(d[0]) + abs(d[1]) + abs(d[2]) > 1e-9]
            if offs:
                psk.morphs.append(PskMorph(name, offs))
        if pos < len(morph_raw):        # 名字数和数据条数对不上，兜底塞成一个
            rest = [(pi, d) for d, _tz, pi in morph_raw[pos:]
                    if abs(d[0]) + abs(d[1]) + abs(d[2]) > 1e-9]
            if rest:
                psk.morphs.append(PskMorph("morph_rest", rest))
    return psk
