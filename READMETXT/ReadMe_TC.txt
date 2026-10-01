模型轉換器

可以把各種模型格式文件轉換輸出到 .pmx文件。

支持的轉換方向：
FBX / unitypackage → PMX
VRM（0.x / 1.0） → PMX 
PMX → VRM（0.x / 1.0）
uemodel → PMX
PMX → uemodel
psk / pskx → PMX 
PMX →FBX
PMX → psk / pskx
XPS → PMX

如果需要macOS、Linux版本，請到這裏下載：
https://github.com/SaraKale/Model-To-PMX/releases

使用方法：
1. 雙擊運行 ModelConvert.exe
2. 把文件拖進來，或點擊拖放區選擇文件。
3. 「任務」下拉框可手動指定轉換方向，默認按擴展名自動判斷。
4. 轉換結果與日誌在左下方，模型預覽是右欄整列。

問題相關：
- 轉換後模型邊緣有黑色線條：本工具默認會在輸出 PMX 時關閉 MMD 輪廓線。
  若仍看到黑邊，請在 MMD / PMXEditor 裏選中全部材質，確認輪廓線已關閉併勾選「雙面描繪」。
- 仍有鏤空：如果有鏤空請勾選界面的「材質強製雙面」。

已知限製：
- FBX 結構：僅支持二進製 FBX 7.x；ASCII FBX 不支持。
- 貼圖格式：DDS / KTX2 / WebP 會被跳過（材質退化為純色）。
- 物理：PMX 剛體/關節 ↔ VRM SpringBone 不會互相轉換。
- 材質效果：.sph .spa .toon 貼圖在 VRM 不保留。
- 骨骼名：FBX 轉換保留英文骨骼名（Hips、Spine …），直接套 MMD 現成動作（.vmd）匹配不上，需在 PMXEditor 裏批量改為日文標準名。可以使用我製作的PE骨骼整理插件，詳見：
https://www.bilibili.com/video/BV1MfYX6JEhE/
https://studio.youtube.com/video/zZj-I4YgIDY/edit
https://www.nicovideo.jp/watch/sm46811122
- 表情：源模型沒有 BlendShape / morph 時，PMX 表情也為 0，需手工創建。

圖標設計和測試：
沙拉酱、小桃子

反饋：
如果仍然有問題，請到下面與我聯系反饋併提供樣本文件。
https://github.com/SaraKale
https://space.bilibili.com/4197121
https://www.aplaybox.com/u/269621856
https://x.com/SaraKale9

---------------------------

更新歷史：

2026-10-01 v1.1.4：新增 XPS → PMX 選項。
2026-09-27 v1.1.3：修復 uemodel 轉換PMX後比例問題，UE選項增加自動對齊到參考模型選項。
2026-09-25 v1.1.2：新增 PMX →FBX，PMX → psk / pskx選項，輸出文件會輸出到同名文件夾。
2026-09-24 v1.1.1：新增 psk / pskx →PMX 選項，併去除toon。
2026-09-23 v1.1.0：修復部分 FBX→PMX 轉換問題
2026-09-21 v1.0.9：修復 uemodel（ZSTD）→PMX 轉換問題
2026-09-19 v1.0.8：增加 .uemodel 文件互轉功能，修復部分問題。
2026-09-18 v1.0.7：修復部分 VRM→PMX 轉換問題
2026-09-18 v1.0.5-v1.0.6：修復 PMX→VRM 轉換問題
2026-09-18 v1.0.4：修正MMD載入UTF-16報錯的問題，修正轉換按鈕的邏輯，增加文件列表框。
2026-09-17 V1.0.3：修復unitypackage的骨骼傾倒問題
2026-09-15 v1.0.2：修復模型預覽界面
2026-09-13 v1.0.1：修復模型預覽界面
2026-09-13 v1.0.0：發布

by:SaraKale