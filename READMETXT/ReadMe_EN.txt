Model Converter

You can convert and output various model format files to .pmx files.

Supported conversion directions:
FBX / unitypackage → PMX
VRM (0.x / 1.0) → PMX
PMX → VRM (0.x / 1.0)
uemodel → PMX
PMX → uemodel
psk / pskx → PMX 
PMX →FBX
PMX → psk / pskx
XPS → PMX

If you need the macOS or Linux versions, please download them here:
https://github.com/SaraKale/Model-To-PMX/releases

Usage:
1. Double-click to run ModelConvert.exe
2. Drag and drop your file here, or click the drag-and-drop area to select a file.
3. The "Task" dropdown box allows manual specification of the conversion direction, with automatic determination by file extension as the default.
4. The conversion results and logs are located at the bottom left, while the model preview occupies the entire right column.

Issue related:
- There are black lines on the edges of the converted model: This tool will turn off MMD contour lines by default when outputting PMX.
 If you still see black borders, select all materials in MMD/PMXEditor, ensure that the outline lines are turned off, and check "Double-Sided Drawing".
- There are still hollowed-out parts: If there are, please check "Force Double-Sided Material" in the interface.

Known limitations:
- FBX structure: Only binary FBX 7.x is supported; ASCII FBX is not supported.
- Sticker formats: DDS/KTX2/WebP will be skipped (textures degrade to solid colors).
- Physics: PMX rigid body/joint ↔ VRM SpringBone cannot be converted to each other.
- Material effect: .sph, .spa, and toon textures are not retained in VRM.
- Bone names: FBX conversion retains English bone names (Hips, Spine, etc.). Directly applying ready-made MMD motions (.vmd) will not match, so they need to be batch-renamed to standard Japanese names in PMXEditor.
You can use the PEBoneStorage plugin I developed; see:：
https://www.bilibili.com/video/BV1MfYX6JEhE/
https://studio.youtube.com/video/zZj-I4YgIDY/edit
https://www.nicovideo.jp/watch/sm46811122
- Expression: When the source model does not have BlendShape/morph, the PMX expression is also 0 and needs to be created manually.

Icon Design and Testing：
沙拉酱、小桃子

Feedback:
If you still encounter issues, please contact me below to provide feedback and a sample file.
https://github.com/SaraKale
https://space.bilibili.com/4197121
https://www.aplaybox.com/u/269621856
https://x.com/SaraKale9

------------------------

Update history:

2026-10-01 v1.1.4：Added the XPS → PMX option.
2026-09-27 v1.1.3: Fixed the scaling issue after converting uemodel to PMX, and added an option in UE to automatically align to the reference model.
2026-09-25 v1.1.2:Added PMX → FBX and PMX → psk/pskx options; the output files will be saved in a folder with the same name.
2026-09-24 v1.1.1: Added the psk/pskx → PMX option and removed toon.
2026-09-23 v1.1.0: Fixed some issues with FBX-to-PMX conversion.
2026-09-21 v1.0.9: Fixed the uemodel (ZSTD) to PMX conversion issue.
2026-09-19 v1.0.8: Added functionality for converting .uemodel files back and forth, and fixed several issues.
2026-09-18 v1.0.7: Fixed some issues with VRM-to-PMX conversion.
2026-09-18 v1.0.5–v1.0.6: Fixed issues with PMX-to-VRM conversion.
2026-09-18 v1.0.4: Corrected an error when loading MMD files in UTF-16 encoding, fixed the logic of the conversion button, and added a file list box.
2026-09-17 v1.0.3: Fixed the bone inversion issue in the unitypackage.
2026-09-15 v1.0.2: Fixed the model preview interface.
2026-09-13 v1.0.1: Fixed the model preview interface.
2026-09-13 v1.0.0: Released.

by:SaraKale