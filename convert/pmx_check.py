"""pmx_check.py - validate a PMX 2.0 file and render a preview PNG.

Pure standard library: no numpy, no PIL. Parses the PMX structure, reports
counts, sanity-checks every index, and software-rasterises two orthographic
views so you can eyeball whether the conversion is intact.

Usage:
    python pmx_check.py "model.pmx"
    python pmx_check.py "model.pmx" -o preview.png --size 640
"""
import argparse
import math
import os
import struct
import sys
import zlib


class Reader:
    def __init__(self, d, enc):
        self.d = d
        self.p = 0
        self.enc = enc

    def i32(self):
        v = struct.unpack_from("<i", self.d, self.p)[0]
        self.p += 4
        return v

    def f32(self):
        v = struct.unpack_from("<f", self.d, self.p)[0]
        self.p += 4
        return v

    def u8(self):
        v = self.d[self.p]
        self.p += 1
        return v

    def text(self):
        n = self.i32()
        s = self.d[self.p:self.p + n]
        self.p += n
        return s.decode("utf-16-le" if self.enc == 0 else "utf-8", "replace")

    def idx(self, size, signed=True):
        if size == 1:
            v = struct.unpack_from("<b" if signed else "<B", self.d, self.p)[0]
            self.p += 1
        elif size == 2:
            v = struct.unpack_from("<h" if signed else "<H", self.d, self.p)[0]
            self.p += 2
        else:
            v = self.i32()
            if not signed:
                v &= 0xFFFFFFFF
        return v


def read_pmx(path):
    d = open(path, "rb").read()
    assert d[:4] == b"PMX ", "not a PMX file"
    version = struct.unpack_from("<f", d, 4)[0]
    r = Reader(d, 0)
    r.p = 8
    gc = r.u8()
    g = [r.u8() for _ in range(gc)]
    enc, add_uv, vi_s, ti_s, mi_s, bi_s, moi_s, ri_s = g
    r.enc = enc

    out = {"version": version, "globals": g, "add_uv": add_uv,
           "vi_s": vi_s, "ti_s": ti_s, "mi_s": mi_s, "bi_s": bi_s}
    out["name"] = r.text()
    out["name_en"] = r.text()
    out["comment"] = r.text()
    out["comment_en"] = r.text()

    # vertices
    nv = r.i32()
    verts = []
    for _ in range(nv):
        px, py, pz = r.f32(), r.f32(), r.f32()
        nx, ny, nz = r.f32(), r.f32(), r.f32()
        u, v = r.f32(), r.f32()
        for _ in range(add_uv):
            for _ in range(4):
                r.f32()
        wt = r.u8()
        nb = {0: 1, 1: 2, 2: 4, 3: 2, 4: 4}.get(wt)
        if nb is None:
            raise ValueError("顶点 %d 的变形类型字节 %d 非法（文件解析失步）"
                             % (len(verts), wt))
        bones = [r.idx(bi_s) for _ in range(nb)]
        nw = {0: 0, 1: 1, 2: 4, 3: 1, 4: 4}[wt]
        ws = [r.f32() for _ in range(nw)]
        # SDEF(3) 在权重之后还有 C / R0 / R1 三个 vec3 —— 漏读会让后面
        # 所有顶点全部错位（表现为 wt 读到 'K'=75 之类的天书字节）。
        sdef = None
        if wt == 3:
            sdef = ((r.f32(), r.f32(), r.f32()),
                    (r.f32(), r.f32(), r.f32()),
                    (r.f32(), r.f32(), r.f32()))
        r.f32()  # edge scale
        verts.append(((px, py, pz), (nx, ny, nz), (u, v), wt, bones, ws, sdef))
    out["vertices"] = verts
    if os.environ.get("PMX_DEBUG"):
        print("[dbg] after verts   p=%d" % r.p)

    # faces
    nf = r.i32()
    faces = [r.idx(vi_s, signed=False) for _ in range(nf)]
    out["faces"] = faces
    if os.environ.get("PMX_DEBUG"):
        print("[dbg] after faces   p=%d (nf=%d)" % (r.p, nf))

    # textures
    ntex = r.i32()
    out["textures"] = [r.text() for _ in range(ntex)]

    # materials
    nm = r.i32()
    mats = []
    for _ in range(nm):
        name = r.text(); name_en = r.text()
        diff = [r.f32() for _ in range(4)]
        spec = [r.f32() for _ in range(3)]
        shin = r.f32()
        amb = [r.f32() for _ in range(3)]
        flag = r.u8()
        edge_col = [r.f32() for _ in range(4)]
        edge_size = r.f32()
        tex = r.idx(mi_s)
        sph = r.idx(mi_s)
        sph_mode = r.u8()
        toon_flag = r.u8()
        toon = r.idx(mi_s) if toon_flag == 0 else r.u8()
        memo = r.text()
        fcount = r.i32()
        mats.append({"name": name, "diffuse": diff, "flag": flag,
                     "tex": tex, "faces": fcount})
    out["materials"] = mats
    if os.environ.get("PMX_DEBUG"):
        print("[dbg] after materials p=%d (nm=%d)" % (r.p, nm))

    # bones
    nbn = r.i32()
    if os.environ.get("PMX_DEBUG"):
        print("[dbg] bones count = %d at p=%d" % (nbn, r.p))
    bones = []
    for _ in range(nbn):
        bname = r.text(); bname_en = r.text()
        pos = (r.f32(), r.f32(), r.f32())
        parent = r.idx(bi_s)
        layer = r.i32()
        bflag = struct.unpack_from("<H", d, r.p)[0]
        r.p += 2
        if bflag & 0x0001:
            tail = ("bone", r.idx(bi_s))
        else:
            tail = ("offset", (r.f32(), r.f32(), r.f32()))
        # PMX 2.0 bone flags:
        #   0x0002 rotatable  0x0004 movable  0x0008 visible  0x0010 operable
        #   0x0020 IK         0x0100 inherit-rot 0x0200 inherit-trans
        #   0x0400 fixed axis 0x0800 local axis  0x2000 external parent
        # 付与親/付与率是回転・移動共通的一组：任一 flag 置位只读一次，
        # 读两遍会多走 6 字节，从第一根双付与骨（如センター 0x031E）开始错位。
        if bflag & 0x0100 or bflag & 0x0200:
            r.idx(bi_s); r.f32()
        if bflag & 0x0400:
            r.f32(); r.f32(); r.f32()
        if bflag & 0x0800:
            for _ in range(6):
                r.f32()
        if bflag & 0x2000:
            r.i32()                     # 外部親Key 只有 4 字节
        if bflag & 0x0020:
            # IK: ターゲット(ボーンIndex) / ループ回数(int) /
            #     1回あたりの制限角度(float) / リンク数(int)
            r.idx(bi_s); r.i32(); r.f32()
            nl = r.i32()
            for _ in range(nl):
                r.idx(bi_s)                 # リンクボーンIndex
                if r.u8():                  # 角度制限フラグ（每个 link 交错）
                    for _ in range(6):      # 下限 float×3 + 上限 float×3
                        r.f32()
        bones.append({"name": bname, "pos": pos, "parent": parent,
                      "flag": bflag, "layer": layer, "tail": tail})
    out["bones"] = bones

    # morphs
    nmo = r.i32()
    morphs = []
    for _ in range(nmo):
        mname = r.text(); mname_en = r.text()
        panel = r.u8(); kind = r.u8()
        noff = r.i32()
        if kind == 0:                       # グループ
            for _ in range(noff):
                r.idx(moi_s); r.f32()
        elif kind == 1:                     # 頂点（顶点索引 + 3 个偏移）
            for _ in range(noff):
                r.idx(vi_s, signed=False)
                r.f32(); r.f32(); r.f32()
        elif kind == 2:                     # ボーン
            for _ in range(noff):
                r.idx(bi_s); r.f32(); r.f32(); r.f32()
                r.f32(); r.f32(); r.f32(); r.f32()
        elif kind in (3, 4, 5, 6, 7):       # UV / 追加UV1-4（4 个偏移）
            for _ in range(noff):
                r.idx(vi_s, signed=False)
                r.f32(); r.f32(); r.f32(); r.f32()
        elif kind == 8:                     # 材質
            for _ in range(noff):
                r.idx(mi_s); r.u8()
                for _ in range(28):
                    r.f32()
        elif kind == 9:                     # フリップ
            for _ in range(noff):
                r.idx(moi_s); r.f32()
        elif kind == 10:                    # インパルス（刚体索引）
            for _ in range(noff):
                r.idx(ri_s)
                r.u8()                  # ローカルフラグ
                r.f32(); r.f32(); r.f32()
                r.f32(); r.f32(); r.f32()
        morphs.append({"name": mname, "kind": kind, "count": noff})
    out["morphs"] = morphs

    # display frames
    nd = r.i32()
    frames = []
    for _ in range(nd):
        fname = r.text(); fname_en = r.text()
        special = r.u8()
        ni = r.i32()
        items = []
        for _ in range(ni):
            t = r.u8()
            # 要素対象：0 = ボーン, 1 = モーフ
            if t == 0:
                items.append(("bone", r.idx(bi_s), None))
            else:
                items.append(("morph", r.idx(moi_s), None))
        frames.append({"name": fname, "special": special, "items": items})
    out["frames"] = frames

    # 刚体：名/英名/关联骨骼/グループ/非衝突グループ(2)/形状/サイズ/位置/回転/
    #      質量/移動減衰/回転減衰/反発力/摩擦力/物理演算
    nrb = r.i32()
    out["rigid_bodies"] = nrb
    for _ in range(nrb):
        r.text(); r.text()
        r.idx(bi_s)
        r.u8()
        r.p += 2
        r.u8()
        for _ in range(14):
            r.f32()
        r.u8()
    # 关节：名/英名/種類/剛体A/剛体B/8 个 vec3
    nj = r.i32()
    out["joints"] = nj
    for _ in range(nj):
        r.text(); r.text()
        r.u8()
        r.idx(ri_s); r.idx(ri_s)
        for _ in range(24):
            r.f32()
    out["consumed"] = r.p
    out["filesize"] = len(d)
    return out


# ---------------------------------------------------------------- render ----

def write_png(path, w, h, rows):
    raw = bytearray()
    for r in rows:
        raw.append(0)
        for px in r:
            raw += bytes(px)

    def chunk(typ, data):
        return (struct.pack(">I", len(data)) + typ + data +
                struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)))
        f.write(chunk(b"IDAT", zlib.compress(raw, 6)))
        f.write(chunk(b"IEND", b""))


def render(model, size=560, bg=(250, 250, 250), show_bones=False):
    verts = model["vertices"]
    faces = model["faces"]
    if not verts or not faces:
        return None

    xs = [v[0][0] for v in verts]
    ys = [v[0][1] for v in verts]
    zs = [v[0][2] for v in verts]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    minz, maxz = min(zs), max(zs)

    SCALE = 0.52
    view_axis = 2       # 2 = looking along +Z (front view)
    sub = (int(maxx - minx) - int(minx), int(miny - miny) - int(miny))

    span = max(maxx - minx, maxy - miny) or 1.0
    k = size * SCALE * 2 / span

    def project(p):
        # front view: screen x = -X (mirror, MMD looks at -Z), screen y = Y
        sx = (p[0] - (minx + maxx) / 2) * k + size / 2
        sy = size / 2 - (p[1] - (miny + maxy) / 2) * k
        return sx, sy, p[2]

    img = [[list(bg) for _ in range(size)] for _ in range(size)]
    zbuf = [[-1e30] * size for _ in range(size)]

    L = (0.45, 0.72, 0.53)
    ln = math.sqrt(L[0] ** 2 + L[1] ** 2 + L[2] ** 2)
    L = (L[0] / ln, L[1] / ln, L[2] / ln)

    pts = [project(v[0]) for v in verts]
    nrm = [v[1] for v in verts]

    for i in range(0, len(faces) - 2, 3):
        a, b, c = faces[i], faces[i + 1], faces[i + 2]
        pa, pb, pc = pts[a], pts[b], pts[c]
        # backface test in screen space
        area = ((pb[0] - pa[0]) * (pc[1] - pa[1]) -
                (pc[0] - pa[0]) * (pb[1] - pa[1]))
        if area <= 0:
            continue
        n = nrm[a]
        lam = abs(n[0] * L[0] + n[1] * L[1] + n[2] * L[2])
        shade = 0.35 + 0.65 * lam
        col = (int(214 * shade), int(198 * shade), int(190 * shade))
        x0 = max(0, int(min(pa[0], pb[0], pc[0])))
        x1 = min(size - 1, int(max(pa[0], pb[0], pc[0])) + 1)
        y0 = max(0, int(min(pa[1], pb[1], pc[1])))
        y1 = min(size - 1, int(max(pa[1], pb[1], pc[1])) + 1)
        if x1 < x0 or y1 < y0:
            continue
        d = (pb[1] - pc[1]) * (pa[0] - pc[0]) + (pc[0] - pb[0]) * (pa[1] - pc[1])
        if abs(d) < 1e-9:
            continue
        inv = 1.0 / d
        for py in range(y0, y1 + 1):
            cy = py + 0.5
            row = img[py]
            zrow = zbuf[py]
            for px in range(x0, x1 + 1):
                cx = px + 0.5
                w0 = ((pb[1] - pc[1]) * (cx - pc[0]) +
                      (pc[0] - pb[0]) * (cy - pc[1])) * inv
                if w0 < 0:
                    continue
                w1 = ((pc[1] - pa[1]) * (cx - pc[0]) +
                      (pa[0] - pc[0]) * (cy - pc[1])) * inv
                if w1 < 0:
                    continue
                w2 = 1.0 - w0 - w1
                if w2 < 0:
                    continue
                z = w0 * pa[2] + w1 * pb[2] + w2 * pc[2]
                if z > zrow[px]:
                    zrow[px] = z
                    row[px] = [col[0], col[1], col[2]]

    if show_bones:
        for b in model["bones"]:
            sx, sy, _ = project(b["pos"])
            ix, iy = int(sx), int(sy)
            for dx in range(-2, 3):
                for dy in range(-2, 3):
                    if abs(dx) + abs(dy) > 3:
                        continue
                    px, py = ix + dx, iy + dy
                    if 0 <= px < size and 0 <= py < size:
                        img[py][px] = [220, 40, 40]
    return img


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pmx")
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--size", type=int, default=560)
    ap.add_argument("--no-render", action="store_true")
    ap.add_argument("--bones", action="store_true",
                    help="overlay bone positions as red markers")
    a = ap.parse_args()

    m = read_pmx(a.pmx)
    print("file      : %s" % a.pmx)
    print("version   : %.1f   globals=%s" % (m["version"], m["globals"]))
    enc = m["globals"][0] if m.get("globals") else -1
    if enc == 0:
        print("encoding  : UTF-16LE  （MMD 可载入）")
    elif enc == 1:
        print("encoding  : UTF-8  ← MMD 无法载入这种 PMX，"
              "需要转成 UTF-16LE（pmxio.py 可以直接转）")
    else:
        print("encoding  : 未知取值 %d" % enc)
    print("name      : %s / %s" % (m["name"], m["name_en"]))
    print("vertices  : %d" % len(m["vertices"]))
    print("faces     : %d indices = %d triangles" % (len(m["faces"]), len(m["faces"]) // 3))
    print("textures  : %d" % len(m["textures"]))
    print("materials : %d" % len(m["materials"]))
    print("bones     : %d" % len(m["bones"]))
    print("morphs    : %d" % len(m["morphs"]))
    print("display   : %d frames" % len(m["frames"]))
    print("rigid/joint: %d / %d" % (m["rigid_bodies"], m["joints"]))
    print("bytes read: %d / %d  %s"
          % (m["consumed"], m["filesize"],
             "OK" if m["consumed"] == m["filesize"] else "MISMATCH"))

    nv = len(m["vertices"])
    nb = len(m["bones"])
    bad_v = sum(1 for f in m["faces"] if f >= nv)
    bad_b = 0
    for v in m["vertices"]:
        for b in v[4]:
            if b >= nb or b < 0:
                bad_b += 1
    print("index check: face->vertex out-of-range=%d  weight->bone out-of-range=%d"
          % (bad_v, bad_b))

    wsum_bad = 0
    for v in m["vertices"]:
        wt, ws = v[3], v[5]
        if wt == 0:
            s = 1.0
        elif wt in (1, 3):                 # BDEF2 / SDEF: w1 + (1 - w1)
            s = ws[0] + (1.0 - ws[0])
        else:                              # BDEF4 / QDEF
            s = sum(ws)
        if abs(s - 1.0) > 0.02:
            wsum_bad += 1
    print("weight sums off by >2%%: %d" % wsum_bad)

    if m["materials"]:
        tot = sum(mm["faces"] for mm in m["materials"])
        print("material face coverage: %d of %d indices" % (tot, len(m["faces"])))
        unused = [mm["name"] for mm in m["materials"] if mm["faces"] == 0]
        if unused:
            print("  materials with 0 faces: %s" % unused)

    if a.no_render:
        return 0
    out = a.out or (os.path.splitext(a.pmx)[0] + "_preview.png")
    img = render(m, size=a.size, show_bones=a.bones)
    if img is None:
        print("nothing to render")
        return 1
    write_png(out, a.size, a.size, img)
    print("\npreview -> %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
