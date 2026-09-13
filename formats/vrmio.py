# -*- coding: utf-8 -*-
"""vrmio.py - GLB / glTF 容器读写 + 纯 Python 图片编解码。

只依赖标准库。提供：
    GLBBuilder   —— 构造 glTF JSON + 单一 BIN chunk
    read_glb     —— 拆开 .vrm/.glb 得到 (json dict, bin bytes)
    write_glb    —— 打包成 .vrm/.glb
    to_png / decode_image / save_image —— 贴图处理
"""
import json
import os
import struct
import zlib

GLB_MAGIC = 0x46546C67          # "glTF"
CHUNK_JSON = 0x4E4F534A         # "JSON"
CHUNK_BIN = 0x004E4942          # "BIN\0"

FLOAT = 5126
UNSIGNED_BYTE = 5121
SHORT = 5122
UNSIGNED_SHORT = 5123
UNSIGNED_INT = 5125

_COMP_FMT = {FLOAT: "<f", UNSIGNED_BYTE: "<B", SHORT: "<h",
             UNSIGNED_SHORT: "<H", UNSIGNED_INT: "<I"}
_COMP_SIZE = {FLOAT: 4, UNSIGNED_BYTE: 1, SHORT: 2,
              UNSIGNED_SHORT: 2, UNSIGNED_INT: 4}
_TYPE_COUNT = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4,
               "MAT4": 16}


# --------------------------------------------------------------- glb build --
class GLBBuilder:
    """边填数据边生成 bufferViews / accessors / images。"""

    def __init__(self):
        self.bin = bytearray()
        self.bufferViews = []
        self.accessors = []
        self.images = []            # (mime, bytes) 占位，稍后统一写入
        self.samplers = []
        self.textures = []

    # -- raw
    def add_bytes(self, data, target=None):
        while len(self.bin) % 4:
            self.bin.append(0)
        off = len(self.bin)
        self.bin += data
        bv = {"buffer": 0, "byteOffset": off, "byteLength": len(data)}
        if target:
            bv["target"] = target
        self.bufferViews.append(bv)
        return len(self.bufferViews) - 1

    # -- accessor
    def add_accessor(self, values, comp_type, type_str,
                     target=None, minmax=True):
        """values: 展平的 python 数字列表。"""
        n = _TYPE_COUNT[type_str]
        count = len(values) // n
        fmt = _COMP_FMT[comp_type]
        data = struct.pack("<%d%s" % (len(values), fmt[1:]), *values)
        bv = self.add_bytes(data, target)
        acc = {"bufferView": bv, "componentType": comp_type,
               "count": count, "type": type_str}
        if minmax and count:
            lo = [min(values[i::n]) for i in range(n)]
            hi = [max(values[i::n]) for i in range(n)]
            if comp_type == UNSIGNED_SHORT:
                acc["min"] = [int(x) & 0xFFFF for x in lo]
                acc["max"] = [int(x) & 0xFFFF for x in hi]
            else:
                acc["min"] = [float(x) for x in lo]
                acc["max"] = [float(x) for x in hi]
        self.accessors.append(acc)
        return len(self.accessors) - 1

    # -- image / texture
    def add_image(self, data, mime):
        bv = self.add_bytes(data)
        self.images.append({"bufferView": bv, "mimeType": mime})
        return len(self.images) - 1

    def add_texture(self, image_index, sampler=0):
        self.textures.append({"sampler": sampler, "source": image_index})
        return len(self.textures) - 1


def default_sampler():
    return {"magFilter": 9729, "minFilter": 9987,
            "wrapS": 10497, "wrapT": 10497}


def write_glb(gltf, bin_bytes, path):
    js = json.dumps(gltf, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    while len(js) % 4:
        js += b" "
    b = bytes(bin_bytes)
    while len(b) % 4:
        b += b"\x00"
    total = 12 + 8 + len(js) + (8 + len(b) if b else 0)
    out = bytearray()
    out += struct.pack("<III", GLB_MAGIC, 2, total)
    out += struct.pack("<II", len(js), CHUNK_JSON)
    out += js
    if b:
        out += struct.pack("<II", len(b), CHUNK_BIN)
        out += b
    with open(path, "wb") as f:
        f.write(out)
    return total


def read_glb(path):
    with open(path, "rb") as f:
        d = f.read()
    if len(d) < 20 or struct.unpack_from("<I", d, 0)[0] != GLB_MAGIC:
        raise ValueError("不是 GLB/VRM 文件：%s" % path)
    total = struct.unpack_from("<I", d, 8)[0]
    js = None
    bin_data = b""
    p = 12
    while p + 8 <= min(len(d), total):
        clen, ctype = struct.unpack_from("<II", d, p)
        p += 8
        chunk = d[p:p + clen]
        p += clen
        if ctype == CHUNK_JSON:
            js = json.loads(chunk.decode("utf-8"))
        elif ctype == CHUNK_BIN:
            bin_data = chunk
    if js is None:
        raise ValueError("GLB 里没有 JSON chunk")
    return js, bin_data


# ------------------------------------------------------------- gltf utils --
def read_accessor(gltf, bin_data, index):
    """把 accessor 读成展平的 python 数字列表，支持稀疏存储。"""
    acc = gltf["accessors"][index]
    return accessor_sparse(gltf, bin_data, index)


def accessor_sparse(gltf, bin_data, index):
    """返回 (values, sparse_pairs) —— 稀疏 accessor 支持。"""
    acc = gltf["accessors"][index]
    n = _TYPE_COUNT[acc["type"]]
    count = acc["count"]
    comp = acc["componentType"]
    fmt = _COMP_FMT[comp]
    size = _COMP_SIZE[comp]
    out = [0.0] * (count * n)
    if "bufferView" in acc:
        bv = gltf["bufferViews"][acc["bufferView"]]
        off = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
        stride = bv.get("byteStride") or (size * n)
        for i in range(count):
            base = off + i * stride
            for k in range(n):
                out[i * n + k] = struct.unpack_from(fmt, bin_data,
                                                    base + k * size)[0]
    sp = acc.get("sparse")
    if sp:
        icount = sp["count"]
        ibv = gltf["bufferViews"][sp["indices"]["bufferView"]]
        icomp = sp["indices"]["componentType"]
        ifmt = _COMP_FMT[icomp]
        isize = _COMP_SIZE[icomp]
        ioff = ibv.get("byteOffset", 0) + sp["indices"].get("byteOffset", 0)
        vbv = gltf["bufferViews"][sp["values"]["bufferView"]]
        voff = vbv.get("byteOffset", 0) + sp["values"].get("byteOffset", 0)
        for i in range(icount):
            vi = struct.unpack_from(ifmt, bin_data, ioff + i * isize)[0]
            for k in range(n):
                out[vi * n + k] = struct.unpack_from(
                    fmt, bin_data, voff + (i * n + k) * size)[0]
    return out


def node_world_matrices(gltf):
    """返回每个 node 的世界矩阵（列主序 4x4 列表）。"""
    nodes = gltf.get("nodes") or []
    n = len(nodes)
    world = [None] * n
    parent = [-1] * n
    for i, nd in enumerate(nodes):
        for c in nd.get("children") or []:
            if 0 <= c < n:
                parent[c] = i

    def local(nd):
        if "matrix" in nd:
            return list(nd["matrix"])
        t = nd.get("translation") or [0.0, 0.0, 0.0]
        r = nd.get("rotation") or [0.0, 0.0, 0.0, 1.0]
        s = nd.get("scale") or [1.0, 1.0, 1.0]
        x, y, z, w = r
        x2, y2, z2 = x + x, y + y, z + z
        xx, xy, xz = x * x2, x * y2, x * z2
        yy, yz, zz = y * y2, y * z2, z * z2
        wx, wy, wz = w * x2, w * y2, w * z2
        # 列主序
        return [s[0] * (1 - (yy + zz)), s[0] * (xy + wz), s[0] * (xz - wy), 0.0,
                s[1] * (xy - wz), s[1] * (1 - (xx + zz)), s[1] * (yz + wx), 0.0,
                s[2] * (xz + wy), s[2] * (yz - wx), s[2] * (1 - (xx + yy)), 0.0,
                t[0], t[1], t[2], 1.0]

    def mul(a, b):
        o = [0.0] * 16
        for c in range(4):
            for r_ in range(4):
                o[c * 4 + r_] = (a[r_] * b[c * 4] + a[4 + r_] * b[c * 4 + 1] +
                                 a[8 + r_] * b[c * 4 + 2] +
                                 a[12 + r_] * b[c * 4 + 3])
        return o

    order = sorted(range(n), key=lambda i: _depth(i, parent))
    for i in order:
        loc = local(nodes[i])
        p = parent[i]
        world[i] = mul(world[p], loc) if p >= 0 and world[p] else loc
    return world


def _depth(i, parent):
    d = 0
    while parent[i] >= 0:
        d += 1
        i = parent[i]
    return d


def world_translation(m):
    return (m[12], m[13], m[14])


# ----------------------------------------------------------------- images --
def encode_png(width, height, rows_rgba):
    """rows_rgba: 每行为 bytes，长度 width*4。"""
    raw = bytearray()
    for r in rows_rgba:
        raw.append(0)
        raw += r

    def chunk(typ, data):
        return (struct.pack(">I", len(data)) + typ + data +
                struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))

    out = bytearray(b"\x89PNG\r\n\x1a\n")
    out += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height,
                                      8, 6, 0, 0, 0))
    out += chunk(b"IDAT", zlib.compress(bytes(raw), 6))
    out += chunk(b"IEND", b"")
    return bytes(out)


def _png_from_rgba(w, h, pix):
    """pix: bytes，长度 w*h*4，按行自上而下。"""
    rows = [pix[y * w * 4:(y + 1) * w * 4] for y in range(h)]
    return encode_png(w, h, rows)


def _decode_bmp(data):
    if data[:2] != b"BM":
        return None
    off = struct.unpack_from("<I", data, 10)[0]
    dib = struct.unpack_from("<I", data, 14)[0]
    if dib < 40:
        return None
    w = struct.unpack_from("<i", data, 18)[0]
    h = struct.unpack_from("<i", data, 22)[0]
    bpp = struct.unpack_from("<H", data, 28)[0]
    comp = struct.unpack_from("<I", data, 30)[0]
    if bpp not in (24, 32) or comp not in (0, 3):
        return None
    flip = h > 0
    h = abs(h)
    stride = ((w * bpp + 31) // 32) * 4
    pix = bytearray(w * h * 4)
    src = off
    for y in range(h):
        sy = (h - 1 - y) if flip else y
        row = data[src + sy * stride: src + sy * stride + stride]
        for x in range(w):
            if bpp == 24:
                b, g, r = row[x * 3], row[x * 3 + 1], row[x * 3 + 2]
                a = 255
            else:
                b, g, r, a = row[x * 4], row[x * 4 + 1], row[x * 4 + 2], row[x * 4 + 3]
            o = (y * w + x) * 4
            pix[o] = r
            pix[o + 1] = g
            pix[o + 2] = b
            pix[o + 3] = a
    return _png_from_rgba(w, h, bytes(pix))


def _decode_tga(data):
    idlen = data[0]
    cmap_type = data[1]
    img_type = data[2]
    w = struct.unpack_from("<H", data, 12)[0]
    h = struct.unpack_from("<H", data, 14)[0]
    depth = data[16]
    desc = data[17]
    if img_type not in (2, 10) or depth not in (24, 32):
        return None
    p = 18 + idlen
    bpp = depth // 8
    pix = bytearray(w * h * 4)
    top = bool(desc & 0x20)
    idx = 0
    if img_type == 2:
        raw = data[p:p + w * h * bpp]
        for i in range(w * h):
            b = raw[i * bpp]
            g = raw[i * bpp + 1]
            r = raw[i * bpp + 2]
            a = raw[i * bpp + 3] if bpp == 4 else 255
            o = i * 4
            pix[o] = r
            pix[o + 1] = g
            pix[o + 2] = b
            pix[o + 3] = a
    else:
        while idx < w * h and p < len(data):
            rep = data[p]
            p += 1
            count = (rep & 0x7F) + 1
            if rep & 0x80:
                px = data[p:p + bpp]
                p += bpp
                chunkpx = [px] * count
            else:
                chunkpx = [data[p + i * bpp:p + (i + 1) * bpp]
                           for i in range(count)]
                p += count * bpp
            for px in chunkpx:
                if idx >= w * h:
                    break
                b, g, r = px[0], px[1], px[2]
                a = px[3] if bpp == 4 else 255
                o = idx * 4
                pix[o] = r
                pix[o + 1] = g
                pix[o + 2] = b
                pix[o + 3] = a
                idx += 1
    if not top:                                   # 底到顶存储，翻过来
        rows = [bytes(pix[y * w * 4:(y + 1) * w * 4]) for y in range(h)]
        rows.reverse()
        pix = bytearray(b"".join(rows))
    return _png_from_rgba(w, h, bytes(pix))


def decode_image(data):
    """任意常见贴图字节 → PNG 字节；无法处理时返回 None。"""
    if not data:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return data
    if data[:3] == b"\xff\xd8\xff":
        return data                                # JPEG 直接透传
    if data[:2] == b"BM":
        try:
            return _decode_bmp(data)
        except Exception:
            return None
    if len(data) > 18 and data[2] in (2, 3, 10, 11) and data[1] in (0, 1):
        try:
            return _decode_tga(data)
        except Exception:
            return None
    return None


def load_texture(path):
    """读取磁盘贴图，返回 (bytes, mime)；失败返回 (None, None)。"""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None, None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return data, "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return data, "image/jpeg"
    out = decode_image(data)
    if out is None:
        return None, None
    return out, ("image/png" if out[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg")


def save_image(data, path):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        p = os.path.splitext(path)[0] + ".png"
    elif data[:3] == b"\xff\xd8\xff":
        p = os.path.splitext(path)[0] + ".jpg"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        p = os.path.splitext(path)[0] + ".webp"
    elif data[:4] == b"\xabKTX":
        p = os.path.splitext(path)[0] + ".ktx2"
    else:
        p = path
    with open(p, "wb") as f:
        f.write(data)
    return p
