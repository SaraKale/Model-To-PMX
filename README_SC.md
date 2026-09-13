# FBX / VRM / PMX 模型转换器（纯 Python）

把 `.fbx`、`.unitypackage`、`.vrm`、`.pmx` 互相转换，**全部用 Python 标准库实现**。
不需要 Blender / Autodesk FBX SDK / Unity 3D，也不需要 mmd_tools / UniVRM 等插件。直接读写二进制格式，拖进窗口即可转换。

[English](README.md) | [简体中文](README_SC.md) | [繁體中文](README_TC.md) | [日本語](README_JP.md)
---

## 下载

请从 [releases](https://github.com/SaraKale/Model-to-PMX/releases/latest) 下载最新版本。

## 一、功能特点

### 支持的转换方向

| 方向 | 说明 |
|---|---|
| FBX / unitypackage → PMX | MMD 用 PMX 2.0，四边形自动扇形三角化 |
| VRM（0.x / 1.0） → PMX | 自动识别 humanoid 骨骼，缺的补占位骨 |
| PMX → VRM（0.x / 1.0） | 自动写 VRM meta、humanoid 骨骼映射、morph target |
| PMX → 仅校验 + 预览 | 只读结构校验，不输出文件 |

### 核心特性

- **零外部依赖**：纯 Python 标准库（tkinter + ctypes）。`Pillow` 为可选加速项，
  缺失只影响贴图解码速度与部分预览，不影响主流程。
- **拖放即用**：走 Windows 原生 `WM_DROPFILES`，窗口任意位置都能拖，无需
  tkinterdnd2 之类的第三方库。
- **实时背视图预览**：拖入模型即可看到 3D 背视图，可拖动旋转 / 滚轮缩放，
  可叠加骨骼点检查对齐。
- **多语言界面**：右上角可切换 简体中文 / 繁體中文 / English / 日本語，选择记忆到配置。
- **高分屏友好**：按系统 DPI 自动缩放，右上角「界面缩放」可手动指定倍率。
- **分页式选项**：选项区按「通用 / FBX / VRM / 输出」分为四个标签页，易于扩展。
- **自动绕序判定**：三角形绕序用「几何面法线 vs 顶点法线」投票自动判定，
  避免出现大面积镂空 / 轮廓线糊成黑块。
- **贴图处理**：PNG/JPEG 直接透传，BMP/TGA 现转 PNG，嵌入 GLB 或导出到 PMX 同目录。

---

## 二、环境要求

- **Windows**（拖放与 GUI 依赖 Windows 原生 API）
- **Python 3.10+**（推荐 3.12 / 3.13 / 3.14，需自带 tkinter）
- **可选**：`Pillow`（加速贴图解码与预览）

---

## 三、目录结构

```
Model-to-PMX/
├── main.py                      # ★ 图形界面入口（完整实现）
├── main.spec                    # ★ PyInstaller 打包配置
├── config.json                  # 界面设置（语言 / 缩放 / 选项等）
│
├── formats/                     # 格式读写 / 解包
│   ├── fbx_reader.py            #   二进制 FBX 7.x 解析库
│   ├── fbx_probe.py             #   探查 FBX 结构（网格/骨骼/蒙皮清单）
│   ├── pmxio.py                 #   完整 PMX 2.0 读写（含表情/IK/付与/刚体/关节）
│   ├── vrmio.py                 #   GLB/glTF 容器读写 + PNG 编码 + BMP/TGA 解码
│   └── unitypackage_unpack.py   #   解包 .unitypackage（gzip tar）
│
├── convert/                     # 转换引擎
│   ├── fbx2pmx.py               #   FBX → PMX
│   ├── vrm2pmx.py               #   VRM → PMX
│   ├── pmx2vrm.py               #   PMX → VRM
│   └── pmx_check.py             #   PMX 校验 + 软件渲染预览图
│
├── gfx/
│   └── preview.py               # 实时 3D 背视图预览控件
│
```

> 引擎模块已按职责归入 `formats/` `convert/` `gfx/` 子目录，运行时由 `main.py`
> 把这三个目录插入 `sys.path`，因此模块间仍用裸名 `import`（如 `import fbx2pmx`）。

---

## 四、使用方法

### 方式一：图形界面（推荐）

1. 输入运行 `python main.py`
2. 把 `.fbx` / `.unitypackage` / `.vrm` / `.pmx` 文件**拖进窗口任意位置**，或点击拖放区选择文件。
1. 「任务」下拉框可手动指定转换方向，默认按扩展名自动判断。
2. 转换结果与日志在左下，模型预览是右栏整列。

界面要点：

- **左右分栏**：像 Blender 那样可以拖中间的分割条调整宽度，位置会记进 `config.json`
- **右栏整列预览**：背视图实时预览，底部工具条有 正视 / 左视 / 背视 / 俯视 / 复位 / 自转 / 骨骼 / 线框
- **右下角渲染后端**：显示当前用的是 `Pillow` 还是纯 Python，以及上一帧耗时（毫秒）
- **右上角「语言」**：简体中文 / 繁體中文 / English / 日本語（预览工具条也跟着切）
- **右上角「界面缩放」**：自动跟随系统 DPI，或手动指定倍率
- **选项分页**：「通用 / FBX / VRM / 输出」四个标签页
- **复选项已加大**：勾选框与点击区域更易点击

### 预览为什么这么快（大模型也不卡）

实时预览是纯 Python 软光栅（没有 OpenGL），所以做了三层配合：

1. **拖动时用抽稀网格**：自动抽到约 1.2 万个面做交互帧，90k 面的模型一帧约 40ms；
2. **停手后分片精修**：精修图按每片 1500 个面推进，每个时间片最多占用 24ms，
   画面逐块变清晰，界面不会整块卡住；
3. **像素预算自适应**：按实测帧耗时自动升降渲染分辨率；装了 Pillow 还会用它
   把低分辨率结果放大到画布尺寸（C 实现）。

### 方式二：命令行

> 路径含空格或中文时务必加双引号；引擎脚本位于 `convert/` `formats/` 子目录。

```powershell
# 打开图形界面
python main.py

# FBX → PMX
python convert/fbx2pmx.py "model.fbx" -o "model.pmx"

# 解包 unitypackage（--list 只列内容不解包）
python formats/unitypackage_unpack.py "pack.unitypackage" --list
python formats/unitypackage_unpack.py "pack.unitypackage"

# 探查 FBX 结构
python formats/fbx_probe.py "model.fbx"

# PMX → VRM 1.0（默认）/ 0.x
python convert/pmx2vrm.py "model.pmx" -o "model.vrm"
python convert/pmx2vrm.py "model.pmx" --spec 0x --title "名字" --author "作者"

# VRM → PMX（贴图自动导出到 PMX 同目录）
python convert/vrm2pmx.py "model.vrm" -o "model.pmx"

# PMX 校验 + 生成预览图
python convert/pmx_check.py "model.pmx"
python convert/pmx_check.py "model.pmx" --bones   # 叠加骨骼位置
```

#### 常用参数速查

`fbx2pmx.py`：

| 参数 | 说明 |
|---|---|
| `--scale mmd` | 默认，自动缩放到 MMD 标准身高（约 20 单位） |
| `--scale raw` | 保持 FBX 原始尺寸（米） |
| `--scale 12.5` | 手动指定缩放倍数 |
| `--no-flip-z` | 不做右手系→左手系转换（默认会转） |
| `--info` | 只打印结构信息，不转换 |

`pmx2vrm.py`：`--spec 1.0|0x`、`--scale auto|倍率`、`--rotate auto|none|y180`、
`--flip-winding`、`--force-double-sided`、`--max-morphs N`、`--title` / `--author`。

`vrm2pmx.py`：`--scale`、`--rotate`、`--flip-winding`、`--edge`（开启轮廓线）、
`--force-double-sided`、`--name`。

更详细的参数与格式映射，见 `FBX转PMX_使用说明.md` 与 `VRM互转_使用说明.md`。

---

## 五、编译与打包

### 1. 安装 PyInstaller

```powershell
pip install pyinstaller
pip install pillow          # 可选，让打包进去的版本也有贴图加速
```

### 2. 使用 spec 打包（推荐）

> **务必走 spec**，不要直接 `pyinstaller main.py`。

```powershell
pyinstaller main.spec
```

产物：`dist/ModelConvert.exe/ModelConvert.exe`（one-folder 模式）。

### 3. 为什么必须用 spec

引擎模块已归入 `formats/` `convert/` `gfx/` 子目录，而 `main.py` 里用的是裸名
`import fbx2pmx` 这种写法，子目录是在**运行期**才通过 `sys.path.insert` 加进去的。
**PyInstaller 只做静态分析，看不到运行期的路径注入**——如果 `pathex` 里没有这些
子目录，打包阶段会把它们当"找不到的模块"直接丢弃，程序能打包成功，但一运行就报：

```
ModuleNotFoundError: No module named 'fbx2pmx'
```

`main.spec` 里已经正确配置好：

```python
Analysis(
    ['main.py'],
    pathex=['.', 'formats', 'convert', 'gfx'],     # 让静态分析找到子目录模块
    hiddenimports=['fbx2pmx', 'pmx_check', 'pmx2vrm', 'vrm2pmx', 'preview',
                   'unitypackage_unpack', 'fbx_reader', 'pmxio', 'vrmio'],
    ...
)
```

如需不用 spec，等价写法是：

```powershell
pyinstaller --paths formats --paths convert --paths gfx -w main.py
```

### 4. 常用参数

| 参数 | 说明 |
|---|---|
| `-F` | 打包成单个可执行文件 |
| `-D` | 打包成包含多个文件的文件夹（默认） |
| `-w` | 隐藏控制台窗口（GUI 应用） |
| `-i icon.ico` | 指定可执行文件图标 |
| `-n 名称` | 指定生成的可执行文件名称 |
| `--add-data "源:目标"` | 添加资源文件 |
| `--hidden-import 模块名` | 手动补充隐藏依赖 |

---

## 六、注意事项

### 通用

1. **路径含空格 / 中文**：命令行调用时一律加双引号。
2. **设置文件 `config.json`**：记录语言、界面缩放、任务方向、各选项等，删除后会以默认值重建。
1. **快捷键与交互**：预览区可拖动旋转、滚轮缩放；拖放区点击可打开文件选择框。

### 模型全黑 / 镂空

- **转换后模型全黑**：在 MMD / PMXEditor 里选中全部材质**关掉轮廓线**，再勾选**「双面描绘」**。
- **仍有镂空**：先看日志里 `绕序自动判定` 那行。判定错了加 `--flip-winding`；薄片几何（头发 / 裙摆）依赖双面渲染则加 `--force-double-sided`，或勾选界面的「材质强制双面」。

### 已知限制

- **FBX 结构**：仅支持二进制 FBX 7.x；ASCII FBX 不支持。
- **贴图格式**：DDS / KTX2 / WebP 会被跳过（材质退化为纯色）。
- **物理**：PMX 刚体/关节 ↔ VRM SpringBone **不会互相转换**。
- **材质效果**：球谐贴图（.sph/.spa）、toon 贴图在 VRM 侧不保留。
- **骨骼名**：FBX 转换保留英文骨骼名（`Hips`、`Spine` …），直接套 MMD 现成动作（.vmd）匹配不上，需在 PMXEditor 里批量改为日文标准名。
- **表情**：源模型没有 BlendShape / morph 时，PMX 表情也为 0，需手工建。

### 拖放相关

- 拖放依赖 Windows 原生 `WM_DROPFILES`，**仅在 Windows 下可用**；其他系统请用「浏览」按钮选择文件。
- 若日志提示「系统未启用拖放接口」，说明当前环境不支持原生拖放，改用按钮选择即可。

### 打包相关

1. **必须用 `pyinstaller main.spec`**（或带 `--paths` 的等价命令），否则会漏掉子目录里的引擎模块，运行时报 `No module named 'fbx2pmx'`。
1. **改了源码后必须重新打包**，直接双击旧 exe 不会生效。
2. 打包环境的 Python 必须带 tkinter（Windows 官方安装包默认包含）。

---

## 七、版权提醒

VRM 的 meta 里带授权信息，本工具默认写成最保守的「需要署名、禁止再分发、禁止改版」。

**转换前请自行确认你有权使用该模型**，工具不会替你做版权检查。
