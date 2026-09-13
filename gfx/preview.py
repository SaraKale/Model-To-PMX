# -*- coding: utf-8 -*-
"""preview.py - 全格式模型实时预览（纯 Python 标准库）。

把 .pmx / .vrm / .fbx / .unitypackage 读成统一的“预览网格”（顶点 / 三角面 /
逐面颜色 / 骨骼），在 tkinter 画布里做**背视图**实时交互预览（默认即背视图，
也可拖动旋转 / 滚轮缩放 / 右键平移 / 一键复位查看其它角度）。

预览**只实时显示、不落盘保存图片**；如需把模型渲染成 PNG，请用命令行
render_to_png / `python preview.py "model.pmx" -o out.png`（独立于实时预览）。

不依赖 numpy / PIL / OpenGL，只用到 tkinter + zlib + struct。

主要接口：
    load_preview(path, kind=None, log=None)  -> Mesh
    Preview3D(master, size=360, ...)         # tkinter 实时预览控件（背视图）
    render_to_png(mesh, path, size=560, ...) # 仅命令行离屏渲染用，GUI 预览不调用

命令行（独立导出，不影响实时预览）：
    python preview.py "model.pmx" -o preview.png --yaw 180 --pitch 0
"""
import io
import math
import os
import struct
import sys
import zlib

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import pmx_check          # PMX 读取（纯标准库）
import vrmio              # GLB / glTF 读取 + 贴图解码

try:
    import tkinter as tk
except Exception:                                    # pragma: no cover
    tk = None

# 可选加速：检测到 Pillow 时，贴图解码走 C 实现（约快 40 倍），
# 否则退回到下面的纯 Python 解码器，保证无第三方依赖也能运行。
try:
    from PIL import Image as _PIL_Image
except Exception:                                    # pragma: no cover
    _PIL_Image = None

# 预览贴图分辨率上限：预览框很小，2048/4096 的贴图没必要全分辨率解码，
# 既省内存又避免渲染时逐像素采样大图。
PREVIEW_TEX_MAX = 1024

# 默认预览视角：背视图（yaw=180°，朝模型背面看）。实时预览只显示不保存。
DEFAULT_YAW = math.radians(180.0)
DEFAULT_PITCH = 0.0


# ----------------------------------------------------------------- 贴图解码 --
class Image:
    """RGBA8 图像，带双线性/最近邻取样。"""

    __slots__ = ("w", "h", "px")

    def __init__(self, w, h, px):
        self.w = w
        self.h = h
        self.px = px                                 # bytearray, w*h*4

    def at(self, x, y):
        if x < 0 or y < 0 or x >= self.w or y >= self.h:
            return (255, 255, 255, 255)
        o = (y * self.w + x) * 4
        p = self.px
        return (p[o], p[o + 1], p[o + 2], p[o + 3])

    def sample(self, u, v):
        """最近的 texel；UV 越界按重复（wrap）处理。MMD/glTF 的 V 轴向下。"""
        w, h = self.w, self.h
        if w <= 0 or h <= 0:
            return (255, 255, 255, 255)
        u = u - math.floor(u)
        v = v - math.floor(v)
        x = min(w - 1, int(u * w))
        y = min(h - 1, int(v * h))
        return self.at(x, y)


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _png_pixels(data, max_size=None):
    """解码 PNG → Image；不支持时返回 None。

    优先用 Pillow（C 实现，极快）；失败或无 Pillow 时走纯 Python 兜底。
    max_size 指定时把贴图缩到不超过该边长（预览框很小，大贴图没必要全分辨率）。
    """
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None

    if _PIL_Image is not None:
        try:
            im = _PIL_Image.open(io.BytesIO(data))
            im.load()
            if im.mode != "RGBA":
                im = im.convert("RGBA")
            w, h = im.width, im.height
            if max_size and max(w, h) > max_size:
                s = max_size / float(max(w, h))
                im = im.resize((max(1, int(w * s + 0.5)),
                                max(1, int(h * s + 0.5))),
                               _PIL_Image.LANCZOS)
            px = im.tobytes()
            return Image(im.width, im.height, bytearray(px))
        except Exception:
            pass

    p = 8
    w = h = bitd = ctype = interlace = None
    plte = None
    trns = None
    idat = bytearray()
    while p + 8 <= len(data):
        ln = struct.unpack_from(">I", data, p)[0]
        typ = data[p + 4:p + 8]
        body = data[p + 8:p + 8 + ln]
        p += 12 + ln
        if typ == b"IHDR":
            w, h, bitd, ctype, _comp, _filt, interlace = struct.unpack(">IIBBBBB", body)
        elif typ == b"PLTE":
            plte = body
        elif typ == b"tRNS":
            trns = body
        elif typ == b"IDAT":
            idat += body
        elif typ == b"IEND":
            break
    if w is None or h is None or w == 0 or h == 0:
        return None
    if bitd not in (1, 2, 4, 8, 16) or ctype not in (0, 2, 3, 4, 6):
        return None
    try:
        raw = zlib.decompress(bytes(idat))
    except Exception:
        return None

    chan = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    depth_bytes = max(1, (bitd + 7) // 8)
    bpp = max(1, chan * depth_bytes)

    def chunk_rows(buf, rw, rh):
        """反滤波并展开到 [(r,g,b,a)]，返回扁平列表。"""
        rows = []
        stride = (rw * chan * bitd + 7) // 8
        out = []
        prev = bytearray(stride)
        pos = 0
        for _y in range(rh):
            ft = buf[pos]
            pos += 1
            line = bytearray(buf[pos:pos + stride])
            pos += stride
            if ft == 1:
                for i in range(bpp, stride):
                    line[i] = (line[i] + line[i - bpp]) & 0xFF
            elif ft == 2:
                for i in range(stride):
                    line[i] = (line[i] + prev[i]) & 0xFF
            elif ft == 3:
                for i in range(stride):
                    a = line[i - bpp] if i >= bpp else 0
                    line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
            elif ft == 4:
                for i in range(stride):
                    a = line[i - bpp] if i >= bpp else 0
                    c = prev[i - bpp] if i >= bpp else 0
                    line[i] = (line[i] + _paeth(a, prev[i], c)) & 0xFF
            out.append(bytes(line))
            prev = line
        return out

    def to_rgba(rows, rw, rh):
        px = bytearray(rw * rh * 4)
        for y in range(rh):
            line = rows[y]
            base = y * rw * 4
            if ctype == 3:                                  # 调色板
                for x in range(rw):
                    if bitd == 8:
                        idx = line[x]
                    elif bitd == 4:
                        b = line[x >> 1]
                        idx = (b >> 4) if (x & 1) == 0 else (b & 0x0F)
                    elif bitd == 2:
                        b = line[x >> 2]
                        idx = (b >> (6 - 2 * (x & 3))) & 0x03
                    else:
                        b = line[x >> 3]
                        idx = (b >> (7 - (x & 7))) & 0x01
                    if plte and idx * 3 + 2 < len(plte):
                        r, g, bl = plte[idx * 3], plte[idx * 3 + 1], plte[idx * 3 + 2]
                    else:
                        r = g = bl = 0
                    a = 255
                    if trns and idx < len(trns):
                        a = trns[idx]
                    o = base + x * 4
                    px[o], px[o + 1], px[o + 2], px[o + 3] = r, g, bl, a
                continue
            if bitd == 16:
                step = chan * 2
                for x in range(rw):
                    vals = [line[x * step + k * 2] for k in range(chan)]
                    px[base + x * 4:base + x * 4 + 4] = _rgba_of(vals, ctype, 255)
                continue
            if bitd < 8:
                mx = (1 << bitd) - 1
                for x in range(rw):
                    b = line[x >> 3]
                    shift = 8 - bitd - (x & (8 // bitd)) * bitd
                    g = ((b >> shift) & mx) * 255 // mx
                    o = base + x * 4
                    px[o] = px[o + 1] = px[o + 2] = g
                    px[o + 3] = 255
                continue
            step = chan
            for x in range(rw):
                vals = line[x * step:x * step + step]
                px[base + x * 4:base + x * 4 + 4] = _rgba_of(vals, ctype, 255)
        return px

    if not interlace:
        rows = chunk_rows(raw, w, h)
        return _downscale(Image(w, h, to_rgba(rows, w, h)), max_size)

    # ---- Adam7 交错
    steps = _adam7_steps(w, h)
    px = bytearray(w * h * 4)
    pos = 0
    for (x0, y0, dx, dy) in steps:
        pw = (w - x0 + dx - 1) // dx
        ph = (h - y0 + dy - 1) // dy
        if pw <= 0 or ph <= 0:
            continue
        need = ph * (1 + ((pw * chan * bitd + 7) // 8))
        rows = chunk_rows(raw[pos:pos + need], pw, ph)
        pos += need
        sub = to_rgba(rows, pw, ph)
        for yy in range(ph):
            for xx in range(pw):
                sx = x0 + xx * dx
                sy = y0 + yy * dy
                o = (sy * w + sx) * 4
                so = (yy * pw + xx) * 4
                px[o:o + 4] = sub[so:so + 4]
    return _downscale(Image(w, h, px), max_size)


def _rgba_of(vals, ctype, amax):
    if ctype == 0:
        g = vals[0]
        return bytes((g, g, g, 255))
    if ctype == 2:
        return bytes((vals[0], vals[1], vals[2], 255))
    if ctype == 4:
        return bytes((vals[0], vals[0], vals[0], vals[1]))
    if ctype == 6:
        return bytes((vals[0], vals[1], vals[2], vals[3]))
    return bytes((255, 255, 255, 255))


def _adam7_steps(w, h):
    return [(0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
            (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2)]


def _downscale(img, max_size):
    """纯 Python 兜底：超过 max_size 时做 Box 下采样（仅在无 Pillow 时才会触发）。"""
    if not max_size:
        return img
    w, h = img.w, img.h
    if max(w, h) <= max_size:
        return img
    s = max_size / float(max(w, h))
    nw = max(1, int(w * s + 0.5))
    nh = max(1, int(h * s + 0.5))
    out = bytearray(nw * nh * 4)
    px = img.px
    sw = nw / float(w)
    sh = nh / float(h)
    for y in range(nh):
        sy0 = int(y / sh)
        sy1 = max(sy0 + 1, int((y + 1) / sh))
        ob = y * nw * 4
        for x in range(nw):
            sx0 = int(x / sw)
            sx1 = max(sx0 + 1, int((x + 1) / sw))
            r = g = b = a = cnt = 0
            for yy in range(sy0, sy1):
                base = yy * w * 4
                for xx in range(sx0, sx1):
                    o = base + xx * 4
                    r += px[o]; g += px[o + 1]; b += px[o + 2]; a += px[o + 3]
                    cnt += 1
            o2 = ob + x * 4
            if cnt:
                out[o2] = r // cnt; out[o2 + 1] = g // cnt
                out[o2 + 2] = b // cnt; out[o2 + 3] = a // cnt
    return Image(nw, nh, out)


def load_image(path_or_bytes, max_size=PREVIEW_TEX_MAX):
    """读磁盘贴图或字节流 → Image；失败返回 None。"""
    if isinstance(path_or_bytes, (bytes, bytearray)):
        data = bytes(path_or_bytes)
    else:
        try:
            with open(path_or_bytes, "rb") as f:
                data = f.read()
        except OSError:
            return None
    if not data:
        return None
    # 优先 Pillow：PNG/BMP/TGA 等都能直接解，C 实现极快
    if _PIL_Image is not None:
        try:
            im = _PIL_Image.open(io.BytesIO(data))
            im.load()
            if im.mode != "RGBA":
                im = im.convert("RGBA")
            w, h = im.width, im.height
            if max_size and max(w, h) > max_size:
                s = max_size / float(max(w, h))
                im = im.resize((max(1, int(w * s + 0.5)),
                                max(1, int(h * s + 0.5))),
                               _PIL_Image.LANCZOS)
            return Image(im.width, im.height, bytearray(im.tobytes()))
        except Exception:
            pass
    # 纯 Python 兜底
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        try:
            return _png_pixels(data, max_size)
        except Exception:
            return None
    # BMP / TGA：交给 vrmio 转成 PNG 再解码
    try:
        png = vrmio.decode_image(data)
    except Exception:
        png = None
    if png and png[:8] == b"\x89PNG\r\n\x1a\n":
        try:
            return _png_pixels(png, max_size)
        except Exception:
            return None
    return None


# --------------------------------------------------------------- 预览网格 ----
class Mesh:
    """与格式无关的预览网格。"""

    def __init__(self, name="model"):
        self.name = name
        self.verts = []          # [(x, y, z)]
        self.uv = []             # [(u, v)] 与 verts 等长
        self.tris = []           # [(i0, i1, i2)]
        self.face_rgb = []       # 每面颜色 (r, g, b)
        self.face_tex = []       # 每面贴图 Image（或 None）
        self.face_col = []       # 每面基色 (r, g, b)
        self.bones = []          # [(x, y, z)]
        self.materials = []      # 原始材质信息
        self._bbox = None
        self._tex_state = None  # 渐进式贴图：延迟解码用的素材，解码后置 None

    # -- 渐进式贴图：先以基色出背视图，后台再解码贴图并重绘
    def load_textures(self):
        """把延迟的贴图解码出来并重新计算逐面颜色；无待解码时返回 False。"""
        st = self._tex_state
        if not st:
            return False
        kind = st["kind"]
        if kind == "pmx":
            m = pmx_check.read_pmx(st["path"])
            folder = os.path.dirname(os.path.abspath(st["path"]))
            search = [folder, os.path.join(folder, "textures")]
            tex_cache = {}
            mats = []
            for mat in m["materials"]:
                d = mat.get("diffuse") or (1.0, 1.0, 1.0, 1.0)
                col = (max(0, min(255, int(d[0] * 255))),
                       max(0, min(255, int(d[1] * 255))),
                       max(0, min(255, int(d[2] * 255))))
                tex_i = mat.get("tex", -1)
                img = None
                if isinstance(tex_i, int) and 0 <= tex_i < len(m["textures"]):
                    rel = m["textures"][tex_i]
                    img = tex_cache.get(rel)
                    if img is None:
                        img = _tex_of(search, rel)
                        tex_cache[rel] = img
                mats.append({"color": col, "tex": img, "name": mat.get("name", "")})
            _resolve_face_colors(self, st["face_mat"], mats)
        elif kind == "vrm":
            gltf = st["gltf"]
            bin_data = st["bin"]
            mat_cache = st["mat_cache"]
            face_mat = st["face_mat"]
            img_cache = st.get("img_cache") or {}
            g_mats = gltf.get("materials") or []
            texs = gltf.get("textures") or []
            for i, mc in enumerate(mat_cache):
                gm = g_mats[i] if i < len(g_mats) else None
                if gm is None:
                    continue
                pbr = gm.get("pbrMetallicRoughness") or {}
                bt = (pbr.get("baseColorTexture") or {}).get("index")
                if bt is None:
                    continue
                tex = texs[bt] if 0 <= bt < len(texs) else {}
                mc["tex"] = _mat_image(gltf, bin_data, tex.get("source"), img_cache)
            _resolve_face_colors(self, face_mat, mat_cache)
        self._tex_state = None
        return True

    # -- 统计
    def stats(self):
        return {"verts": len(self.verts), "tris": len(self.tris),
                "mats": len(self.materials), "bones": len(self.bones)}

    def bbox(self):
        if self._bbox is not None:
            return self._bbox
        if not self.verts:
            self._bbox = ((0.0, 0.0, 0.0), 1.0)
            return self._bbox
        xs = [v[0] for v in self.verts]
        ys = [v[1] for v in self.verts]
        zs = [v[2] for v in self.verts]
        cx = (min(xs) + max(xs)) / 2.0
        cy = (min(ys) + max(ys)) / 2.0
        cz = (min(zs) + max(zs)) / 2.0
        span = max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs))
        self._bbox = ((cx, cy, cz), (span or 1.0))
        return self._bbox

    def finalize(self):
        """按材质/贴图求出每个三角面的颜色。"""
        n = len(self.tris)
        if len(self.face_rgb) == n:
            return
        self.face_rgb = [(210, 205, 200)] * n


def _resolve_face_colors(mesh, face_mat, mats):
    """face_mat: 每个三角面的材质下标；mats: [{color, tex}]。

    同时产出：
        face_rgb  —— 每面的代表色（实时预览用，取贴图重心色）
        face_tex  —— 每面的贴图对象（离屏渲染时逐像素采样，最接近原图）
        face_col  —— 每面的基色
    """
    colors = []
    texes = []
    cols = []
    for ti, tri in enumerate(mesh.tris):
        mi = face_mat[ti] if ti < len(face_mat) else 0
        mat = mats[mi] if 0 <= mi < len(mats) else None
        if mat is None:
            colors.append((210, 205, 200))
            texes.append(None)
            cols.append((210, 205, 200))
            continue
        r, g, b = mat.get("color", (210, 205, 200))
        tex = mat.get("tex")
        cols.append((r, g, b))
        texes.append(tex)
        if tex is not None:
            # 用三角面重心 UV 取样，保证颜色和贴图一致（“像图片一样”）
            u = v = 0.0
            for k in tri:
                u += mesh.uv[k][0]
                v += mesh.uv[k][1]
            u /= 3.0
            v /= 3.0
            tr, tg, tb, _a = tex.sample(u, v)
            colors.append((r * tr // 255, g * tg // 255, b * tb // 255))
        else:
            colors.append((r, g, b))
    mesh.face_rgb = colors
    mesh.face_tex = texes
    mesh.face_col = cols


def _tex_of(search_dirs, rel):
    """在若干目录里按相对路径 / 文件名找贴图并解码。"""
    if not rel:
        return None
    rel = rel.replace("\\", "/")
    base = os.path.basename(rel)
    cands = []
    for d in search_dirs:
        cands.append(os.path.join(d, rel))
        cands.append(os.path.join(d, base))
        cands.append(os.path.join(d, "textures", base))
    for p in cands:
        if os.path.isfile(p):
            img = load_image(p)
            if img is not None:
                return img
    return None


# ------------------------------------------------------------------- PMX -----
def mesh_from_pmx(path, log=None, with_textures=True):
    m = pmx_check.read_pmx(path)
    mesh = Mesh(os.path.splitext(os.path.basename(path))[0])
    search = [os.path.dirname(os.path.abspath(path)),
              os.path.join(os.path.dirname(os.path.abspath(path)), "textures")]
    tex_cache = {}
    mats = []
    for mat in m["materials"]:
        d = mat.get("diffuse") or (1.0, 1.0, 1.0, 1.0)
        col = (max(0, min(255, int(d[0] * 255))),
               max(0, min(255, int(d[1] * 255))),
               max(0, min(255, int(d[2] * 255))))
        tex_i = mat.get("tex", -1)
        img = None
        if with_textures and isinstance(tex_i, int) and 0 <= tex_i < len(m["textures"]):
            rel = m["textures"][tex_i]
            if rel in tex_cache:
                img = tex_cache[rel]
            else:
                img = _tex_of(search, rel)
                tex_cache[rel] = img
        mats.append({"color": col, "tex": img, "name": mat.get("name", "")})
    mesh.materials = mats

    for v in m["vertices"]:
        mesh.verts.append(v[0])
        mesh.uv.append(v[2])

    # 面 → 材质（PMX 每个材质覆盖连续的索引段）
    faces = m["faces"]
    face_mat = []
    idx = 0
    for mi, mat in enumerate(m["materials"]):
        cnt = mat.get("faces", 0)
        for _ in range(cnt // 3):
            face_mat.append(mi)
        idx += cnt
    # 兜底：材质段对不上时补齐
    ntris = len(faces) // 3
    if len(face_mat) < ntris:
        face_mat.extend([0] * (ntris - len(face_mat)))

    for t in range(0, len(faces) - 2, 3):
        mesh.tris.append((faces[t], faces[t + 1], faces[t + 2]))
    mesh.bones = [b["pos"] for b in m["bones"]]

    if with_textures:
        mesh._tex_state = None
    else:
        mesh._tex_state = {"kind": "pmx", "path": path, "face_mat": face_mat}

    _resolve_face_colors(mesh, face_mat, mats)
    return mesh


# ------------------------------------------------------------------- VRM -----
def _vrm_is10(gltf):
    if gltf.get("extensions", {}).get("VRMC_vrm"):
        return True
    if "extensions" in gltf and "VRM" in gltf["extensions"]:
        return False
    return False


def _mat_image(gltf, bin_data, img_index, cache):
    if img_index is None or img_index < 0:
        return None
    if img_index in cache:
        return cache[img_index]
    img = None
    try:
        node = (gltf.get("images") or [])[img_index]
        data = None
        if "bufferView" in node:
            bv = gltf["bufferViews"][node["bufferView"]]
            off = bv.get("byteOffset", 0)
            data = bin_data[off:off + bv["byteLength"]]
        elif str(node.get("uri", "")).startswith("data:"):
            import base64
            data = base64.b64decode(node["uri"].split(",", 1)[1])
        if data:
            img = load_image(data)
    except Exception:
        img = None
    cache[img_index] = img
    return img


def mesh_from_vrm(path, log=None, with_textures=True):
    gltf, bin_data = vrmio.read_glb(path)
    mesh = Mesh(os.path.splitext(os.path.basename(path))[0])
    is1 = _vrm_is10(gltf)
    y180 = is1                                  # 与 vrm2pmx 的朝向约定保持一致

    def xf(p):
        x, y, z = p
        if y180:
            x, z = -x, -z
        return (x, y, z)

    worlds = vrmio.node_world_matrices(gltf)
    nodes = gltf.get("nodes") or []
    meshes = gltf.get("meshes") or []
    g_mats = gltf.get("materials") or []
    img_cache = {}
    mat_cache = []
    for gm in g_mats:
        pbr = gm.get("pbrMetallicRoughness") or {}
        f = pbr.get("baseColorFactor") or [1.0, 1.0, 1.0, 1.0]
        col = (max(0, min(255, int(f[0] * 255))),
               max(0, min(255, int(f[1] * 255))),
               max(0, min(255, int(f[2] * 255))))
        img = None
        bt = (pbr.get("baseColorTexture") or {}).get("index")
        if with_textures and bt is not None:
            tex = (gltf.get("textures") or [])[bt] if bt < len(gltf.get("textures") or []) else {}
            img = _mat_image(gltf, bin_data, tex.get("source"), img_cache)
        mat_cache.append({"color": col, "tex": img, "name": gm.get("name", "")})
    mesh.materials = mat_cache

    face_mat = []
    for node_i, node in enumerate(nodes):
        if "mesh" not in node:
            continue
        w = worlds[node_i] if node_i < len(worlds) and worlds[node_i] else None
        mi = node["mesh"]
        if mi >= len(meshes):
            continue
        for prim in meshes[mi].get("primitives") or []:
            attrs = prim.get("attributes") or {}
            if "POSITION" not in attrs:
                continue
            pos = vrmio.read_accessor(gltf, bin_data, attrs["POSITION"])
            uv = (vrmio.read_accessor(gltf, bin_data, attrs["TEXCOORD_0"])
                  if "TEXCOORD_0" in attrs else None)
            idx = (vrmio.read_accessor(gltf, bin_data, prim["indices"])
                   if "indices" in prim else None)
            n = len(pos) // 3
            if n == 0:
                continue
            if idx is None:
                idx = list(range(n))
            mat_i = prim.get("material", len(mat_cache) - 1)
            base = len(mesh.verts)
            for v in range(n):
                p = (pos[v * 3], pos[v * 3 + 1], pos[v * 3 + 2])
                if w is not None:
                    p = (w[0] * p[0] + w[1] * p[1] + w[2] * p[2] + w[12],
                         w[4] * p[0] + w[5] * p[1] + w[6] * p[2] + w[13],
                         w[8] * p[0] + w[9] * p[1] + w[10] * p[2] + w[14])
                mesh.verts.append(xf(p))
                mesh.uv.append((uv[v * 2], uv[v * 2 + 1]) if uv else (0.0, 0.0))
            for t in range(0, len(idx) - 2, 3):
                a, b, c = idx[t], idx[t + 1], idx[t + 2]
                if a >= n or b >= n or c >= n:
                    continue
                mesh.tris.append((base + a, base + b, base + c))
                face_mat.append(mat_i if 0 <= mat_i < len(mat_cache) else 0)

    # 骨骼：优先用 skin 的 joints
    skins = gltf.get("skins") or []
    if skins:
        joints = skins[0].get("joints") or []
        for j in joints:
            if j < len(nodes):
                w = worlds[j] if j < len(worlds) and worlds[j] else None
                if w:
                    mesh.bones.append(xf((w[12], w[13], w[14])))
    if not mesh.bones:
        for node_i, node in enumerate(nodes):
            if node.get("name") and node_i < len(worlds) and worlds[node_i]:
                w = worlds[node_i]
                mesh.bones.append(xf((w[12], w[13], w[14])))

    if with_textures:
        mesh._tex_state = None
    else:
        mesh._tex_state = {"kind": "vrm", "gltf": gltf, "bin": bin_data,
                           "mat_cache": mat_cache, "face_mat": list(face_mat),
                           "img_cache": {}}

    _resolve_face_colors(mesh, face_mat, mat_cache)
    return mesh


# ------------------------------------------------------------------- FBX -----
def mesh_from_fbx(path, flip_z=True, log=None, with_textures=True):
    import fbx2pmx
    scene = fbx2pmx.Scene(path)
    mesh = Mesh(os.path.splitext(os.path.basename(path))[0])
    fbx_dir = os.path.dirname(os.path.abspath(path))

    def xf(p):
        x, y, z = p
        return (x, y, -z if flip_z else z)

    # 材质 → 颜色 / 贴图
    def mat_info(mat_id):
        if mat_id is None:
            return {"color": (200, 195, 190), "tex": None, "name": ""}
        node = scene.byid.get(mat_id)
        col = (204, 204, 204)
        if node is not None:
            d = scene._props70(node)
            for key in ("DiffuseColor", "Diffuse"):
                v = d.get(key)
                if v and len(v) >= 3:
                    col = (max(0, min(255, int(float(v[0]) * 255))),
                           max(0, min(255, int(float(v[1]) * 255))),
                           max(0, min(255, int(float(v[2]) * 255))))
                    break
        img = None
        if with_textures:
            for tid in scene.texture_materials.get(mat_id, []):
                t = scene.textures.get(tid)
                if not t:
                    continue
                name = t.get("name") or ""
                if "diffuse" not in name.lower() and img is not None:
                    continue
                img = _find_fbx_texture(fbx_dir, t.get("rel"), t.get("name"))
                if img is not None and "diffuse" in name.lower():
                    break
        return {"color": col, "tex": img, "name": node.props[1] if node is not None else ""}

    geo_ids = [nid for nid, n in scene.byid.items()
               if n.name == "Geometry" and nid in scene.geometry_model]

    def _sort_key(nid):
        try:
            return scene.byid[nid].props[1]
        except Exception:
            return 0

    geo_ids.sort(key=_sort_key)

    mat_cache = {}
    mats = []          # 全局材质表
    face_mat = []
    for gid in geo_ids:
        g = scene.byid[gid]
        mid = scene.geometry_model[gid]
        model = scene.models[mid]
        vn = g.get("Vertices")
        pvn = g.get("PolygonVertexIndex")
        if vn is None or pvn is None:
            continue
        verts = vn.props[0]
        pvi = pvn.props[0]
        tris = fbx2pmx.triangulate(pvi)
        uv = fbx2pmx.sample_uv(g.get("LayerElementUV"), pvi)
        m_mats = fbx2pmx._mesh_materials(g, model, scene)
        poly_mat = fbx2pmx._poly_material_index(g, pvi, len(m_mats))
        w = fbx2pmx.m_mul(model.world, scene._geom_matrix(scene.byid.get(mid)))

        local_mat = {}
        for mi, mat_id in enumerate(m_mats):
            info = mat_cache.get(mat_id)
            if info is None:
                info = mat_info(mat_id)
                mat_cache[mat_id] = info
            local_mat[mi] = len(mats)
            mats.append(info)
        base = len(mesh.verts)
        corner_pos = []
        for tri in tris:
            out = []
            for cp, corner in tri:
                p = verts[cp * 3:cp * 3 + 3]
                wp = xf(fbx2pmx.m_point(w, p))
                mesh.verts.append(wp)
                if uv is not None and corner < len(uv):
                    mesh.uv.append(uv[corner])
                else:
                    mesh.uv.append((0.0, 0.0))
                out.append(len(mesh.verts) - 1)
            mesh.tris.append((out[0], out[1], out[2]))
            mm = poly_mat[len(face_mat)] if (poly_mat is not None
                                             and len(face_mat) < len(poly_mat)) else 0
            face_mat.append(local_mat.get(mm, 0))

    for m in scene.models.values():
        if m.cls == "LimbNode":
            mesh.bones.append(xf(fbx2pmx.m_translation(m.world)))

    mesh.materials = mats
    _resolve_face_colors(mesh, face_mat, mats)
    return mesh


def _find_fbx_texture(fbx_dir, rel, name):
    if not name:
        return None
    cands = []
    if rel:
        cands.append(os.path.join(fbx_dir, rel.replace("\\", "/")))
        cands.append(os.path.join(fbx_dir, "textures", rel.replace("\\", "/")))
    cands.append(os.path.join(fbx_dir, name))
    cands.append(os.path.join(fbx_dir, "textures", name))
    cands.append(os.path.join(os.path.dirname(fbx_dir), name))
    cands.append(os.path.join(os.path.dirname(fbx_dir), "textures", name))
    for p in cands:
        if os.path.isfile(p):
            img = load_image(p)
            if img is not None:
                return img
    # 兜底：在 FBX 目录附近递归找同名文件
    try:
        import fbx2pmx
        p = fbx2pmx._find_file(fbx_dir, name)
        if p:
            return load_image(p)
    except Exception:
        pass
    return None


# -------------------------------------------------------------- unitypackage --
def _find_files(root, exts):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for f in filenames:
            if f.lower().endswith(exts):
                p = os.path.join(dirpath, f)
                try:
                    if os.path.getsize(p) > 1024:
                        out.append(p)
                except OSError:
                    pass
    return out


def mesh_from_unitypackage(path, log=None, with_textures=True):
    import tempfile
    from unitypackage_unpack import unpack as unpack_unitypackage
    dest = tempfile.mkdtemp(prefix="pmx_preview_")
    unpack_unitypackage(path, dest)
    fbxs = _find_files(dest, ".fbx")
    if not fbxs:
        raise ValueError("unitypackage 里没有 FBX")
    fbxs.sort(key=lambda p: -os.path.getsize(p))
    if log:
        log("包内 FBX：%s" % ", ".join(os.path.basename(x) for x in fbxs[:3]))
    return mesh_from_fbx(fbxs[0], log=log, with_textures=with_textures)


def classify(path):
    ext = os.path.splitext(path)[1].lower()
    return {".fbx": "fbx", ".unitypackage": "unitypackage",
            ".vrm": "vrm", ".glb": "vrm", ".pmx": "pmx"}.get(ext, "unknown")


def load_preview(path, kind=None, log=None, flip_z=True, with_textures=True):
    """把任意支持的模型文件读成预览网格。

    with_textures=False 时只解析几何与材质基色（极快），贴图留待
    Mesh.load_textures() 在后台延迟解码——用于拖入大模型时立刻出背视图。
    """
    if kind is None:
        kind = classify(path)
    if kind == "pmx":
        return mesh_from_pmx(path, log=log, with_textures=with_textures)
    if kind == "vrm":
        return mesh_from_vrm(path, log=log, with_textures=with_textures)
    if kind == "fbx":
        return mesh_from_fbx(path, flip_z=flip_z, log=log, with_textures=with_textures)
    if kind == "unitypackage":
        return mesh_from_unitypackage(path, log=log, with_textures=with_textures)
    raise ValueError("不支持的格式：%s" % path)


# ------------------------------------------------------------- 视图核心 ------
def _rot_matrix(yaw, pitch):
    cy, sy = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)
    # R = Rx(pitch) * Ry(yaw)
    return (cy, sy, cp, sp)


def _view_point(p, c, r):
    cy, sy, cp, sp = r
    x = p[0] - c[0]
    y = p[1] - c[1]
    z = p[2] - c[2]
    x1 = x * cy + z * sy
    z1 = -x * sy + z * cy
    y2 = y * cp - z1 * sp
    z2 = y * sp + z1 * cp
    return (x1, y2, z2)


LIGHT = (0.42, 0.55, 0.72)


def _shade(nx, ny, nz):
    ln = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
    nx, ny, nz = nx / ln, ny / ln, nz / ln
    if nz < 0:
        nx, ny, nz = -nx, -ny, -nz
    d = nx * LIGHT[0] + ny * LIGHT[1] + nz * LIGHT[2]
    return 0.42 + 0.58 * max(0.0, d)


# ------------------------------------------------------------------- 控件 ----
if tk is not None:
    class Preview3D(tk.Frame):
        """背视图实时预览控件：默认显示背视图，可拖动旋转、滚轮缩放、右键平移、双击复位。"""

        BG = "#fbfcfe"
        CARD = "#ffffff"
        BORDER = "#dee3ea"
        TXT = "#1f2430"
        MUTED = "#6b7280"
        ACCENT = "#2f6fed"
        ACCENT_SOFT = "#e8f0ff"

        FAST_MAX = 15000        # 拖动旋转时最多绘制的三角面数

        def __init__(self, master, size=360, uiscale=1.0, **kw):
            super().__init__(master, bg=self.CARD, **kw)
            self._px = lambda v: int(round(v * uiscale))
            self.size = size
            self.mesh = None
            self.yaw = 0.0
            self.pitch = 0.0
            self.zoom = 1.0
            self.panx = 0.0
            self.pany = 0.0
            self.show_bones = False
            self.wireframe = False
            self.spin = False
            self._dragging = False
            self._fast = None
            self._last = (0, 0)
            self._spin_job = None
            self._full_job = None

            self._toolbar = tk.Frame(self, bg=self.CARD)
            self._toolbar.pack(side="bottom", fill="x")
            self._build_toolbar()

            self.canvas = tk.Canvas(self, width=self._px(size),
                                    height=self._px(size), bg=self.BG,
                                    highlightthickness=0, cursor="fleur")
            self.canvas.pack(side="top", fill="both", expand=True)
            self.canvas.bind("<Configure>", lambda e: self.redraw())
            self.canvas.bind("<Button-1>", self._on_press)
            self.canvas.bind("<B1-Motion>", self._on_drag)
            self.canvas.bind("<ButtonRelease-1>", self._on_release)
            self.canvas.bind("<Button-3>", self._on_press)
            self.canvas.bind("<B3-Motion>", self._on_pan)
            self.canvas.bind("<Button-2>", self._on_press)
            self.canvas.bind("<B2-Motion>", self._on_pan)
            self.canvas.bind("<MouseWheel>", self._on_wheel)
            self.canvas.bind("<Button-4>", lambda e: self._zoom_at(1.1, e.x, e.y))
            self.canvas.bind("<Button-5>", lambda e: self._zoom_at(1 / 1.1, e.x, e.y))
            self.canvas.bind("<Double-Button-1>", lambda e: self.reset_view())
            self._placeholder()

        # -- 工具条
        def _small(self, parent, text, cmd):
            b = tk.Button(parent, text=text, command=cmd, bg=self.CARD,
                          fg=self.TXT, activebackground=self.ACCENT_SOFT,
                          activeforeground=self.TXT, relief="flat", bd=0,
                          highlightthickness=0, font=("Microsoft YaHei UI", 8),
                          padx=self._px(6), pady=self._px(1), cursor="hand2")
            b._toggle_on = False
            return b

        def _build_toolbar(self):
            tb = self._toolbar
            self._small(tb, "正视", lambda: self.set_view(0, 0)).pack(side="left")
            self._small(tb, "左视", lambda: self.set_view(90, 0)).pack(side="left")
            self._small(tb, "背视", lambda: self.set_view(180, 0)).pack(side="left")
            self._small(tb, "俯视", lambda: self.set_view(0, 78)).pack(side="left")
            self._small(tb, "复位", self.reset_view).pack(side="left")
            self.btn_spin = self._small(tb, "自转", self.toggle_spin)
            self.btn_spin.pack(side="left")
            self.btn_bone = self._small(tb, "骨骼", self.toggle_bones)
            self.btn_bone.pack(side="left")
            self.btn_wire = self._small(tb, "线框", self.toggle_wire)
            self.btn_wire.pack(side="left")

        def _paint_btn(self, b, on):
            b.configure(bg=self.ACCENT if on else self.CARD,
                        fg="#ffffff" if on else self.TXT)

        def _placeholder(self):
            c = self.canvas
            c.delete("all")
            w = c.winfo_width()
            h = c.winfo_height()
            if w <= 1:
                w = self._px(self.size)
            if h <= 1:
                h = self._px(self.size)
            c.create_text(w / 2, h / 2, text="拖入模型后这里实时显示背视图预览\n"
                                             "（按住拖动可旋转，滚轮缩放）",
                          fill=self.MUTED, font=("Microsoft YaHei UI", 9),
                          justify="center")

        # -- 数据
        def set_mesh(self, mesh):
            self.mesh = mesh
            self._fast = None
            if mesh is not None and len(mesh.tris) > self.FAST_MAX:
                stride = max(1, len(mesh.tris) // self.FAST_MAX)
                self._fast = list(range(0, len(mesh.tris), stride))
            self.reset_view()

        def set_bones(self, on):
            self.show_bones = bool(on)
            self._paint_btn(self.btn_bone, self.show_bones)
            self.redraw()

        def toggle_bones(self):
            self.set_bones(not self.show_bones)

        def toggle_wire(self):
            self.wireframe = not self.wireframe
            self._paint_btn(self.btn_wire, self.wireframe)
            self.redraw()

        def toggle_spin(self):
            self.spin = not self.spin
            self._paint_btn(self.btn_spin, self.spin)
            if self.spin:
                self._spin_tick()
            elif self._spin_job:
                try:
                    self.after_cancel(self._spin_job)
                except Exception:
                    pass
                self._spin_job = None

        def _spin_tick(self):
            if not self.spin:
                return
            self.yaw += 0.045
            self._draw(fast=True)
            self._spin_job = self.after(45, self._spin_tick)

        # -- 视图
        def set_view(self, yaw_deg, pitch_deg):
            self.yaw = math.radians(yaw_deg)
            self.pitch = math.radians(pitch_deg)
            self.panx = self.pany = 0.0
            self.zoom = 1.0
            self.redraw()

        def reset_view(self):
            # 默认视角统一为背视图（yaw=180°）；实时预览只显示，不保存图片
            self.yaw = DEFAULT_YAW
            self.pitch = DEFAULT_PITCH
            self.zoom = 1.0
            self.panx = self.pany = 0.0
            self.redraw()

        # -- 交互
        def _on_press(self, e):
            self._dragging = True
            self._last = (e.x, e.y)

        def _on_pan(self, e):
            dx, dy = e.x - self._last[0], e.y - self._last[1]
            self._last = (e.x, e.y)
            self.panx += dx
            self.pany += dy
            self.redraw(fast=True)

        def _on_drag(self, e):
            dx, dy = e.x - self._last[0], e.y - self._last[1]
            self._last = (e.x, e.y)
            self.yaw += dx * 0.012
            self.pitch += dy * 0.012
            lim = math.radians(89)
            self.pitch = max(-lim, min(lim, self.pitch))
            self.redraw(fast=True)

        def _on_release(self, e):
            self._dragging = False
            self.redraw()

        def _on_wheel(self, e):
            self._zoom_at(1.1 if e.delta > 0 else 1 / 1.1, e.x, e.y)

        def _zoom_at(self, f, cx, cy):
            self.zoom = max(0.15, min(12.0, self.zoom * f))
            self.redraw(fast=True)

        # -- 绘制
        def redraw(self, fast=False):
            if fast and self._dragging:
                self._draw(fast=True)
                return
            self._schedule_full()

        def _schedule_full(self):
            if self._full_job:
                try:
                    self.after_cancel(self._full_job)
                except Exception:
                    pass
            self._full_job = self.after(40, self._draw)

        def _draw(self, dragging=False, fast=False):
            self._full_job = None
            c = self.canvas
            c.delete("all")
            m = self.mesh
            if m is None or not m.verts or not m.tris:
                self._placeholder()
                return
            W = c.winfo_width()
            H = c.winfo_height()
            if W <= 1:
                W = self._px(self.size)
            if H <= 1:
                H = self._px(self.size)
            # 交互中 / 自转中 / 线框：用画布多边形（够快），停下后再出精细贴图
            if (fast or dragging or self.wireframe) or (W * H > 900000):
                self._draw_poly(W, H)
            else:
                self._draw_image(W, H)

        def _view_common(self, W, H):
            ctr, span = self.mesh.bbox()
            rot = _rot_matrix(self.yaw, self.pitch)
            fit = min(W, H) * 0.46 / span * self.zoom
            return ctr, rot, fit, W / 2 + self.panx, H / 2 + self.pany

        def _draw_image(self, W, H):
            """逐像素采样贴图，最接近原图的当前视角（默认背视图）。"""
            rows = render(self.mesh, (W, H), yaw=self.yaw, pitch=self.pitch,
                          zoom=self.zoom, show_bones=self.show_bones,
                          bg=(251, 252, 254), pan=(self.panx, self.pany),
                          max_tris=120000)
            if rows is None:
                self._placeholder()
                return
            self._photo = tk.PhotoImage(data=rows_to_ppm(rows))
            self.canvas.create_image(0, 0, anchor="nw", image=self._photo)

        def _draw_poly(self, W, H):
            c = self.canvas
            m = self.mesh
            ctr, rot, fit, cx, cy = self._view_common(W, H)
            proj = []
            ap = proj.append
            for p in m.verts:
                v = _view_point(p, ctr, rot)
                ap((cx + v[0] * fit, cy - v[1] * fit, v[0], v[1], v[2]))
            tris = self._fast if (self._fast is not None and self._dragging) else None
            rng = range(len(m.tris)) if tris is None else tris
            items = []
            ap = items.append
            for i in rng:
                a, b, d = m.tris[i]
                pa, pb, pd = proj[a], proj[b], proj[d]
                ap((pa[4] + pb[4] + pd[4], i))
            items.sort(key=lambda t: t[0])

            frgb = m.face_rgb
            if self.wireframe:
                edges = set()
                for _depth, i in items:
                    a, b, d = m.tris[i]
                    for a2, b2 in ((a, b), (b, d), (d, a)):
                        e = (a2, b2) if a2 < b2 else (b2, a2)
                        if e in edges:
                            continue
                        edges.add(e)
                        pa, pb = proj[a2], proj[b2]
                        c.create_line(pa[0], pa[1], pb[0], pb[1],
                                      fill="#8a9099", width=1)
            else:
                for _depth, i in items:
                    a, b, d = m.tris[i]
                    pa, pb, pd = proj[a], proj[b], proj[d]
                    col = frgb[i] if i < len(frgb) else (200, 200, 200)
                    ux, uy, uz = pb[2] - pa[2], pb[3] - pa[3], pb[4] - pa[4]
                    vx, vy, vz = pd[2] - pa[2], pd[3] - pa[3], pd[4] - pa[4]
                    sh = _shade(uy * vz - uz * vy,
                                uz * vx - ux * vz,
                                ux * vy - uy * vx)
                    r = min(255, int(col[0] * sh))
                    g = min(255, int(col[1] * sh))
                    bl = min(255, int(col[2] * sh))
                    c.create_polygon(pa[0], pa[1], pb[0], pb[1], pd[0], pd[1],
                                     fill="#%02x%02x%02x" % (r, g, bl), outline="")

            if self.show_bones and m.bones:
                for bp in m.bones:
                    v = _view_point(bp, ctr, rot)
                    x = cx + v[0] * fit
                    y = cy - v[1] * fit
                    rr = 3
                    c.create_oval(x - rr, y - rr, x + rr, y + rr,
                                  fill="#e04040", outline="#8a1a1a")


# --------------------------------------------------------------- 离屏渲染 ----
def render_to_png(mesh, path, size=560, yaw=0.0, pitch=0.0, zoom=1.0,
                  show_bones=False, bg=(247, 249, 252)):
    img = render(mesh, size=size, yaw=yaw, pitch=pitch, zoom=zoom,
                 show_bones=show_bones, bg=bg)
    if img is None:
        return None
    pmx_check.write_png(path, size, size, img)
    return path


def render(mesh, size=560, yaw=0.0, pitch=0.0, zoom=1.0, show_bones=False,
           bg=(247, 249, 252), pan=(0.0, 0.0), max_tris=None):
    """正交投影 + z-buffer 软件光栅化，逐像素采样贴图。

    size 可以是整数（正方形）或 (W, H)。返回 W×H 的 [[r,g,b]] 行。
    """
    if not mesh.verts or not mesh.tris:
        return None
    if isinstance(size, (tuple, list)):
        W, H = int(size[0]), int(size[1])
    else:
        W = H = int(size)
    if W <= 0 or H <= 0:
        return None

    ctr, span = mesh.bbox()
    rot = _rot_matrix(yaw, pitch)
    fit = min(W, H) * 0.46 / span * zoom
    ox = W / 2.0 + pan[0]
    oy = H / 2.0 + pan[1]

    proj = []
    for p in mesh.verts:
        v = _view_point(p, ctr, rot)
        proj.append((ox + v[0] * fit, oy - v[1] * fit, v[2]))

    img = [[list(bg) for _ in range(W)] for _ in range(H)]
    zbuf = [[-1e30] * W for _ in range(H)]
    frgb = mesh.face_rgb
    ftex = getattr(mesh, "face_tex", None)
    fcol = getattr(mesh, "face_col", None)
    uv = mesh.uv

    ntri = len(mesh.tris)
    tri_range = range(ntri)
    if max_tris and ntri > max_tris:
        stride = (ntri + max_tris - 1) // max_tris
        tri_range = range(0, ntri, stride)

    for i in tri_range:
        a, b, d = mesh.tris[i]
        pa, pb, pd = proj[a], proj[b], proj[d]
        x0 = max(0, int(min(pa[0], pb[0], pd[0])))
        x1 = min(W - 1, int(max(pa[0], pb[0], pd[0])) + 1)
        y0 = max(0, int(min(pa[1], pb[1], pd[1])))
        y1 = min(H - 1, int(max(pa[1], pb[1], pd[1])) + 1)
        if x1 < x0 or y1 < y0:
            continue
        den = (pb[1] - pd[1]) * (pa[0] - pd[0]) + (pd[0] - pb[0]) * (pa[1] - pd[1])
        if abs(den) < 1e-9:
            continue
        inv = 1.0 / den
        col = frgb[i] if i < len(frgb) else (200, 200, 200)
        tex = ftex[i] if (ftex and i < len(ftex)) else None
        base = fcol[i] if (fcol and i < len(fcol)) else col
        if tex is not None and len(uv) > max(a, b, d):
            ua, va = uv[a]
            ub, vb = uv[b]
            ud, vd = uv[d]
        else:
            tex = None
        # 面法线（视图空间）→ 光照
        ux, uy, uz = pb[0] - pa[0], pb[1] - pa[1], pb[2] - pa[2]
        vx, vy, vz = pd[0] - pa[0], pd[1] - pa[1], pd[2] - pa[2]
        sh = _shade(uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx)
        cr0 = min(255, int(base[0] * sh))
        cg0 = min(255, int(base[1] * sh))
        cb0 = min(255, int(base[2] * sh))
        for py in range(y0, y1 + 1):
            cyy = py + 0.5
            row = img[py]
            zrow = zbuf[py]
            for px in range(x0, x1 + 1):
                cxx = px + 0.5
                w0 = ((pb[1] - pd[1]) * (cxx - pd[0]) +
                      (pd[0] - pb[0]) * (cyy - pd[1])) * inv
                if w0 < 0:
                    continue
                w1 = ((pd[1] - pa[1]) * (cxx - pd[0]) +
                      (pa[0] - pd[0]) * (cyy - pd[1])) * inv
                if w1 < 0:
                    continue
                w2 = 1.0 - w0 - w1
                if w2 < 0:
                    continue
                z = w0 * pa[2] + w1 * pb[2] + w2 * pd[2]
                if z <= zrow[px]:
                    continue
                zrow[px] = z
                if tex is not None:
                    uu = w0 * ua + w1 * ub + w2 * ud
                    vv = w0 * va + w1 * vb + w2 * vd
                    tr, tg, tb, _t = tex.sample(uu, vv)
                    row[px] = [cr0 * tr // 255, cg0 * tg // 255, cb0 * tb // 255]
                else:
                    row[px] = [cr0, cg0, cb0]

    if show_bones:
        for bp in mesh.bones:
            v = _view_point(bp, ctr, rot)
            ix, iy = int(ox + v[0] * fit), int(oy - v[1] * fit)
            for dx in range(-2, 3):
                for dy in range(-2, 3):
                    if abs(dx) + abs(dy) > 3:
                        continue
                    px, py = ix + dx, iy + dy
                    if 0 <= px < W and 0 <= py < H:
                        img[py][px] = [220, 40, 40]
    return img


def rows_to_ppm(rows):
    """[[r,g,b]] → PPM(P6) 字节，供 tk.PhotoImage 直接使用。"""
    h = len(rows)
    w = len(rows[0]) if h else 0
    out = bytearray(("P6\n%d %d\n255\n" % (w, h)).encode("ascii"))
    for r in rows:
        out += bytes(b for px in r for b in px)
    return bytes(out)


# --------------------------------------------------------------------- CLI --
def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="全格式模型预览渲染")
    ap.add_argument("model")
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--size", type=int, default=560)
    ap.add_argument("--yaw", type=float, default=0.0)
    ap.add_argument("--pitch", type=float, default=0.0)
    ap.add_argument("--bones", action="store_true")
    a = ap.parse_args(argv)
    mesh = load_preview(a.model, log=lambda s: print(s))
    st = mesh.stats()
    print("网格：顶点 %d · 三角面 %d · 材质 %d · 骨骼 %d"
          % (st["verts"], st["tris"], st["mats"], st["bones"]))
    out = a.out or (os.path.splitext(a.model)[0] + "_preview.png")
    p = render_to_png(mesh, out, size=a.size, yaw=math.radians(a.yaw),
                      pitch=math.radians(a.pitch), show_bones=a.bones)
    print("预览 -> %s" % p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
