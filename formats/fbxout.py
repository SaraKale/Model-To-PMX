# -*- coding: utf-8 -*-
"""fbxout.py - 把网格 + 骨骼 + 蒙皮写成 **ASCII FBX 7.4**（纯标准库）。

为什么要 ASCII：二进制 FBX 要做 deflate + 节点表 + 32/64 位偏移宽度，
坑多且难调试（本工程读 FBX 时就踩过 7400=32 位偏移的坑）。ASCII FBX 是
纯文本，任何 DCC（Blender / Maya / 3ds Max / Unity / UE）都认，出问题能直接
肉眼看字节。

坐标系（与工程的 FBX 读取路径**互为逆操作**，可自校验）
    fbx = (x, y, -z)_pmx        —— 即读取侧的「Z 取反」
    PMX 左手系、fbx 右手系 ⇒ 手性和绕序一起翻，所以三角形要**反转绕序**。
    GlobalSettings 里如实声明 UpAxis=Y / FrontAxis=Z(sign +1) / CoordAxis=X，
    这样 Blender、Maya 之流会按声明把模型摆正（角色在 Blender 里仍朝 -Y）。
    用本工程的 FBX→PMX 路径（Z 轴翻转=开）再读回来，应当与源 PMX 一致。

蒙皮约定（FBX SDK 的老规矩）
    对每个 Cluster：TransformLink = 绑定姿势下骨骼的世界矩阵 W，
                    Transform     = W⁻¹
    于是 TransformLink · Transform = I ⇒ 顶点本来就在绑定姿势里，静止时不变形；
    摆姿势时按 A_bone · (Transform · v) 的公式走。绑定姿势的骨骼只有平移
    （PMX 的骨骼本来就不带旋转），所以两个矩阵都是纯平移。
    矩阵按**行主序**写 16 个数，平移落在最后一行（第 13/14/15 个）。

用法（也可直接当命令行工具，接 .pmx）：
    python fbxout.py model.pmx -o model.fbx
"""

import argparse
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))


def _log_default(msg, tag=None):
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("utf-8", "replace").decode("ascii", "replace"))


def _f(v):
    """FBX 里的浮点写法：够短又不丢精度。"""
    x = float(v)
    if x == int(x) and abs(x) < 1e15:
        return str(int(x))
    return "%.6g" % x


def _arr(w, indent, values):
    """把整个数组写成一条 `a: ...` 行。

    Autodesk 自己的 ASCII FBX 就是这么写的 —— 顶点表几 MB 全塞在一行里很正常。
    早期版本按固定条数换行、每行重复写一次 `a:` 标签，虽然也有工具这么干，
    但那样会让不少第三方解析器（以及本工程自己）在续行处多吃/漏吃数值，
    所以统一改成一条行最保险。
    """
    w("%sa: %s" % (indent, ",".join(values)))


def _esc(s):
    return (s or "").replace("\\", "\\\\").replace('"', '\\"')


def _san(s):
    """名字里的非 ASCII 会被某些工具当成编码问题，统一换成下划线。"""
    out = []
    for ch in (s or ""):
        out.append(ch if 32 < ord(ch) < 127 else "_")
    nm = "".join(out).strip() or "unnamed"
    return nm[:120]


def _name(prefix, s):
    """带 `Class::` 命名空间的名字（FBX 里的传统写法）。"""
    return "%s::%s" % (prefix, _san(s))


def _clip(s, n=120):
    """名字清洗：去掉控制字符（换行会破坏 FBX 的行结构）并截断。

    与 `_san` 不同，这里**保留非 ASCII** —— 表情名用日文/中文很常见，
    而 FBX 文本本身就是 UTF-8，SDK 与 Blender 都能正确解码。
    """
    s = (s or "").strip()
    s = "".join(ch for ch in s if ord(ch) >= 32)
    return s[:n]


class _Ids(object):
    def __init__(self, start=1000000, step=100000):
        self.n = start
        self.step = step

    def block(self):
        base = self.n
        self.n += self.step
        return base


def _weights_of(vertex):
    """PMX 顶点 → [(骨骼下标, 权重)]。

    PMX 的约定：wt=1(BDEF2) 只存 w1，w2 = 1-w1；wt=3(SDEF) 同 BDEF2；
    wt=4(QDEF) 同 BDEF4。把补位权重丢掉，只留真正参与的点。
    """
    wt = vertex.get("wtype", 0)
    bs = list(vertex.get("wbones") or [])
    ws = list(vertex.get("wweights") or [])
    out = []
    if wt in (0,) or not bs:
        return [(bs[0] if bs else 0, 1.0)]
    if wt in (1, 3):
        w1 = ws[0] if ws else 1.0
        out = [(bs[0], w1), (bs[1] if len(bs) > 1 else bs[0], 1.0 - w1)]
    else:                                   # 2=BDEF4 / 4=QDEF
        for k in range(min(len(bs), 4)):
            w = ws[k] if k < len(ws) else 0.0
            if w > 1e-6:
                out.append((bs[k], w))
    out = [(b, w) for b, w in out if w > 1e-6]
    return out or [(0, 1.0)]


def export_model(path, verts, faces, materials, bones, morphs=None,
                 mirror_z=True, reverse_winding=True, tex_dir=None, log=None,
                 creator="ueformat2pmx", name=None, model_name="model",
                 flip_v=True):
    """写出 ASCII FBX。

    verts      : PMX 顶点 dict 列表（pos/normal/uv/wtype/wbones/wweights）
    faces      : 展平的三角索引
    materials  : PMX 材质 dict 列表（用 name/diffuse/tex）
    bones      : PMX 骨骼 dict 列表（用 name/pos/parent）
    morphs     : PMX 表情 dict 列表；kind=1（顶点表情）会写成
                 BlendShape/BlendShapeChannel/Shape —— 只写真正动过的控制点，
                 数值是**增量**（FBX 文件里 Shape 的 Vertices 就是这个语义）。
                 骨骼/UV/材质表情 FBX 装不下，跳过。
    model_name : 网格节点名（默认 "model"，传模型名更友好）
    flip_v     : 是否把 UV 的 V 轴翻过来（**默认必须翻**，见下面注释）
    返回统计 dict。
    """
    _l = log or _log_default
    sx = 1.0
    sz = -1.0 if mirror_z else 1.0
    if name:
        model_name = name
    model_name = _san(model_name)

    def xf(p):
        return (p[0] * sx, p[1], p[2] * sz)

    nv = len(verts)
    ntris = len(faces) // 3
    out = []
    w = out.append

    # ------------------------------------------------- 表情预处理（→ Shape）--
    # **FBX 文件里 Shape 的 Vertices 是「相对基础控制点的增量」，不是绝对坐标**
    # （实测确认：写全 0 → 读回等于原网格；写基础坐标 → 读回是两倍）。
    # FBX SDK / Blender 读取时会给未列在 Indexes 里的控制点补 0 增量，
    # 并把结果展开成与网格控制点等长的绝对坐标数组 —— 读取端因此**不需要**
    # 知道我们只写了变化点，插件侧的 FbxShape.GetControlPointAt 拿到的就是
    # 与网格一一对应的绝对坐标。
    # 于是这里只写「真正动过的控制点」：本工程样本 161 个表情、每个约 600 点，
    # 合计约 3 MB；若按全量写，光这一个模型就要 300 MB 以上。
    morph_shapes = []
    for mo in (morphs or []):
        if int(mo.get("kind", 1)) != 1:
            continue                     # 只搬顶点表情；骨骼/UV/材质表情 FBX 装不下
        pairs = {}
        for vi, d in (mo.get("offsets") or []):
            vi = int(vi)
            if 0 <= vi < nv:
                pairs[vi] = d
        if pairs:
            morph_shapes.append((mo, pairs))
    n_shape = len(morph_shapes)
    n_attr = len(bones) + 1              # 每根骨骼一个 + 网格一个

    # 每根骨骼影响哪些顶点（Cluster 用；顺带决定 Definitions 里 Deformer 的数量）
    per_bone = {}
    for vi, v in enumerate(verts):
        for b, wt in _weights_of(v):
            if 0 <= b < len(bones):
                per_bone.setdefault(b, []).append((vi, wt))
    n_cluster = len(per_bone)
    n_def = 1 + n_cluster + (1 + n_shape if n_shape else 0)

    hdr_axis = "Z" if mirror_z else "-Z"
    _l("FBX：%d 顶点 · %d 三角面 · %d 骨骼 · %d 材质（ASCII 7.4，%s-up，前方 %s）%s"
       % (nv, ntris, len(bones), len(materials), "Y", hdr_axis,
          ("· %d 个顶点表情写入 BlendShape" % n_shape) if n_shape else ""))

    ids = _Ids()
    gid = ids.block()                    # Geometry
    mid = ids.block()                    # Mesh Model
    bone_id0 = ids.block()               # 骨骼 Model 段
    mat_id0 = ids.block()                # Material 段
    tex_id0 = ids.block()                # Texture / Video 段
    def_id0 = ids.block()                # Deformer 段（Skin + Cluster）
    na_id0 = ids.block()                 # NodeAttribute 段（骨骼 + 网格）
    morph_id0 = ids.block()              # BlendShape / BlendShapeChannel / Shape 段
    bone_model = [bone_id0 + i for i in range(max(len(bones), 1))]

    # ------------------------------------------------------------ 头 + 设置 --
    w("; FBX 7.4.0 project file")
    w("; 由 %s 生成（纯 Python，ASCII FBX）" % creator)
    w("; ----------------------------------------------------")
    w("")
    t = time.localtime()
    w("FBXHeaderExtension:  {")
    w("\tFBXHeaderVersion: 1003")
    w("\tFBXVersion: 7400")
    w("\tCreationTimeStamp:  {")
    w("\t\tVersion: 1000")
    w("\t\tYear: %d" % t.tm_year)
    w("\t\tMonth: %d" % t.tm_mon)
    w("\t\tDay: %d" % t.tm_mday)
    w("\t\tHour: %d" % t.tm_hour)
    w("\t\tMinute: %d" % t.tm_min)
    w("\t\tSecond: %d" % t.tm_sec)
    w("\t\tMillisecond: 0")
    w("\t}")
    w('\tCreator: "%s"' % creator)
    w("}")
    w("")
    w("GlobalSettings:  {")
    w("\tVersion: 1000")
    w("\tProperties70:  {")
    w('\t\tP: "UpAxis", "int", "Integer", "",1')
    w('\t\tP: "UpAxisSign", "int", "Integer", "",1')
    w('\t\tP: "FrontAxis", "int", "Integer", "",2')
    w('\t\tP: "FrontAxisSign", "int", "Integer", "",%d'
      % (1 if mirror_z else -1))
    w('\t\tP: "CoordAxis", "int", "Integer", "",0')
    w('\t\tP: "CoordAxisSign", "int", "Integer", "",1')
    w('\t\tP: "OriginalUpAxis", "int", "Integer", "",1')
    w('\t\tP: "OriginalUpAxisSign", "int", "Integer", "",1')
    w('\t\tP: "UnitScaleFactor", "double", "Number", "",1')
    w('\t\tP: "OriginalUnitScaleFactor", "double", "Number", "",1')
    w('\t}')
    w("}")
    w("")

    # ------------------------------------------------------------ Definitions --
    n_tex = sum(1 for m in materials
                if isinstance(m.get("tex"), int) and m["tex"] >= 0)
    w("Definitions:  {")
    w("\tVersion: 100")
    # Count = 下面 ObjectType 块的**个数**：GlobalSettings / Model / Geometry /
    # Material / Deformer / NodeAttribute，有贴图时再加 Texture 与 Video。
    w("\tCount: %d" % (6 + (2 if n_tex else 0)))
    w('\tObjectType: "GlobalSettings" {')
    w("\t\tCount: 1")
    w("\t}")
    w('\tObjectType: "Model" {')
    w("\t\tCount: %d" % (1 + len(bones)))
    w("\t}")
    w('\tObjectType: "Geometry" {')
    w("\t\tCount: %d" % (1 + n_shape))
    w("\t}")
    # NodeAttribute 段不能省：**Model 的「类型」是由它挂着的 NodeAttribute
    # 对象决定的**，Model 行里的 "LimbNode" 只是个字符串。
    # 少了它，FBX SDK 侧 node.GetNodeAttribute() 返回 null（网格还能靠
    # Geometry 连接被 SDK 兜回来，骨骼却没有任何线索），任何按 SDK API
    # 判断骨骼的导入器都会一根骨头都收不到 —— PE 的 FBX 导入插件就是这样
    # 在 PE 里「只有网格、没有骨骼」的。
    w('\tObjectType: "NodeAttribute" {')
    w("\t\tCount: %d" % n_attr)
    w("\t}")
    if n_tex:
        w('\tObjectType: "Video" {')
        w("\t\tCount: %d" % n_tex)
        w("\t}")
        w('\tObjectType: "Texture" {')
        w("\t\tCount: %d" % n_tex)
        w("\t}")
    w('\tObjectType: "Material" {')
    w("\t\tCount: %d" % max(len(materials), 1))
    w("\t}")
    w('\tObjectType: "Deformer" {')
    w("\t\tCount: %d" % n_def)
    w("\t}")
    w("}")
    w("")

    # ---------------------------------------------------------------- Objects --
    w("Objects:  {")

    # Geometry
    w('\tGeometry: %d, "%s", "Mesh" {' % (gid, _name("Geometry", "mesh")))
    w("\t\tVertices: *%d  {" % (nv * 3))
    _arr(w, "\t\t\t", [_f(c) for v in verts for c in xf(v["pos"])])
    w("\t\t} ")
    # 三角形的顶点顺序。**PVI / 法线 / UVIndex 三处必须用同一个顺序**：
    # 法线与 UV 都是 ByPolygonVertex（每个多边形顶点一项，按多边形顶点顺序
    # 一一对应）。镜像手性时绕序要反转，但如果只反 PVI、不反 UVIndex 与
    # 法线，等于把每个三角形首末两个角的 UV/法线对调 —— 几何形状完全正确、
    # 贴图却整片错乱（用户报的「FBX 材质显示不对、同时导出的 PMX 正常」
    # 就是这个：实测 115563/115563 个三角形全部首末对调）。
    tris = []
    for t2 in range(ntris):
        a, b, c = faces[t2 * 3], faces[t2 * 3 + 1], faces[t2 * 3 + 2]
        if reverse_winding:
            a, b, c = c, b, a
        tris.append((a, b, c))

    w("\t\tPolygonVertexIndex: *%d  {" % (ntris * 3))
    buf = []
    for a, b, c in tris:
        buf.append("%d,%d,%d" % (a, b, -(c + 1)))    # 末位取反 = 多边形结束
    _arr(w, "\t\t\t", buf)
    w("\t\t} ")
    w("\t\tGeometryVersion: 124")

    # 法线：ByPolygonVertex（最兼容的写法）
    w("\t\tLayerElementNormal: 0 {")
    w("\t\t\tVersion: 101")
    w('\t\t\tName: ""')
    w('\t\t\tMappingInformationType: "ByPolygonVertex"')
    w('\t\t\tReferenceInformationType: "Direct"')
    w("\t\t\tNormals: *%d  {" % (ntris * 9))
    buf = []
    for tri in tris:
        for vi in tri:
            n = verts[vi]["normal"] if vi < nv else (0.0, 1.0, 0.0)
            buf.append("%s,%s,%s" % (_f(n[0] * sx), _f(n[1]), _f(n[2] * sz)))
    _arr(w, "\t\t\t\t", buf)
    w("\t\t\t} ")
    w("\t\t}")

    # UV：ByPolygonVertex + IndexToDirect
    # **V 轴必须翻**：MMD / PMX / UE 的 UV 原点在**左上**（v 向下），而 FBX 的
    # 通行约定（Autodesk / Blender / Maya）原点在**左下**（v 向上）。原样写出去，
    # 下游按 v 向上解释就把贴图上下颠倒了 —— 表现为形状完全正确、图案整体错位
    # （脸上出现嘴/脖子处的图、腿脚出现靴子、头发出现大片黑斑）。本工程的
    # fbx2pmx 读 FBX 时同样做 `1.0 - v`，两边对称，导入导出往返才能对上。
    w("\t\tLayerElementUV: 0 {")
    w("\t\t\tVersion: 101")
    w('\t\t\tName: "UVChannel_1"')
    w('\t\t\tMappingInformationType: "ByPolygonVertex"')
    w('\t\t\tReferenceInformationType: "IndexToDirect"')
    w("\t\t\tUV: *%d  {" % (nv * 2))
    buf = []
    for v in verts:
        u = v.get("uv") or (0.0, 0.0)
        vv = (1.0 - u[1]) if flip_v else u[1]
        buf.append("%s,%s" % (_f(u[0]), _f(vv)))
    _arr(w, "\t\t\t\t", buf)
    w("\t\t\t} ")
    w("\t\t\tUVIndex: *%d  {" % (ntris * 3))
    buf = []
    for tri in tris:
        buf.append("%d,%d,%d" % tri)
    _arr(w, "\t\t\t\t", buf)
    w("\t\t\t} ")
    w("\t\t}")

    # 逐面材质
    if materials:
        w("\t\tLayerElementMaterial: 0 {")
        w("\t\t\tVersion: 101")
        w('\t\t\tName: ""')
        w('\t\t\tMappingInformationType: "ByPolygon"')
        w('\t\t\tReferenceInformationType: "IndexToDirect"')
        w("\t\t\tMaterials: *%d  {" % ntris)
        per_mat = []
        for mi, mt in enumerate(materials):
            per_mat.extend([mi] * (max(0, int(mt.get("faces", 0))) // 3))
        if len(per_mat) < ntris:
            per_mat.extend([0] * (ntris - len(per_mat)))
        del per_mat[ntris:]
        _arr(w, "\t\t\t\t", [str(x) for x in per_mat])
        w("\t\t\t} ")
        w("\t\t}")

    w("\t\tLayer: 0 {")
    w("\t\t\tVersion: 100")
    w('\t\t\tLayerElement:  {')
    w('\t\t\t\tType: "LayerElementNormal"')
    w("\t\t\t\tTypedIndex: 0")
    w("\t\t\t}")
    w('\t\t\tLayerElement:  {')
    w('\t\t\t\tType: "LayerElementUV"')
    w("\t\t\t\tTypedIndex: 0")
    w("\t\t\t}")
    if materials:
        w('\t\t\tLayerElement:  {')
        w('\t\t\t\tType: "LayerElementMaterial"')
        w("\t\t\t\tTypedIndex: 0")
        w("\t\t\t}")
    w("\t\t}")
    w("\t}")

    # 骨骼：Model(LimbNode)，Lcl Translation = 相对父骨骼的位置（绑定姿势只有平移）
    bpos = [xf(b["pos"]) for b in bones]
    for bi, b in enumerate(bones):
        p = b.get("parent", -1)
        if 0 <= p < len(bones):
            d = (bpos[bi][0] - bpos[p][0], bpos[bi][1] - bpos[p][1],
                 bpos[bi][2] - bpos[p][2])
        else:
            d = bpos[bi]
        # 骨名写**裸名**、不加 `Model::` 前缀。本工程自己的 FBX→PMX 读取路径
        # 会把 Model 名字原样当骨名用，加前缀会让「导出再读回」的骨名变成
        # `Model::Bip001Pelvis`，导进 Blender / UE 看着也脏。
        w('\tModel: %d, "%s", "LimbNode" {' % (bone_model[bi], _san(b["name"])))
        w("\t\tVersion: 232")
        w("\t\tProperties70:  {")
        w('\t\t\tP: "InheritType", "enum", "", "",1')
        # **DefaultAttributeIndex 不能省**（网格 Model 一直有它，骨骼以前漏了）：
        # FBX SDK 只在 Model 显式声明这个属性之后，才会把 "OO" 连接上来的
        # NodeAttribute 挂到该节点上；缺了它 node.GetNodeAttribute() 永远是 null。
        # 这正是「骨骼在 Blender 里看得见（Blender 自己解析文件，不看这个属性），
        # 在 PE 的 FBX 导入插件里却一根都没有（插件走 FBX SDK 的 API）」的原因。
        # 实测：同一份文件只补这一行，eSkeleton 节点数 0 → 489。
        w('\t\t\tP: "DefaultAttributeIndex", "int", "Integer", "",0')
        w('\t\t\tP: "Lcl Translation", "Lcl Translation", "", "A",%s,%s,%s'
          % (_f(d[0]), _f(d[1]), _f(d[2])))
        w('\t\t\tP: "Lcl Rotation", "Lcl Rotation", "", "A",0,0,0')
        w('\t\t\tP: "Lcl Scaling", "Lcl Scaling", "", "A",1,1,1')
        w('\t\t}')
        w('\t\tShading: T')
        w('\t\tCulling: "CullingOff"')
        w("\t}")

    # 网格节点
    w('\tModel: %d, "%s", "Mesh" {' % (mid, _san(model_name)))
    w("\t\tVersion: 232")
    w("\t\tProperties70:  {")
    w('\t\t\tP: "InheritType", "enum", "", "",1')
    w('\t\t\tP: "DefaultAttributeIndex", "int", "Integer", "",0')
    w("\t\t}")
    w("\t\tShading: T")
    w('\t\tCulling: "CullingOff"')
    w("\t}")

    # 节点属性：骨骼 → Skeleton，网格 → Mesh。名字照 Autodesk 的习惯留空
    # （`"NodeAttribute::"`），类型由第三个字符串决定。
    for bi in range(len(bones)):
        w('\tNodeAttribute: %d, "NodeAttribute::", "LimbNode" {' % (na_id0 + bi))
        w('\t\tTypeFlags: "Skeleton"')
        w("\t}")
    w('\tNodeAttribute: %d, "NodeAttribute::", "Mesh" {' % (na_id0 + len(bones)))
    w('\t\tTypeFlags: "Mesh"')
    w("\t\tGeometryVersion: 124")
    w("\t}")

    # 材质（+ 贴图 Video/Texture）
    mat_ids = []
    tex_ids = []
    for mi, mt in enumerate(materials or [{"name": "material_0",
                                           "diffuse": (1.0, 1.0, 1.0, 1.0),
                                           "tex": -1}]):
        myid = mat_id0 + mi
        mat_ids.append(myid)
        diff = list(mt.get("diffuse") or (1.0, 1.0, 1.0, 1.0))
        alpha = diff[3] if len(diff) > 3 else 1.0
        w('\tMaterial: %d, "%s", "" {' % (myid, _name("Material", mt.get("name"))))
        w("\t\tVersion: 102")
        w('\t\tShadingModel: "phong"')
        w("\t\tMultiLayer: 0")
        w("\t\tProperties70:  {")
        w('\t\t\tP: "DiffuseColor", "Color", "", "A",%s,%s,%s'
          % (_f(diff[0]), _f(diff[1]), _f(diff[2])))
        w('\t\t\tP: "DiffuseFactor", "Number", "", "A",1')
        w('\t\t\tP: "TransparencyFactor", "Number", "", "A",%s'
          % _f(1.0 - max(0.0, min(1.0, alpha))))
        w('\t\t\tP: "SpecularColor", "Color", "", "A",0,0,0')
        w('\t\t\tP: "SpecularFactor", "Number", "", "A",0')
        w('\t\t\tP: "ShininessExponent", "Number", "", "A",0')
        w('\t\t\tP: "EmissiveColor", "Color", "", "A",0,0,0')
        w('\t\t\tP: "AmbientColor", "Color", "", "A",0.5,0.5,0.5')
        w("\t\t}")
        w("\t}")
        ti = mt.get("tex", -1)
        if isinstance(ti, int) and ti >= 0:
            tex_ids.append((mi, ti))

    # 贴图（引用文本文件名，不内嵌二进制）
    tex_files = {}
    if tex_ids and tex_dir is not None:
        folder_idx = {}
        try:
            for fn in os.listdir(tex_dir):
                folder_idx.setdefault(fn.lower(), fn)
        except OSError:
            pass
        for mi, ti in tex_ids:
            vid = tex_id0 + mi * 2
            tid = vid + 1
            # 文件名由调用方通过 materials[i]["_tex_file"] 传入；没有就按序号找
            fn = materials[mi].get("_tex_file")
            if not fn:
                hits = [v for k, v in folder_idx.items()
                        if k.endswith(".png") and ("tex%02d" % ti) in k]
                fn = hits[0] if hits else None
            if not fn:
                continue
            rel = "textures/" + fn
            tex_files[mi] = rel
            w('\tVideo: %d, "Video::tex%d", "Clip" {' % (vid, mi))
            w("\t\tType: \"Clip\"")
            w("\t\tProperties70:  {")
            w('\t\t\tP: "Path", "KString", "XRefUrl", "", "%s"' % _esc(rel))
            w("\t\t}")
            w("\t\tUseMipMap: 0")
            w('\t\tFilename: "%s"' % _esc(rel))
            w('\t\tRelativeFilename: "%s"' % _esc(rel))
            w("\t}")
            w('\tTexture: %d, "Texture::tex%d", "" {' % (tid, mi))
            w('\t\tType: "TextureVideoClip"')
            w("\t\tVersion: 202")
            w('\t\tTextureName: "Texture::tex%d"' % mi)
            w('\t\tMedia: "Video::tex%d"' % mi)
            w('\t\tFileName: "%s"' % _esc(rel))
            w('\t\tRelativeFilename: "%s"' % _esc(rel))
            w("\t\tModelUVTranslation: 0,0")
            w("\t\tModelUVScaling: 1,1")
            w('\t\tTexture_Alpha_Source: "None"')
            w("\t\tCropping: 0,0,0,0")
            w("\t}")

    # 蒙皮：Skin + 每个骨骼一个 Cluster
    skin_id = def_id0
    w('\tDeformer: %d, "Deformer::Skin", "Skin" {' % skin_id)
    w("\t\tVersion: 101")
    w("\t\tLink_DeformAcuracy: 50")
    w("\t}")

    # per_bone / n_cluster 已经在前面算好（Definitions 里要用到）
    for bi in sorted(per_bone):
        pairs = sorted(per_bone[bi])
        cx = bpos[bi]
        w('\tDeformer: %d, "SubDeformer::%s", "Cluster" {'
          % (skin_id + 1 + bi, _san(bones[bi]["name"])))
        w("\t\tVersion: 100")
        w('\t\tUserData: "", ""')
        w("\t\tIndexes: *%d  {" % len(pairs))
        _arr(w, "\t\t\t", [str(x[0]) for x in pairs])
        w("\t\t} ")
        w("\t\tWeights: *%d  {" % len(pairs))
        _arr(w, "\t\t\t", [_f(x[1]) for x in pairs])
        w("\t\t} ")
        # 行主序 4x4：TransformLink = W（骨骼世界），Transform = W⁻¹
        w("\t\tTransform: *16  {")
        w("\t\t\ta: " + ",".join(["1", "0", "0", "0", "0", "1", "0", "0",
                                  "0", "0", "1", "0",
                                  _f(-cx[0]), _f(-cx[1]), _f(-cx[2]), "1"]))
        w("\t\t} ")
        w("\t\tTransformLink: *16  {")
        w("\t\t\ta: " + ",".join(["1", "0", "0", "0", "0", "1", "0", "0",
                                  "0", "0", "1", "0",
                                  _f(cx[0]), _f(cx[1]), _f(cx[2]), "1"]))
        w("\t\t} ")
        w("\t}")

    # 表情：BlendShape → BlendShapeChannel（一个表情一个）→ Shape（增量）
    # 名字一律写**裸名**（和骨名一样，不加 `SubDeformer::` / `Shape::` 前缀）：
    #   · FBX SDK 侧 channel.GetName() 会把前缀剥掉，两种写法结果相同；
    #   · 本工程自己的 fbx2pmx 直接取字段原文，写裸名才不会把表情名变成
    #     "Shape::L_P"；
    #   · Blender 的 shape key 名字也直接取这个字段。
    # 名字里的非 ASCII（日文表情名很常见）**保留原样** —— FBX 文本是 UTF-8，
    # SDK 与 Blender 都能正确解码，换成下划线反而让用户在 PE 里认不出表情。
    shape_ids = {}
    if n_shape:
        w('\tDeformer: %d, "BlendShape", "BlendShape" {' % morph_id0)
        w("\t\tVersion: 101")
        w("\t}")
    for k, (mo, pairs) in enumerate(morph_shapes):
        cid = morph_id0 + 1 + k * 2
        sid = cid + 1
        shape_ids[k] = (cid, sid)
        nm = _esc(_clip(mo.get("name"))) or ("morph_%d" % k)
        w('\tDeformer: %d, "%s", "BlendShapeChannel" {' % (cid, nm))
        w("\t\tVersion: 100")
        # 字段名是 DeformPercent（不是 DeformerPercent）——后者会被 SDK 当成
        # 未知字段默默丢掉，通道权重就一直是 0。
        w("\t\tDeformPercent: 0")
        # FullWeights = 每个 target shape 的默认权重（百分比）。SDK 导出时必写。
        w("\t\tFullWeights: *1  {")
        w("\t\t\ta: 100")
        w("\t\t} ")
        w("\t}")
        keys = sorted(pairs)
        w('\tGeometry: %d, "%s", "Shape" {' % (sid, nm))
        w("\t\tVersion: 100")
        w("\t\tIndexes: *%d  {" % len(keys))
        _arr(w, "\t\t\t", [str(x) for x in keys])
        w("\t\t} ")
        w("\t\tVertices: *%d  {" % (len(keys) * 3))
        buf = []
        for vi in keys:
            d = xf(pairs[vi])
            buf.append("%s,%s,%s" % (_f(d[0]), _f(d[1]), _f(d[2])))
        _arr(w, "\t\t\t", buf)
        w("\t\t} ")
        w("\t}")
    w("}")
    w("")

    # ------------------------------------------------------------ Connections --
    w("Connections:  {")
    w('\tC: "OO",%d,0' % mid)
    w('\tC: "OO",%d,%d' % (gid, mid))
    w('\tC: "OO",%d,%d' % (skin_id, gid))
    # 每个 Model 挂自己的 NodeAttribute（没有它 SDK 认不出节点类型）
    w('\tC: "OO",%d,%d' % (na_id0 + len(bones), mid))
    for bi in range(len(bones)):
        w('\tC: "OO",%d,%d' % (na_id0 + bi, bone_model[bi]))
    # 表情链：Shape → Channel → BlendShape → Geometry
    if n_shape:
        w('\tC: "OO",%d,%d' % (morph_id0, gid))
        for k in range(n_shape):
            cid, sid = shape_ids[k]
            w('\tC: "OO",%d,%d' % (cid, morph_id0))
            w('\tC: "OO",%d,%d' % (sid, cid))
    for bi in range(len(bones)):
        p = bones[bi].get("parent", -1)
        if 0 <= p < len(bones):
            w('\tC: "OO",%d,%d' % (bone_model[bi], bone_model[p]))
        else:
            w('\tC: "OO",%d,0' % bone_model[bi])
        if bi in per_bone:
            w('\tC: "OO",%d,%d' % (skin_id + 1 + bi, skin_id))
            w('\tC: "OO",%d,%d' % (bone_model[bi], skin_id + 1 + bi))
    for mi, myid in enumerate(mat_ids):
        w('\tC: "OO",%d,%d' % (myid, mid))
        if mi in tex_files:
            tid = tex_id0 + mi * 2 + 1
            w('\tC: "OP",%d,%d, "DiffuseColor"' % (tid, myid))
    w("}")
    w("")
    w("Takes:  {")
    w('\tCurrent: ""')
    w("}")
    w("")

    data = "\n".join(out)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(data)
    os.replace(tmp, path)
    st = {"bytes": len(data.encode("utf-8")), "vertices": nv, "tris": ntris,
          "bones": len(bones), "materials": len(materials or []),
          "clusters": n_cluster, "textures": len(tex_files),
          "shapes": n_shape, "morphs": len(morphs or [])}
    _l("FBX：%d 个 Cluster · %d 个表情 Shape · 引用贴图 %d 张 · %.2f MB"
       % (n_cluster, n_shape, len(tex_files), st["bytes"] / 1048576.0))
    return st


def export_pmx(pmx_path, fbx_path, log=None, mirror_z=True):
    """直接读 PMX 再写 FBX（命令行用）。"""
    import pmxio
    m = pmxio.read_pmx(pmx_path)
    tex_dir = os.path.join(os.path.dirname(os.path.abspath(pmx_path)), "textures")
    for mt in m["materials"]:
        ti = mt.get("tex", -1)
        if isinstance(ti, int) and 0 <= ti < len(m["textures"]):
            mt["_tex_file"] = os.path.basename(m["textures"][ti])
    model_name = (m.get("name") or "").strip() or \
        os.path.splitext(os.path.basename(pmx_path))[0]
    return export_model(fbx_path, m["vertices"], m["faces"], m["materials"],
                        m["bones"], m["morphs"], mirror_z=mirror_z,
                        reverse_winding=True, tex_dir=tex_dir, log=log,
                        creator="fbxout.py", model_name=model_name)


def main(argv=None):
    ap = argparse.ArgumentParser(description="PMX → ASCII FBX 7.4（纯 Python）")
    ap.add_argument("src")
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--no-mirror", action="store_true",
                    help="不取反 Z（默认取反，与工程 FBX 读取路径互逆）")
    a = ap.parse_args(argv)
    out = a.out or (os.path.splitext(a.src)[0] + ".fbx")
    st = export_pmx(a.src, out, log=lambda m_, t=None: print(m_),
                    mirror_z=not a.no_mirror)
    print("完成：%s（%.2f MB）" % (out, st["bytes"] / 1048576.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
