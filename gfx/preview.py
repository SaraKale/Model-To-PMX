# -*- coding: utf-8 -*-
"""preview.py - 全格式模型实时预览（纯 Python 标准库）。

把 .pmx / .vrm / .fbx / .unitypackage 读成统一的“预览网格”（顶点 / 三角面 /
逐面颜色 / 骨骼），在 tkinter 画布里做**背视图**实时交互预览（默认即背视图，
也可拖动旋转 / 滚轮缩放 / 右键平移 / 一键复位查看其它角度）。

预览**只实时显示、不落盘保存图片**；如需把模型渲染成 PNG，请用命令行
render_to_png / `python preview.py "model.pmx" -o out.png`（独立于实时预览）。

不依赖 numpy / PIL / OpenGL，只用到 tkinter + zlib + struct。
装了 Pillow 时会更顺：贴图解码走 C 实现，低分辨率渲染结果也交给 Pillow
放大到画布尺寸（Y 方向双线性），渲染后端会显示在预览工具栏右下角。

预览的流畅度来自三层配合（纯 Python 软光栅很难单靠优化循环赢）：
    1) 交互（拖动 / 滚轮 / 自转）时用 build_lod() 抽稀出的轻量网格，
       面数与顶点数都降下来，一帧 ~40ms；
    2) 每个时间片只处理 SLICE 个三角面，界面不会被一帧渲染长时间占住；
    3) 像素预算按实测耗时自适应，机器快就出高清，机器慢就自动降分辨率。

主要接口：
    load_preview(path, kind=None, log=None)  -> Mesh
    Preview3D(master, size=480, ...)         # tkinter 实时预览控件（背视图）
    Rasterizer(mesh, size, ...).step(n)      # 可分片推进的软光栅器
    render(mesh, size, ...)                  # 一次画完，返回 (W, H, buf)
    render_to_png(mesh, path, size=560, ...) # 仅命令行离屏渲染用，GUI 预览不调用

命令行（独立导出，不影响实时预览）：
    python preview.py "model.pmx" -o preview.png --yaw 180 --pitch 0
"""
import io
import math
import os
import struct
import sys
import time
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

def pil_info():
    """返回 (是否可用, 版本串)；供界面显示渲染后端。"""
    if _PIL_Image is None:
        return (False, "")
    try:
        return (True, getattr(_PIL_Image, "__version__", "") or "")
    except Exception:
        return (True, "")


# ImageTk 只在真正画时才 import（tkinter 未就绪时 import 会失败）
_PIL_ImageTk = None
_PIL_ITK_TRIED = False


def get_imagetk():
    global _PIL_ImageTk, _PIL_ITK_TRIED
    if _PIL_ITK_TRIED:
        return _PIL_ImageTk
    _PIL_ITK_TRIED = True
    if _PIL_Image is None or tk is None:
        return None
    try:
        from PIL import ImageTk
        _PIL_ImageTk = ImageTk
    except Exception:
        _PIL_ImageTk = None
    return _PIL_ImageTk


# 预览贴图分辨率上限：改为 2048，在高分屏 / 放大查看时贴图细节更清晰。
# 内存占用增加有限，但能有效减少放大后的贴图模糊。
PREVIEW_TEX_MAX = 2048

# 透明度裁剪阈值（0~255）。低于它的像素整块丢弃（露出后面的面/背景），
# 高于它的按 alpha 混合。蕾丝、发梢、裙纱这些靠贴图 alpha 挖洞的材质，
# 以前会被当成实心画出来（透明区通常是黑的），于是满屏黑边黑点。
ALPHA_CUT = 90

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
        self.face_alpha = []     # 每面材质不透明度 0~1（PMX diffuse.a / VRM baseColorFactor.a）
        self.bones = []          # [(x, y, z)]
        self.materials = []      # 原始材质信息
        self._bbox = None
        self._tex_state = None  # 渐进式贴图：延迟解码用的素材，解码后置 None
        self.rev = 0            # 每次重算逐面颜色就 +1，预览控件据此判断是否需要重绘

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
                mats.append({"color": col, "tex": img, "name": mat.get("name", ""),
                             "alpha": d[3] if len(d) > 3 else 1.0})
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
        self.rev += 1
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
        self.face_alpha = [1.0] * n

    def build_lod(self, target=15000):
        """抽稀出一个只用于“拖动时”的轻量网格；面数本来就不多则返回 None。

        随机抽样（固定种子）而不是按索引等距丢面，这样抽到的面在整个模型上
        分布均匀，不会出现一圈一圈的空洞。同时重排顶点表：只保留被抽到的面
        用到的顶点，于是每帧连投影的顶点数也跟着降下来（投影是大头之一）。
        """
        n = len(self.tris)
        if n <= target or target <= 0:
            return None
        import random
        rng = random.Random(20240913)
        order = sorted(rng.sample(range(n), target))
        vmap = {}
        verts = []
        uv = []
        tris = []
        frgb = []
        ftex = []
        fcol = []
        falpha = []
        for ti in order:
            nt = []
            for vi in self.tris[ti]:
                j = vmap.get(vi)
                if j is None:
                    j = len(verts)
                    vmap[vi] = j
                    verts.append(self.verts[vi])
                    uv.append(self.uv[vi] if vi < len(self.uv) else (0.0, 0.0))
                nt.append(j)
            tris.append((nt[0], nt[1], nt[2]))
            frgb.append(self.face_rgb[ti] if ti < len(self.face_rgb)
                        else (210, 205, 200))
            ftex.append(self.face_tex[ti] if ti < len(self.face_tex) else None)
            fcol.append(self.face_col[ti] if ti < len(self.face_col)
                        else (210, 205, 200))
            falpha.append(self.face_alpha[ti] if ti < len(self.face_alpha)
                          else 1.0)
        out = Mesh(self.name)
        out.verts = verts
        out.uv = uv
        out.tris = tris
        out.face_rgb = frgb
        out.face_tex = ftex
        out.face_col = fcol
        out.face_alpha = falpha
        out.bones = self.bones
        out.materials = self.materials
        out.rev = self.rev
        try:
            out._bbox = self.bbox()      # 视角/缩放不受抽稀影响
        except Exception:
            pass
        return out


def _resolve_face_colors(mesh, face_mat, mats):
    """face_mat: 每个三角面的材质下标；mats: [{color, tex}]。

    同时产出：
        face_rgb   —— 每面的代表色（实时预览用，取贴图重心色）
        face_tex   —— 每面的贴图对象（离屏渲染时逐像素采样，最接近原图）
        face_col   —— 每面的基色
        face_alpha —— 每面的材质不透明度（贴图自身的 alpha 在光栅化时再判）
    """
    colors = []
    texes = []
    cols = []
    alphas = []
    for ti, tri in enumerate(mesh.tris):
        mi = face_mat[ti] if ti < len(face_mat) else 0
        mat = mats[mi] if 0 <= mi < len(mats) else None
        if mat is None:
            colors.append((210, 205, 200))
            texes.append(None)
            cols.append((210, 205, 200))
            alphas.append(1.0)
            continue
        r, g, b = mat.get("color", (210, 205, 200))
        tex = mat.get("tex")
        al = mat.get("alpha", 1.0)
        try:
            al = float(al)
        except (TypeError, ValueError):
            al = 1.0
        alphas.append(0.0 if al < 0.0 else (1.0 if al > 1.0 else al))
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
    mesh.face_alpha = alphas


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
        mats.append({"color": col, "tex": img, "name": mat.get("name", ""),
                     "alpha": d[3] if len(d) > 3 else 1.0})
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
        mat_cache.append({"color": col, "tex": img, "name": gm.get("name", ""),
                          "alpha": f[3] if len(f) > 3 else 1.0})
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
            return {"color": (200, 195, 190), "tex": None, "alpha": 1.0,
                    "name": ""}
        node = scene.byid.get(mat_id)
        col = (204, 204, 204)
        alpha = 1.0
        if node is not None:
            d = scene._props70(node)
            for key in ("DiffuseColor", "Diffuse"):
                v = d.get(key)
                if v and len(v) >= 3:
                    col = (max(0, min(255, int(float(v[0]) * 255))),
                           max(0, min(255, int(float(v[1]) * 255))),
                           max(0, min(255, int(float(v[2]) * 255))))
                    if len(v) > 3:
                        try:
                            alpha = float(v[3])
                        except (TypeError, ValueError):
                            alpha = 1.0
                    break
            # FBX 也常用 TransparencyFactor 表达不透明度（1 = 全透明）
            if alpha >= 1.0:
                tf = d.get("TransparencyFactor")
                if tf:
                    try:
                        alpha = 1.0 - float(tf[0])
                    except (TypeError, ValueError, IndexError):
                        pass
        if not (alpha == alpha):          # NaN
            alpha = 1.0
        alpha = 0.0 if alpha < 0.0 else (1.0 if alpha > 1.0 else alpha)
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
        return {"color": col, "tex": img, "alpha": alpha,
                "name": node.props[1] if node is not None else ""}

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
# 预览控件自带的文案；界面其它部分有自己的 i18n，这里由调用方通过 labels= 覆盖。
PV_LABELS = {
    "front": "正视", "left": "左视", "back": "背视", "top": "俯视",
    "reset": "复位", "spin": "自转", "bone": "骨骼", "wire": "线框",
    "placeholder": "拖入模型后这里实时显示背视图预览\n（按住拖动可旋转，滚轮缩放）",
}


def pv_labels(**kw):
    """拿一份预览文案（已按 kw 覆盖）交给 Preview3D。"""
    out = dict(PV_LABELS)
    out.update({k: v for k, v in kw.items() if v})
    return out


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

        FAST_MAX = 25000        # 线框模式下最多画出的三角面数

        # 交互与精修的节奏：拖动时按 ~12ms 一帧出图，停手 80ms 后出精修图
        FAST_DELAY = 12
        FULL_DELAY = 80
        # 高清参数：提高像素预算，把最大降采样限制在 2×2 以内，
        # 静止时尽量 1:1 渲染，拖动时最多 2×2 降采样，显著减少马赛克。
        FAST_PX = 240000         # 交互时的像素预算（480×480 以下可 1:1 渲染）
        MIN_PX = 150000          # 精修预算下限，避免被自适应压得太糊
        START_PX = 1600000       # 精修起始像素预算，静止时优先全分辨率
        MAX_PX = 1600000         # 精修像素上限，支持 960×960 以内 1:1 渲染
        # 注：以上数值可按机器性能继续上调；MAX_PX 大于等于画布面积时即 1:1 渲染。
        LOD_TRIS = 30000         # 交互用的轻量网格目标面数（更精细）
        SLICE = 3000             # 精修渲染每片推进的三角面数
        SLICE_MS = 0.05          # 精修渲染每个时间片最长占用（秒）

        def __init__(self, master, size=480, uiscale=1.0, labels=None, **kw):
            super().__init__(master, bg=self.CARD, **kw)
            self._px = lambda v: int(round(v * uiscale))
            self.labels = dict(PV_LABELS)
            if labels:
                self.labels.update(labels)
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
            self._fast_job = None
            self._shown = None          # 上次绘制的画面参数指纹，用于跳过重复绘制
            self._img_id = None
            self._budget = self.START_PX
            self._ms = 0.0
            self._lod = None            # 交互用的轻量网格（第一次拖动时才生成）
            self._lod_built = False
            self._r = None              # 正在进行的精修渲染
            self._rjob = None
            self._r_t0 = 0.0
            self._r_last = 0.0
            self._cur = (0, 0, 1)

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
            L = self.labels
            self._small(tb, L["front"], lambda: self.set_view(0, 0)).pack(side="left")
            self._small(tb, L["left"], lambda: self.set_view(90, 0)).pack(side="left")
            self._small(tb, L["back"], lambda: self.set_view(180, 0)).pack(side="left")
            self._small(tb, L["top"], lambda: self.set_view(0, 78)).pack(side="left")
            self._small(tb, L["reset"], self.reset_view).pack(side="left")
            self.btn_spin = self._small(tb, L["spin"], self.toggle_spin)
            self.btn_spin.pack(side="left")
            self.btn_bone = self._small(tb, L["bone"], self.toggle_bones)
            self.btn_bone.pack(side="left")
            self.btn_wire = self._small(tb, L["wire"], self.toggle_wire)
            self.btn_wire.pack(side="left")
            # 右下角：渲染后端 + 上一帧耗时，便于判断“卡”在哪个环节
            ok, _ver = pil_info()
            self._backend = "Pillow ✓" if ok else "纯 Python"
            self.lbl_perf = tk.Label(tb, text=self._backend, bg=self.CARD,
                                     fg=self.MUTED, anchor="e",
                                     font=("Consolas", 8))
            self.lbl_perf.pack(side="right", padx=(0, self._px(6)))

        def _perf_text(self):
            return "%s · %d ms" % (self._backend, int(self._ms + 0.5))

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
            c.create_text(w / 2, h / 2, text=self.labels["placeholder"],
                          fill=self.MUTED, font=("Microsoft YaHei UI", 9),
                          justify="center")

        # -- 数据
        def set_mesh(self, mesh):
            self.mesh = mesh
            self._fast = None
            self._shown = None
            self._lod = None
            self._lod_built = False
            self._cancel_render()
            if mesh is not None and len(mesh.tris) > self.FAST_MAX:
                stride = max(1, len(mesh.tris) // self.FAST_MAX)
                self._fast = list(range(0, len(mesh.tris), stride))
            self.reset_view()
            # 立刻出一帧低清图（拖入 / 换向的手感就在这里），精修图随后跟上
            try:
                self._draw(fast=True)
            except Exception:
                pass

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

        # -- 绘制：交互时低分辨率快速出图，停手后再出精修图
        def redraw(self, fast=False):
            """合并短时间内的多次请求。

            每一帧的 Python 软光栅是有成本的：拖动时事件可能每 5ms 来一次，
            如果每次都重新定时，定时器会被反复取消、一帧都画不出来。所以这里用
            “同一时刻最多一个待执行帧”的办法，稳定按 FAST_DELAY 出低清图。
            """
            if fast:
                if self._fast_job is None:
                    self._fast_job = self.after(self.FAST_DELAY, self._frame_fast)
                return
            self._schedule_full()

        def _frame_fast(self):
            self._fast_job = None
            self._draw(fast=True)
            self._schedule_full()

        def _schedule_full(self, delay=None):
            if self._full_job:
                try:
                    self.after_cancel(self._full_job)
                except Exception:
                    pass
            self._full_job = self.after(self.FULL_DELAY if delay is None else delay,
                                        self._frame_full)

        def _frame_full(self):
            self._full_job = None
            self._draw(fast=False)

        def _canvas_size(self):
            W = self.canvas.winfo_width()
            H = self.canvas.winfo_height()
            if W <= 1:
                W = self._px(self.size)
            if H <= 1:
                H = self._px(self.size)
            return W, H

        def _plan(self, W, H, fast):
            """按像素预算决定渲染分辨率；返回 (rw, rh, 放大倍数)。"""
            budget = self.FAST_PX if fast else min(self._budget, self.MAX_PX, W * H)
            pix = W * H
            step = 1
            if pix > budget and budget > 0:
                step = int(math.ceil(math.sqrt(pix / float(budget))))
                # 有 Pillow 时用 LANCZOS 放大，限制最多 2×2 降采样以保持清晰；
                # 无 Pillow 时 tk.PhotoImage.zoom() 是最近邻，静态渲染直接全分辨率。
                max_step = 2 if get_imagetk() is not None else 1
                step = max(1, min(max_step, step))
            rw = max(8, int(math.ceil(W / float(step))))
            rh = max(8, int(math.ceil(H / float(step))))
            return rw, rh, step

        def _cancel_render(self):
            """丢掉正在进行的精修渲染（用户又开始操作了）。"""
            self._r = None
            if self._rjob:
                try:
                    self.after_cancel(self._rjob)
                except Exception:
                    pass
                self._rjob = None

        def _lod_mesh(self):
            """交互用的轻量网格；第一次需要时才抽稀（一次性成本，几十毫秒）。"""
            if self._lod_built:
                return self._lod
            self._lod_built = True
            try:
                self._lod = (self.mesh.build_lod(self.LOD_TRIS)
                             if self.mesh is not None else None)
            except Exception:
                self._lod = None
            return self._lod

        def _adapt(self, dt, W, H):
            """实测每帧耗时，自动在“分辨率”与“帧率”之间找平衡。

            为了保证高清，降低时更保守（避免一步砍到太糊），
            帧率富余时稳步提升分辨率。
            """
            if dt > 0.25:
                self._budget = max(self.MIN_PX, int(self._budget * 0.5))
            elif dt > 0.12:
                self._budget = max(self.MIN_PX, int(self._budget * 0.75))
            elif dt < 0.03:
                self._budget = min(self.MAX_PX, W * H,
                                   int(self._budget * 1.5) + 5000)

        def _draw(self, dragging=False, fast=False):
            m = self.mesh
            if m is None or not m.verts or not m.tris:
                self._cancel_render()
                self._placeholder()
                return
            W, H = self._canvas_size()
            mesh = (self._lod_mesh() or m) if fast else m
            rw, rh, step = self._plan(W, H, fast)
            # 画面指纹：视角 / 画布 / 渲染参数全一样时直接跳过，避免 Configure
            # 事件、重复排程把同一帧刷了一遍又一遍。
            key = (id(mesh), getattr(mesh, "rev", 0), id(m),
                   round(self.yaw, 5), round(self.pitch, 5), round(self.zoom, 5),
                   round(self.panx, 2), round(self.pany, 2),
                   rw, rh, self.show_bones, self.wireframe)
            if self.wireframe:
                if key == self._shown:
                    return
                self._shown = key
                self._cancel_render()
                self._draw_poly(W, H)
                return
            if self._r is not None and key == self._shown:
                return                      # 精修渲染还在跑，别打断
            self._shown = key
            self._cancel_render()
            self._cur = (W, H, step)
            if fast:
                out = render(mesh, (rw, rh), yaw=self.yaw, pitch=self.pitch,
                             zoom=self.zoom, show_bones=self.show_bones,
                             bg=(251, 252, 254), pan=(self.panx, self.pany))
                if out is None:
                    self._placeholder()
                    return
                self._blit(out[0], out[1], out[2], step, W, H)
                self._schedule_full()
                return
            # 精修：分片推进，每帧只占 SLICE_MS，界面不会跟着卡
            try:
                self._r = Rasterizer(mesh, (rw, rh), yaw=self.yaw,
                                     pitch=self.pitch, zoom=self.zoom,
                                     show_bones=self.show_bones,
                                     bg=(251, 252, 254),
                                     pan=(self.panx, self.pany))
            except Exception:
                self._placeholder()
                return
            self._r_t0 = time.perf_counter()
            self._r_last = 0.0
            self._tick_full()

        def _tick_full(self):
            """一次 after 回调里推进几片；画完或超时就把中间结果贴上去。"""
            self._rjob = None
            r = self._r
            if r is None:
                return
            W, H, step = self._cur
            t0 = time.perf_counter()
            while True:
                done = r.step(self.SLICE)
                if done:
                    self._r = None
                    total = time.perf_counter() - self._r_t0
                    self._blit(r.W, r.H, r.buf, step, W, H)
                    self._adapt(total, W, H)
                    self._ms = total * 1000.0
                    try:
                        self.lbl_perf.configure(text=self._perf_text())
                    except Exception:
                        pass
                    return
                if time.perf_counter() - t0 > self.SLICE_MS:
                    break
            now = time.perf_counter()
            if now - self._r_last > 0.09:      # 别每片都刷，建图也是有成本的
                self._r_last = now
                self._blit(r.W, r.H, r.buf, step, W, H)
            self._rjob = self.after(1, self._tick_full)

        def _blit(self, rw, rh, buf, step, W, H):
            """把渲染结果（可能是低分辨率的）放大到画布尺寸后贴上去。"""
            itk = get_imagetk()
            if itk is not None:
                # Pillow 负责把低清图放大到画布尺寸（C 实现，几乎不耗时）
                im = _PIL_Image.frombytes("RGB", (rw, rh), bytes(buf))
                if (rw, rh) != (W, H):
                  # LANCZOS 放大低清渲染结果，比 BILINEAR 更清晰、锯齿更少
                    im = im.resize((W, H), _PIL_Image.LANCZOS)
                photo = itk.PhotoImage(im)
            else:
                photo = tk.PhotoImage(data=buf_to_ppm(buf, rw, rh))
                if step > 1:
                    photo = photo.zoom(step)
            self._photo = photo                  # 必须留引用，否则图片会被回收
            c = self.canvas
            c.delete("all")
            self._img_id = c.create_image(0, 0, anchor="nw", image=photo)

        def _view_common(self, W, H):
            ctr, span = self.mesh.bbox()
            rot = _rot_matrix(self.yaw, self.pitch)
            fit = min(W, H) * 0.46 / span * self.zoom
            return ctr, rot, fit, W / 2 + self.panx, H / 2 + self.pany

        def _draw_poly(self, W, H):
            """线框模式：直接画画布图元（线比逐像素取样清楚得多）。"""
            c = self.canvas
            m = self.mesh
            c.delete("all")
            ctr, rot, fit, cx, cy = self._view_common(W, H)
            proj = []
            ap = proj.append
            for p in m.verts:
                v = _view_point(p, ctr, rot)
                ap((cx + v[0] * fit, cy - v[1] * fit, v[0], v[1], v[2]))
            tris = self._fast if self._fast is not None else None
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
    w, h, buf = img
    _write_png_flat(path, w, h, buf)
    return path


def _write_png_flat(path, w, h, buf):
    """把扁平 RGB 缓冲写成 PNG（不依赖 Pillow）。"""
    stride = w * 3
    raw = bytearray()
    for y in range(h):
        o = y * stride
        raw.append(0)
        raw += buf[o:o + stride]

    def chunk(typ, data):
        return (struct.pack(">I", len(data)) + typ + data +
                struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)))
        f.write(chunk(b"IDAT", zlib.compress(bytes(raw), 6)))
        f.write(chunk(b"IEND", b""))


def buf_to_ppm(buf, w, h):
    """扁平 RGB 缓冲 → PPM(P6) 字节，供 tk.PhotoImage 直接使用。"""
    return ("P6\n%d %d\n255\n" % (w, h)).encode("ascii") + bytes(buf)


class Rasterizer(object):
    """可分片推进的软件光栅器。

    主线程每次只给它几十毫秒（step(n) 处理 n 个三角面），大模型的精修图会
    “逐块变清晰”，而不是一帧卡住几百毫秒让整个界面失去响应。
    返回的 buf 是长度 W*H*3 的扁平 RGB bytearray。
    """

    def __init__(self, mesh, size, yaw=0.0, pitch=0.0, zoom=1.0,
                 show_bones=False, bg=(247, 249, 252), pan=(0.0, 0.0),
                 max_tris=None):
        if isinstance(size, (tuple, list)):
            W, H = int(size[0]), int(size[1])
        else:
            W = H = int(size)
        if W <= 0 or H <= 0:
            raise ValueError("bad size")
        self.mesh = mesh
        self.W = W
        self.H = H
        self.show_bones = show_bones

        ctr, span = mesh.bbox()
        rot = _rot_matrix(yaw, pitch)
        fit = min(W, H) * 0.46 / span * zoom
        self.ctr = ctr
        self.rot = rot
        self.fit = fit
        ox = W / 2.0 + pan[0]
        oy = H / 2.0 + pan[1]
        self.ox = ox
        self.oy = oy

        proj = []
        ap = proj.append
        # 这里刻意把 _view_point 的算式内联展开：模型动辄几万个顶点，
        # 省掉一次函数调用 / 元组拆包，投影环节就能快上近一倍。
        cy_, sy_, cp_, sp_ = rot
        ccx, ccy, ccz = ctr
        for p in mesh.verts:
            x = p[0] - ccx
            y = p[1] - ccy
            z = p[2] - ccz
            x1 = x * cy_ + z * sy_
            z1 = -x * sy_ + z * cy_
            ap((ox + x1 * fit, oy - (y * cp_ - z1 * sp_) * fit,
                 y * sp_ + z1 * cp_))
        self.proj = proj

        self.buf = bytearray(bytes((int(bg[0]) & 255, int(bg[1]) & 255,
                                    int(bg[2]) & 255)) * (W * H))
        self.zbuf = [-1e30] * (W * H)
        self.frgb = mesh.face_rgb
        self.ftex = getattr(mesh, "face_tex", None)
        self.fcol = getattr(mesh, "face_col", None)
        self.falpha = getattr(mesh, "face_alpha", None)
        self.uv = mesh.uv

        ntri = len(mesh.tris)
        if max_tris and ntri > max_tris:
            stride = (ntri + max_tris - 1) // max_tris
            self.order = list(range(0, ntri, stride))
        else:
            self.order = None
        self.total = len(self.order) if self.order is not None else ntri
        self.pos = 0
        self.done = False

    def step(self, n=None):
        """处理最多 n 个三角面（None = 一次做完）；返回 True 表示整幅已完成。"""
        if self.done:
            return True
        W, H = self.W, self.H
        proj = self.proj
        buf = self.buf
        zbuf = self.zbuf
        frgb = self.frgb
        ftex = self.ftex
        fcol = self.fcol
        falpha = self.falpha
        uv = self.uv
        nuv = len(uv)
        tris = self.mesh.tris
        order = self.order
        pos = self.pos
        end = self.total if n is None else min(self.total, pos + n)

        for k in range(pos, end):
            i = k if order is None else order[k]
            a, b, d = tris[i]
            pa, pb, pd = proj[a], proj[b], proj[d]
            ax, ay = pa[0], pa[1]
            bx, by = pb[0], pb[1]
            cx, cy = pd[0], pd[1]
            # 屏幕包围盒（顺便裁掉退化面 / 亚像素面）
            minx = ax if ax < bx else bx
            if cx < minx:
                minx = cx
            maxx = ax if ax > bx else bx
            if cx > maxx:
                maxx = cx
            miny = ay if ay < by else by
            if cy < miny:
                miny = cy
            maxy = ay if ay > by else by
            if cy > maxy:
                maxy = cy
            x0 = int(minx)
            if x0 < 0:
                x0 = 0
            y0 = int(miny)
            if y0 < 0:
                y0 = 0
            x1 = int(maxx)
            if x1 >= W:
                x1 = W - 1
            y1 = int(maxy)
            if y1 >= H:
                y1 = H - 1
            if x1 < x0 or y1 < y0:
                continue

            # 三条边的边函数及其像素步进量（比逐像素重算重心坐标快得多）
            eab_dx = -(by - ay)
            eab_dy = (bx - ax)
            ebc_dx = -(cy - by)
            ebc_dy = (cx - bx)
            eca_dx = -(ay - cy)
            eca_dy = (ax - cx)
            sx0 = x0 + 0.5
            sy0 = y0 + 0.5
            eab0 = (bx - ax) * (sy0 - ay) - (by - ay) * (sx0 - ax)
            ebc0 = (cx - bx) * (sy0 - by) - (cy - by) * (sx0 - bx)
            eca0 = (ax - cx) * (sy0 - cy) - (ay - cy) * (sx0 - cx)
            area2 = eab0 + ebc0 + eca0
            if area2 == 0.0:
                continue
            if area2 < 0.0:                 # 统一绕序，内部判定恒为 >= 0
                area2 = -area2
                eab0, eab_dx, eab_dy = -eab0, -eab_dx, -eab_dy
                ebc0, ebc_dx, ebc_dy = -ebc0, -ebc_dx, -ebc_dy
                eca0, eca_dx, eca_dy = -eca0, -eca_dx, -eca_dy
            inv = 1.0 / area2
            za, zb, zc = pa[2], pb[2], pd[2]

            col = frgb[i] if i < len(frgb) else (200, 200, 200)
            tex = ftex[i] if (ftex and i < len(ftex)) else None
            base = fcol[i] if (fcol and i < len(fcol)) else col
            # 材质自身的不透明度（PMX diffuse.a / VRM baseColorFactor.a）
            fa = 255
            if falpha is not None and i < len(falpha):
                _fa = falpha[i]
                if _fa < 1.0:
                    fa = int(_fa * 255.0 + 0.5)
                    if fa < ALPHA_CUT:
                        continue            # 整面几乎全透明，直接跳过
            # 面法线（视图空间）→ 光照，整个面一个常量
            ux, uy, uz = bx - ax, by - ay, zb - za
            vx, vy, vz = cx - ax, cy - ay, zc - za
            sh = _shade(uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx)
            cr0 = min(255, int(base[0] * sh))
            cg0 = min(255, int(base[1] * sh))
            cb0 = min(255, int(base[2] * sh))

            if tex is not None and nuv > max(a, b, d):
                ua, va = uv[a]
                ub, vb = uv[b]
                uc, vc = uv[d]
                tp = tex.px
                tw = tex.w
                th = tex.h
                twf = float(tw)
                thf = float(th)
            else:
                tex = None

            eab_r, ebc_r, eca_r = eab0, ebc0, eca0
            if tex is None:
                if fa >= 250:
                    for py in range(y0, y1 + 1):
                        eab = eab_r
                        ebc = ebc_r
                        eca = eca_r
                        row0 = py * W
                        for px in range(x0, x1 + 1):
                            if eab >= 0.0 and ebc >= 0.0 and eca >= 0.0:
                                idx = row0 + px
                                z = (ebc * za + eca * zb + eab * zc) * inv
                                if z > zbuf[idx]:
                                    zbuf[idx] = z
                                    o = idx * 3
                                    buf[o] = cr0
                                    buf[o + 1] = cg0
                                    buf[o + 2] = cb0
                            eab += eab_dx
                            ebc += ebc_dx
                            eca += eca_dx
                        eab_r += eab_dy
                        ebc_r += ebc_dy
                        eca_r += eca_dy
                else:
                    # 材质半透明（无贴图）：与已经画上去的底层混合
                    ia = 255 - fa
                    for py in range(y0, y1 + 1):
                        eab = eab_r
                        ebc = ebc_r
                        eca = eca_r
                        row0 = py * W
                        for px in range(x0, x1 + 1):
                            if eab >= 0.0 and ebc >= 0.0 and eca >= 0.0:
                                idx = row0 + px
                                z = (ebc * za + eca * zb + eab * zc) * inv
                                if z > zbuf[idx]:
                                    zbuf[idx] = z
                                    o = idx * 3
                                    buf[o] = (cr0 * fa + buf[o] * ia) // 255
                                    buf[o + 1] = (cg0 * fa + buf[o + 1] * ia) // 255
                                    buf[o + 2] = (cb0 * fa + buf[o + 2] * ia) // 255
                            eab += eab_dx
                            ebc += ebc_dx
                            eca += eca_dx
                        eab_r += eab_dy
                        ebc_r += ebc_dy
                        eca_r += eca_dy
            else:
                # UV 也用增量步进，避免每个像素再做一遍重心插值
                u_r = (ebc0 * ua + eca0 * ub + eab0 * uc) * inv
                v_r = (ebc0 * va + eca0 * vb + eab0 * vc) * inv
                du_dx = (ebc_dx * ua + eca_dx * ub + eab_dx * uc) * inv
                dv_dx = (ebc_dx * va + eca_dx * vb + eab_dx * vc) * inv
                du_dy = (ebc_dy * ua + eca_dy * ub + eab_dy * uc) * inv
                dv_dy = (ebc_dy * va + eca_dy * vb + eab_dy * vc) * inv
                for py in range(y0, y1 + 1):
                    eab = eab_r
                    ebc = ebc_r
                    eca = eca_r
                    u = u_r
                    v = v_r
                    row0 = py * W
                    for px in range(x0, x1 + 1):
                        if eab >= 0.0 and ebc >= 0.0 and eca >= 0.0:
                            idx = row0 + px
                            z = (ebc * za + eca * zb + eab * zc) * inv
                            if z > zbuf[idx]:
                                uu = u
                                if uu >= 1.0:
                                    uu -= int(uu)
                                elif uu < 0.0:
                                    uu -= int(uu) - 1
                                vv = v
                                if vv >= 1.0:
                                    vv -= int(vv)
                                elif vv < 0.0:
                                    vv -= int(vv) - 1
                                xi = int(uu * twf)
                                if xi >= tw:
                                    xi = tw - 1
                                yi = int(vv * thf)
                                if yi >= th:
                                    yi = th - 1
                                o2 = (yi * tw + xi) << 2
                                # 贴图 alpha：低于阈值整块丢弃（露出后面的面），
                                # 中间值按 alpha 与底层混合。丢弃时不写 z，
                                # 于是后面的层仍然能画出来 —— 蕾丝/发梢的镂空才对。
                                ta = tp[o2 + 3]
                                if ta:
                                    if fa != 255:
                                        ta = (ta * fa) // 255
                                    if ta >= ALPHA_CUT:
                                        o = idx * 3
                                        if ta >= 250:
                                            buf[o] = (cr0 * tp[o2]) // 255
                                            buf[o + 1] = (cg0 * tp[o2 + 1]) // 255
                                            buf[o + 2] = (cb0 * tp[o2 + 2]) // 255
                                        else:
                                            ia = 255 - ta
                                            buf[o] = ((cr0 * tp[o2] // 255) * ta
                                                      + buf[o] * ia) // 255
                                            buf[o + 1] = ((cg0 * tp[o2 + 1] // 255) * ta
                                                          + buf[o + 1] * ia) // 255
                                            buf[o + 2] = ((cb0 * tp[o2 + 2] // 255) * ta
                                                          + buf[o + 2] * ia) // 255
                                        zbuf[idx] = z
                        eab += eab_dx
                        ebc += ebc_dx
                        eca += eca_dx
                        u += du_dx
                        v += dv_dx
                    eab_r += eab_dy
                    ebc_r += ebc_dy
                    eca_r += eca_dy
                    u_r += du_dy
                    v_r += dv_dy

        self.pos = end
        if end >= self.total:
            self.done = True
            if self.show_bones:
                self._draw_bones()
            return True
        return False

    def _draw_bones(self):
        buf = self.buf
        W, H = self.W, self.H
        ctr, rot = self.ctr, self.rot
        ox, oy, fit = self.ox, self.oy, self.fit
        for bp in self.mesh.bones:
            v = _view_point(bp, ctr, rot)
            ix, iy = int(ox + v[0] * fit), int(oy - v[1] * fit)
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    if abs(dx) + abs(dy) > 3:
                        continue
                    px, py = ix + dx, iy + dy
                    if 0 <= px < W and 0 <= py < H:
                        o = (py * W + px) * 3
                        buf[o] = 220
                        buf[o + 1] = 40
                        buf[o + 2] = 40


def render(mesh, size=560, yaw=0.0, pitch=0.0, zoom=1.0, show_bones=False,
           bg=(247, 249, 252), pan=(0.0, 0.0), max_tris=None):
    """一次性软件光栅化（离屏 / CLI 用）；GUI 实时预览走 Rasterizer 分片推进。

    size 可以是整数（正方形）或 (W, H)，返回 (W, H, buf)。
    """
    if not mesh.verts or not mesh.tris:
        return None
    r = Rasterizer(mesh, size, yaw=yaw, pitch=pitch, zoom=zoom,
                   show_bones=show_bones, bg=bg, pan=pan, max_tris=max_tris)
    r.step()
    return (r.W, r.H, r.buf)


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
