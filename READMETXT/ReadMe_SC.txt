模型转换器

可以把各种模型格式文件转换输出到 .pmx文件。

支持的转换方向：
FBX / unitypackage → PMX
VRM（0.x / 1.0） → PMX 
PMX → VRM（0.x / 1.0）
uemodel → PMX
PMX → uemodel
psk / pskx → PMX 
PMX →FBX
PMX → psk / pskx
XPS → PMX

如果需要macOS、Linux版本，请到这里下载：
https://github.com/SaraKale/Model-To-PMX/releases

使用方法：
1. 双击运行 ModelConvert.exe
2. 把文件拖进来，或点击拖放区选择文件。
3. 「任务」下拉框可手动指定转换方向，默认按扩展名自动判断。
4. 转换结果与日志在左下方，模型预览是右栏整列。

问题相关：
- 转换后模型边缘有黑色线条：本工具默认会在输出 PMX 时关闭 MMD 轮廓线。
  若仍看到黑边，请在 MMD / PMXEditor 里选中全部材质，确认轮廓线已关闭并勾选「双面描绘」。
- 仍有镂空：如果有镂空请勾选界面的「材质强制双面」。

已知限制：
- FBX 结构：仅支持二进制 FBX 7.x；ASCII FBX 不支持。
- 贴图格式：DDS / KTX2 / WebP 会被跳过（材质退化为纯色）。
- 物理：PMX 刚体/关节 ↔ VRM SpringBone 不会互相转换。
- 材质效果：.sph .spa .toon 贴图在 VRM 不保留。
- 骨骼名：FBX 转换保留英文骨骼名（Hips、Spine …），直接套 MMD 现成动作（.vmd）匹配不上，需在 PMXEditor 里批量改为日文标准名。可以使用我制作的PE骨骼整理插件，详见：
https://www.bilibili.com/video/BV1MfYX6JEhE/
https://studio.youtube.com/video/zZj-I4YgIDY/edit
https://www.nicovideo.jp/watch/sm46811122
- 表情：源模型没有 BlendShape / morph 时，PMX 表情也为 0，需手工创建。

图标设计和测试：
沙拉酱、小桃子

反馈：
如果仍然有问题，请到下面与我联系反馈并提供样本文件。
https://github.com/SaraKale
https://space.bilibili.com/4197121
https://www.aplaybox.com/u/269621856
https://x.com/SaraKale9

---------------------------

更新历史：

2026-10-01 v1.1.4：新增 XPS → PMX 选项。
2026-09-27 v1.1.3：修复 uemodel 转换PMX后比例问题，UE选项增加自动对齐到参考模型选项。
2026-09-25 v1.1.2：新增 PMX →FBX，PMX → psk / pskx选项，输出文件会输出到同名文件夹。
2026-09-24 v1.1.1：新增 psk / pskx →PMX 选项，并去除toon。
2026-09-23 v1.1.0：修复部分 FBX→PMX 转换问题
2026-09-21 v1.0.9：修复 uemodel（ZSTD）→PMX 转换问题
2026-09-19 v1.0.8：增加 .uemodel 文件互转功能，修复部分问题。
2026-09-18 v1.0.7：修复部分 VRM→PMX 转换问题
2026-09-18 v1.0.5-v1.0.6：修复 PMX→VRM 转换问题
2026-09-18 v1.0.4：修正MMD载入UTF-16报错的问题，修正转换按钮的逻辑，增加文件列表框。
2026-09-17 V1.0.3：修复unitypackage的骨骼倾倒问题
2026-09-15 v1.0.2：修复模型预览界面
2026-09-13 v1.0.1：修复模型预览界面
2026-09-13 v1.0.0：发布

by:SaraKale