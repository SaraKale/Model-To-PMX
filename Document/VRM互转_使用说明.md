# VRM ↔ PMX 互转说明

纯 Python 标准库实现，不依赖 Blender、mmd_tools、Unity、UniVRM、pygltflib。
（GitHub 上那个 `aia7520-gif/pmx2vrm` 是 Blender + mmd_tools + VRM 插件的方案，
需要装 Blender 和两个插件；这里直接读写二进制，不需要任何外部程序。）

## 工具清单

| 文件 | 作用 |
|---|---|
| `formats/pmxio.py` | 完整 PMX 2.0 读写（含表情 offset、IK、付与、刚体、关节） |
| `formats/vrmio.py` | GLB/glTF 容器读写 + PNG 编码 + BMP/TGA 解码 |
| `convert/pmx2vrm.py` | **PMX → VRM（1.0 / 0.x）** |
| `convert/vrm2pmx.py` | **VRM（1.0 / 0.x）→ PMX** |
| `gfx/preview.py` | 实时 3D 正面预览控件（GUI 调用） |

## 常用命令

```powershell
# PMX → VRM 1.0（默认）
python convert/pmx2vrm.py "model.pmx" -o "model.vrm"

# PMX → VRM 0.x（老软件/老 SDK 才需要）
python convert/pmx2vrm.py "model.pmx" --spec 0x --title "名字" --author "作者"

# VRM → PMX（贴图会自动导出到 PMX 同目录）
python convert/vrm2pmx.py "model.vrm" -o "model.pmx"

# 校验 + 渲染预览
python convert/pmx_check.py "model.pmx"
```

`pmx2vrm.py` 主要参数：

| 参数 | 说明 |
|---|---|
| `--spec 1.0` / `--spec 0x` | VRM 规范版本，默认 1.0 |
| `--scale auto` | 默认。归一化到 1.6 m；也可给具体倍率 |
| `--rotate auto` | 默认按版本决定；`none` / `y180` 可强制 |
| `--flip-winding` | 强制反转三角形绕序（默认**自动判定**，通常不需要） |
| `--force-double-sided` | 所有材质强制双面（仍有镂空时的兜底） |
| `--max-morphs N` | 只导出前 N 个表情；`0` = 完全不导出 |
| `--title` / `--author` | 写进 VRM meta |

`vrm2pmx.py` 主要参数：`--scale`（默认归一化到 20 MMD 单位）、`--rotate`、
`--flip-winding`、`--edge`（开启 MMD 轮廓线，默认关闭）、
`--force-double-sided`、`--name`。

## 坐标系（这部分决定了模型朝向对不对）

| 格式 | 正面 | 上 | 正面缠绕（从外面看） |
|---|---|---|---|
| PMX（MMD，左手系） | **-Z** | +Y | **逆时针** |
| VRM 0.x（glTF 右手系） | **-Z** | +Y | **逆时针** |
| VRM 1.0（glTF 右手系） | **+Z** | +Y | **逆时针** |

因此：

* PMX → VRM 0.x：坐标原样搬运。
* PMX → VRM 1.0：绕 Y 轴旋转 180°（`(x,y,z) → (-x, y, -z)`）。
* **绕序不反转** —— PMX 和 glTF 都是「从外面看逆时针」，约定一致。
  转换器默认用「几何面法线 vs 顶点法线」投票自动判定，日志里会打出
  `绕序自动判定：与法线同向 N 面 / 反向 M 面`。
* 尺寸：MMD 约 20 单位 ≈ 1.6 m，所以换算倍率约 0.08（VRM 侧 1 单位 = 1 米）。

### 绕序判定的实测依据

坐标系的结论不要靠推理，用数据说话，两个都很好测：

1. **正面朝向**：用脚尖骨骼减脚踝骨骼。
   云岫飞袂：`左つま先.Z − 左足ＩＫ.Z = −1.195` → 脚尖朝 −Z → PMX 正面 −Z。
   真实 VRM 0.x（VRoid 素体 / KizunaAI）：`leftToes.Z − leftFoot.Z = −0.095` → 同样 −Z。
2. **绕序**：拿每个三角形的几何法线（`(v1−v0)×(v2−v0)`）去点乘顶点法线，投票。
   抽样 6 个模型（YYBMiku / Rikka / 夕刻ロベル / シエン / ScaleMMD / 云岫飞袂）
   都是压倒性同向（例如 47340 : 202）→ 从外面看逆时针，与 glTF 一致。

> 早期版本硬编码「必须反转绕序」，结果模型被背面剔除，表现就是
> **大面积镂空 + 轮廓线糊成一片黑**。现在已改为自动判定。

### 万一还有镂空

先看日志里 `绕序自动判定` 那行。如果确实判定错了（反向面占多数），
手动加 `--flip-winding`。如果是模型本身有薄片几何（头发、裙摆、飘带）
依赖双面渲染，就加 `--force-double-sided`，或勾选界面上的「材质强制双面」。

如果导入某个软件后发现**模型背对你**，把 `--rotate` 手动反过来；
如果发现**模型破面/内外翻转**，加 `--keep-winding`。

## PMX → VRM 的映射

| PMX | VRM / glTF |
|---|---|
| 骨骼层级 | glTF 节点（只有平移，无旋转无缩放，符合 VRM 的 T-pose 约束） |
| 骨骼绝对坐标 | `skin.inverseBindMatrices`（纯平移的逆） |
| 顶点权重 BDEF1/2/4、SDEF、QDEF | `JOINTS_0`（ushort×4）+ `WEIGHTS_0`（float×4，已归一化） |
| 材质 | glTF PBR 材质（`baseColorFactor` + `baseColorTexture`）；0.x 另外写一份 MToon `materialProperties` |
| 贴图 | 嵌入 GLB。PNG/JPEG 直接透传，BMP/TGA 现转成 PNG；不支持的（DDS 等）跳过 |
| 顶点表情（含グループ展开） | morph target + VRM expression |
| 表情名 | 自动对应 VRM 预设（まばたき→blink、あ→aa、怒り→angry …） |

骨骼名 → VRM humanoid 的对应（下半身→hips、上半身→spine、上半身2→chest、
首→neck、頭→head、左/右腕→UpperArm、左/右ひじ→LowerArm、左/右手首→Hand、
左/右足→UpperLeg、左/右ひざ→LowerLeg、左/右足首→Foot、左/右つま先→Toes、
左/右目→Eye、手指 親指/人指/中指/薬指/小指→对应 finger bone）。

VRM 1.0 有 15 根**必需**骨骼，缺哪根会自动生成 `vrm_xxx` 占位节点
（挂在规范要求的父节点下），保证文件一定合法可加载。

## VRM → PMX 的映射

| VRM / glTF | PMX |
|---|---|
| skin joints | 骨骼（按 DFS 重排，父在前；humanoid 里登记但未蒙皮的骨骼也会保留） |
| 节点世界平移 | 骨骼位置（`tail` 指向第一个子骨骼，叶子骨骼按父子方向给偏移） |
| JOINTS_0/WEIGHTS_0 | BDEF1 / BDEF2 / BDEF4 |
| glTF 材质 | PMX 材质（diffuse 取 `baseColorFactor`，**不使用 toon**：`toon_flag=0` + `toon=-1`） |
| 嵌入贴图 | 导出成 `<pmx名>_texNN.png` 放在 PMX 同目录 |
| expression / blendShape | PMX 顶点表情（组表情会按权重叠加展开） |
| `extras.targetNames` 里没被引用的 target | 也会单独建一个 PMX 表情 |

## 已知限制

1. **不支持的贴图格式**：DDS / KTX2 / WebP 会被跳过（材质退化成纯色）。
2. **物理**：PMX 刚体、关节不会转成 VRM SpringBone；反过来 VRM 的
   SpringBone / node constraint 也不会转成 PMX 刚体。
3. **材质效果**：球谐贴图（.sph/.spa）、toon 贴图在 VRM 侧不保留；
   MToon 的高级参数只有 0.x 会写一份默认值，1.0 用的是标准 PBR。
   反方向（VRM → PMX）本工具**一律不使用 Toon**，材质写成 `toon_flag=0` + `toon=-1`
   （PMXEditor / MMD 里显示为「なし」）。

   ⚠️ 顺便记一个坑：PMX 材质的 toon 字段宽度由「共有Toonフラグ」决定，写反了会给每个材质
   硬套一层 `toon01.bmp`：

   | flag | 含义 | 字段 |
   |---|---|---|
   | `1` | 共享 / 内建 toon | **1 字节编号**，引用 MMD 的 `Data/toon01.bmp` … `toon10.bmp`，**编号 0 就是 `toon01.bmp`** |
   | `0` | 本模型纹理表里的贴图 | 纹理索引宽度，**`-1` = なし（不使用）** |

   「编号 0 = `toon00.bmp` = 不使用」是误解 —— MMD 的 `Data/` 目录里没有 `toon00.bmp`，
   mmd_tools 源码里写死了 `toon%02d.bmp % (shared + 1)`。本工具 2026-09-24 之前就是错的那个写法。
4. **骨骼表情**：PMX 的骨骼表情（kind 2）在 VRM 里没有对应物，会被忽略。
5. **体型差异**：VRM 要求 T-pose，MMD 模型多是微微 A-pose 的直臂，
   导入动作重定向软件时可能需要手动调一下手臂角度。
6. 表情多的大模型，VRM 的 morph target 是密集存储（`N表情 × 顶点数 × 12 字节`），
   文件会明显变大。可以用 `--max-morphs` 限制数量。

## 版权

VRM 的 meta 里带授权信息。默认写成「需要署名、禁止再分发、禁止改版」，
`allowRedistribution` / `modification` / `commercialUsage` 等字段按最保守的填。
**转换前请确认你有权使用该模型**，工具不会替你做版权检查。
