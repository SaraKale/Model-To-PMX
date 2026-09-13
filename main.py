# -*- coding: utf-8 -*-
"""模型转换器图形界面 - FBX / unitypackage / VRM / PMX 互转。

把 .fbx、.unitypackage、.vrm、.pmx 拖进窗口即可自动转换，不需要 Blender、
不需要 FBX SDK、不需要 Unity，全部是纯 Python。

支持的方向：
    FBX / unitypackage → PMX
    VRM（0.x / 1.0）    → PMX
    PMX                → VRM（0.x / 1.0）
    PMX                → 仅校验 + 预览

依赖：仅 Python 标准库（tkinter + ctypes）。
拖放走 Windows 原生 WM_DROPFILES，不需要安装 tkinterdnd2 之类的第三方库。

界面会按系统 DPI 自动缩放（高分屏上不再是小字）。右上角「界面缩放」可以
手动指定倍率，选择会记进同目录的「config.json」。

用法：
    双击同目录的「PMX转换器.bat」
    或  python PMX转换器.py
"""
import json
import os
import queue
import struct
import sys
import threading
import traceback

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import tkinter as tk
from tkinter import filedialog, messagebox
import tkinter.ttk as ttk

# 引擎模块已归类到 formats / convert / gfx 子目录，加进 sys.path 后仍能按裸名 import
for _sub in ("formats", "convert", "gfx"):
    _p = os.path.join(BASE, _sub)
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

import fbx2pmx
import pmx_check
import pmx2vrm
import vrm2pmx
import preview as model_preview
from unitypackage_unpack import unpack as unpack_unitypackage

try:
    import ctypes
    from ctypes import wintypes
    HAS_CTYPES = True
except Exception:                                   # pragma: no cover
    HAS_CTYPES = False

# ----------------------------------------------------------------- palette --
BG = "#f4f6f9"
CARD = "#ffffff"
BORDER = "#dee3ea"
TXT = "#1f2430"
MUTED = "#6b7280"
FAINT = "#9aa3b0"
ACCENT = "#2f6fed"
ACCENT_HOVER = "#2559c8"
ACCENT_SOFT = "#e8f0ff"
OK = "#149c5c"
WARN = "#c47f12"
ERR = "#d93b3b"

FONT = "Microsoft YaHei UI"
F_TITLE = (FONT, 16, "bold")
F_SUB = (FONT, 9)
F_BODY = (FONT, 10)
F_BOLD = (FONT, 10, "bold")
F_SMALL = (FONT, 9)
F_DROP = (FONT, 13, "bold")
F_DROP2 = (FONT, 9)
F_ICON = (FONT, 26)
F_MONO = ("Consolas", 9)

SETTINGS_PATH = os.path.join(BASE, "config.json")
PREVIEW_BASE = 320

# ---- 界面缩放 --------------------------------------------------------------
# 所有像素尺寸都过 px()，字体的点值则由 tk scaling 统一放大。
# 高分屏（如 200% 缩放）下必须这样做，否则窗口按物理像素绘制、字号却按 96 DPI
# 计算，界面会小到看不清。
UI_SCALE = 1.0


def px(v):
    return int(round(v * UI_SCALE))


VRM_SPECS = ["1.0", "0.x"]
MODEL_EXTS = (".fbx", ".unitypackage", ".vrm", ".pmx")

# ----------------------------------------------------------------- i18n ----
# 界面所有文案集中在此；切换语言后通过 _rebuild() 重建即可整窗本地化。
LANG = {
    "zh_CN": {
        "app_title": "模型转换器 · FBX / VRM / PMX",
        "subtitle": "FBX / unitypackage / VRM / PMX 互转 · 纯 Python，不需要 Blender / FBX SDK / Unity",
        "zoom_label": "界面缩放",
        "lang_label": "语言",
        "drop_hint": "把模型文件拖到这里",
        "drop_formats": "支持 .fbx / .unitypackage / .vrm / .pmx　·　也可以点击本区域选择文件",
        "task": "任务",
        "task_auto": "自动（按扩展名）",
        "task_fbx2pmx": "FBX / unitypackage → PMX",
        "task_vrm2pmx": "VRM → PMX",
        "task_pmx2vrm": "PMX → VRM",
        "task_check": "PMX 仅校验 + 预览",
        "hint_auto": "按扩展名自动判断：.fbx/.unitypackage→PMX，.vrm→PMX，.pmx→VRM",
        "hint_fbx2pmx": "FBX / unitypackage 转成 MMD 的 PMX",
        "hint_vrm2pmx": "VRM 0.x / 1.0 转成 MMD 的 PMX（贴图会导出到同目录）",
        "hint_pmx2vrm": "PMX 转成 VRM（自动识别 humanoid 骨骼，缺的会补占位骨）",
        "hint_check": "只读取 PMX 做结构校验和预览，不输出文件",
        "scale": "缩放",
        "scale_auto": "自动",
        "scale_raw": "原始尺寸",
        "scale_custom": "自定义",
        "opt_flipz": "Z 轴翻转（FBX 右手系 → MMD）",
        "opt_bones": "预览叠加骨骼点",
        "opt_open": "完成后打开输出目录",
        "vrm_version": "VRM 版本",
        "opt_morphs": "导出表情（morph / blendShape）",
        "opt_twosided": "材质强制双面",
        "opt_edge": "VRM→PMX 时开启轮廓线",
        "opt_center": "FBX→PMX 自动居中并落地",
        "opt_fbxmorph": "FBX→PMX 导出形态键为表情",
        "opt_alpha": "FBX→PMX 去除贴图透明通道",
        "note_winding": "（仍有镂空可勾选双面；绕序默认自动判定）",
        "output": "输出",
        "opt_samedir": "与源文件同目录",
        "browse": "浏览",
        "outdir_placeholder": "（输出到源文件所在目录）",
        "start": "开始转换",
        "open_out": "打开输出目录",
        "clear": "清空日志",
        "status_wait": "等待文件…",
        "log_title": "转换日志",
        "preview_title": "模型预览（背视图实时预览 · 可拖动旋转 / 滚轮缩放）",
        "preview_stats": "拖入任意格式的模型即可实时背视图预览",
        "pv_front": "正视",
        "pv_left": "左视",
        "pv_back": "背视",
        "pv_top": "俯视",
        "pv_reset": "复位",
        "pv_spin": "自转",
        "pv_bone": "骨骼",
        "pv_wire": "线框",
        "pv_placeholder": "拖入模型后这里实时显示背视图预览\n（按住拖动可旋转，滚轮缩放）",
        "pv_stats": "{name}\n顶点 {v} · 三角面 {t}\n骨骼 {b} · 材质 {m}{size}",
        "pv_stats_file": "\n文件 {size}",
        "tab_general": "通用",
        "tab_fbx": "FBX 选项",
        "tab_vrm": "VRM 选项",
        "tab_output": "输出",
        "zoom_auto": "自动（跟随系统）",
        "msg_title": "提示",
        "msg_pick": "请先把 .fbx / .unitypackage / .vrm / .pmx 文件拖进窗口，或点击拖放区域选择文件。",
        "msg_unsupported": "不支持的文件",
        "msg_unsupported_body": "无法识别：\n{files}\n\n支持 .fbx / .unitypackage / .vrm / .pmx",
        "msg_mismatch": "所选文件与当前任务方向不匹配。",
        "msg_no_outdir": "还没有输出目录，先转换一次吧。",
        "msg_open_fail": "打开失败",
        "msg_drop_unavail": "提示：系统未启用拖放接口，请用“浏览”按钮选择文件。",
        "ready": "就绪。把 .fbx / .unitypackage 文件拖进上方区域即可。",
        "zoom_changed": "界面缩放：{label}（实际 {pct}%）",
        "log_start": "══ 开始处理 {n} 个文件 ══",
        "log_done": "══ 完成：成功生成 {n} 个 PMX ══",
        "log_none": "没有生成任何文件。",
        "status_process": "正在处理 {n} 个文件…",
        "status_done": "完成 · {n} 个文件",
        "status_none": "未生成文件",
        "dlg_title": "选择要转换的文件",
        "dlg_out": "选择输出目录",
    },
    "zh_TW": {
        "app_title": "模型轉換器 · FBX / VRM / PMX",
        "subtitle": "FBX / unitypackage / VRM / PMX 互轉 · 純 Python，不需要 Blender / FBX SDK / Unity",
        "zoom_label": "介面縮放",
        "lang_label": "語言",
        "drop_hint": "把模型檔案拖到這裡",
        "drop_formats": "支援 .fbx / .unitypackage / .vrm / .pmx　·　也可以點擊此區域選擇檔案",
        "task": "任務",
        "task_auto": "自動（依副檔名）",
        "task_fbx2pmx": "FBX / unitypackage → PMX",
        "task_vrm2pmx": "VRM → PMX",
        "task_pmx2vrm": "PMX → VRM",
        "task_check": "PMX 僅校驗 + 預覽",
        "hint_auto": "依副檔名自動判斷：.fbx/.unitypackage→PMX，.vrm→PMX，.pmx→VRM",
        "hint_fbx2pmx": "FBX / unitypackage 轉成 MMD 的 PMX",
        "hint_vrm2pmx": "VRM 0.x / 1.0 轉成 MMD 的 PMX（貼圖會匯出到同目錄）",
        "hint_pmx2vrm": "PMX 轉成 VRM（自動辨識 humanoid 骨骼，缺少的會補佔位骨）",
        "hint_check": "只讀取 PMX 做結構校驗與預覽，不輸出檔案",
        "scale": "縮放",
        "scale_auto": "自動",
        "scale_raw": "原始尺寸",
        "scale_custom": "自訂",
        "opt_flipz": "Z 軸翻轉（FBX 右手系 → MMD）",
        "opt_bones": "預覽疊加骨骼點",
        "opt_open": "完成後開啟輸出目錄",
        "vrm_version": "VRM 版本",
        "opt_morphs": "匯出表情（morph / blendShape）",
        "opt_twosided": "材質強制雙面",
        "opt_edge": "VRM→PMX 時開啟輪廓線",
        "opt_center": "FBX→PMX 自動置中並落地",
        "opt_fbxmorph": "FBX→PMX 匯出形態鍵為表情",
        "opt_alpha": "FBX→PMX 去除貼圖透明通道",
        "note_winding": "（仍有鏤空可勾選雙面；繞序預設自動判定）",
        "output": "輸出",
        "opt_samedir": "與來源檔案同目錄",
        "browse": "瀏覽",
        "outdir_placeholder": "（輸出到來源檔案所在目錄）",
        "start": "開始轉換",
        "open_out": "開啟輸出目錄",
        "clear": "清空日誌",
        "status_wait": "等待檔案…",
        "log_title": "轉換日誌",
        "preview_title": "模型預覽（背視圖即時預覽 · 可拖曳旋轉 / 滾輪縮放）",
        "preview_stats": "拖入任意格式的模型即可即時背視圖預覽",
        "pv_front": "正視",
        "pv_left": "左視",
        "pv_back": "背視",
        "pv_top": "俯視",
        "pv_reset": "復位",
        "pv_spin": "自轉",
        "pv_bone": "骨骼",
        "pv_wire": "線框",
        "pv_placeholder": "拖入模型後這裡即時顯示背視圖預覽\n（按住拖曳可旋轉，滾輪縮放）",
        "pv_stats": "{name}\n頂點 {v} · 三角面 {t}\n骨骼 {b} · 材質 {m}{size}",
        "pv_stats_file": "\n檔案 {size}",
        "tab_general": "通用",
        "tab_fbx": "FBX 選項",
        "tab_vrm": "VRM 選項",
        "tab_output": "輸出",
        "zoom_auto": "自動（跟隨系統）",
        "msg_title": "提示",
        "msg_pick": "請先把 .fbx / .unitypackage / .vrm / .pmx 檔案拖進視窗，或點擊拖放區域選擇檔案。",
        "msg_unsupported": "不支援的檔案",
        "msg_unsupported_body": "無法辨識：\n{files}\n\n支援 .fbx / .unitypackage / .vrm / .pmx",
        "msg_mismatch": "所選檔案與目前任務方向不符。",
        "msg_no_outdir": "還沒有輸出目錄，先轉換一次吧。",
        "msg_open_fail": "開啟失敗",
        "msg_drop_unavail": "提示：系統未啟用拖放介面，請用「瀏覽」按鈕選擇檔案。",
        "ready": "就緒。把 .fbx / .unitypackage 檔案拖進上方區域即可。",
        "zoom_changed": "介面縮放：{label}（實際 {pct}%）",
        "log_start": "══ 開始處理 {n} 個檔案 ══",
        "log_done": "══ 完成：成功產生 {n} 個 PMX ══",
        "log_none": "沒有產生任何檔案。",
        "status_process": "正在處理 {n} 個檔案…",
        "status_done": "完成 · {n} 個檔案",
        "status_none": "未產生檔案",
        "dlg_title": "選擇要轉換的檔案",
        "dlg_out": "選擇輸出目錄",
    },
    "en": {
        "app_title": "Model Converter · FBX / VRM / PMX",
        "subtitle": "FBX / unitypackage / VRM / PMX conversion · Pure Python, no Blender / FBX SDK / Unity",
        "zoom_label": "UI scale",
        "lang_label": "Language",
        "drop_hint": "Drop model files here",
        "drop_formats": "Supports .fbx / .unitypackage / .vrm / .pmx　·　or click this area to pick files",
        "task": "Task",
        "task_auto": "Auto (by extension)",
        "task_fbx2pmx": "FBX / unitypackage → PMX",
        "task_vrm2pmx": "VRM → PMX",
        "task_pmx2vrm": "PMX → VRM",
        "task_check": "PMX check + preview only",
        "hint_auto": "Auto by extension: .fbx/.unitypackage→PMX, .vrm→PMX, .pmx→VRM",
        "hint_fbx2pmx": "Convert FBX / unitypackage into MMD PMX",
        "hint_vrm2pmx": "Convert VRM 0.x / 1.0 into MMD PMX (textures exported alongside)",
        "hint_pmx2vrm": "Convert PMX into VRM (auto-detect humanoid bones, fill missing with placeholders)",
        "hint_check": "Only read PMX for structure check and preview, no output file",
        "scale": "Scale",
        "scale_auto": "Auto",
        "scale_raw": "Original size",
        "scale_custom": "Custom",
        "opt_flipz": "Flip Z axis (FBX right-handed → MMD)",
        "opt_bones": "Overlay bones in preview",
        "opt_open": "Open output folder when done",
        "vrm_version": "VRM version",
        "opt_morphs": "Export morphs (morph / blendShape)",
        "opt_twosided": "Force double-sided materials",
        "opt_edge": "Enable outline (VRM→PMX)",
        "opt_center": "FBX→PMX auto-center and drop to ground",
        "opt_fbxmorph": "FBX→PMX export shape keys as morphs",
        "opt_alpha": "FBX→PMX strip texture alpha channel",
        "note_winding": "(If still holed, check double-sided; winding auto-detected)",
        "output": "Output",
        "opt_samedir": "Same folder as source",
        "browse": "Browse",
        "outdir_placeholder": "(Output to source file folder)",
        "start": "Start conversion",
        "open_out": "Open output folder",
        "clear": "Clear log",
        "status_wait": "Waiting for files…",
        "log_title": "Conversion log",
        "preview_title": "Model preview (live back view · drag to rotate / wheel to zoom)",
        "preview_stats": "Drop any model format to see a live back view preview",
        "pv_front": "Front",
        "pv_left": "Left",
        "pv_back": "Back",
        "pv_top": "Top",
        "pv_reset": "Reset",
        "pv_spin": "Spin",
        "pv_bone": "Bones",
        "pv_wire": "Wire",
        "pv_placeholder": "Drop a model here to see the live back view\n(drag to rotate · wheel to zoom)",
        "pv_stats": "{name}\nVerts {v} · Tris {t}\nBones {b} · Materials {m}{size}",
        "pv_stats_file": "\nFile {size}",
        "tab_general": "General",
        "tab_fbx": "FBX",
        "tab_vrm": "VRM",
        "tab_output": "Output",
        "zoom_auto": "Auto (follow system)",
        "msg_title": "Note",
        "msg_pick": "Please drop .fbx / .unitypackage / .vrm / .pmx files into the window, or click the drop area to pick files.",
        "msg_unsupported": "Unsupported file",
        "msg_unsupported_body": "Unrecognized:\n{files}\n\nSupports .fbx / .unitypackage / .vrm / .pmx",
        "msg_mismatch": "Selected files do not match the current task direction.",
        "msg_no_outdir": "No output folder yet — convert something first.",
        "msg_open_fail": "Failed to open",
        "msg_drop_unavail": "Note: drag-and-drop is unavailable on this system; use the \"Browse\" button.",
        "ready": "Ready. Drop .fbx / .unitypackage files into the area above.",
        "zoom_changed": "UI scale: {label} (actual {pct}%)",
        "log_start": "══ Processing {n} file(s) ══",
        "log_done": "══ Done: generated {n} PMX file(s) ══",
        "log_none": "No files were generated.",
        "status_process": "Processing {n} file(s)…",
        "status_done": "Done · {n} file(s)",
        "status_none": "No files generated",
        "dlg_title": "Select files to convert",
        "dlg_out": "Select output folder",
    },
    "ja": {
        "app_title": "モデル変換 · FBX / VRM / PMX",
        "subtitle": "FBX / unitypackage / VRM / PMX 相互変換 · 純 Python、Blender / FBX SDK / Unity 不要",
        "zoom_label": "表示倍率",
        "lang_label": "言語",
        "drop_hint": "ここにモデルファイルをドロップ",
        "drop_formats": "対応 .fbx / .unitypackage / .vrm / .pmx　·　またはここをクリックして選択",
        "task": "タスク",
        "task_auto": "自動（拡張子で判定）",
        "task_fbx2pmx": "FBX / unitypackage → PMX",
        "task_vrm2pmx": "VRM → PMX",
        "task_pmx2vrm": "PMX → VRM",
        "task_check": "PMX 検証＋プレビューのみ",
        "hint_auto": "拡張子で自動判定：.fbx/.unitypackage→PMX、.vrm→PMX、.pmx→VRM",
        "hint_fbx2pmx": "FBX / unitypackage を MMD の PMX に変換",
        "hint_vrm2pmx": "VRM 0.x / 1.0 を MMD の PMX に変換（テクスチャは同フォルダへ）",
        "hint_pmx2vrm": "PMX を VRM に変換（humanoid ボーン自動判定、欠損はダミー骨で補完）",
        "hint_check": "PMX を読むだけで構造検証とプレビュー、出力はしません",
        "scale": "倍率",
        "scale_auto": "自動",
        "scale_raw": "元のサイズ",
        "scale_custom": "カスタム",
        "opt_flipz": "Z 軸反転（FBX 右手系 → MMD）",
        "opt_bones": "プレビューにボーン点を重ねる",
        "opt_open": "完了後に出力フォルダを開く",
        "vrm_version": "VRM バージョン",
        "opt_morphs": "モーフを書き出し（morph / blendShape）",
        "opt_twosided": "マテリアルを両面化（強制）",
        "opt_edge": "輪郭線を有効化（VRM→PMX）",
        "opt_center": "FBX→PMX 自動中央配置＆接地",
        "opt_fbxmorph": "FBX→PMX シェイプキーをモーフに",
        "opt_alpha": "FBX→PMX テクスチャのアルファを除去",
        "note_winding": "（それでも抜けがある場合は両面化をチェック。巻き順は自動判定）",
        "output": "出力",
        "opt_samedir": "元ファイルと同じフォルダ",
        "browse": "参照",
        "outdir_placeholder": "（元ファイルのあるフォルダへ出力）",
        "start": "変換開始",
        "open_out": "出力フォルダを開く",
        "clear": "ログを消去",
        "status_wait": "ファイル待ち…",
        "log_title": "変換ログ",
        "preview_title": "モデルプレビュー（背面部 リアルタイム・ドラッグで回転／ホイールで拡大）",
        "preview_stats": "任意の形式のモデルをドロップすると背面部が表示されます",
        "pv_front": "正面",
        "pv_left": "左面",
        "pv_back": "背面",
        "pv_top": "上面",
        "pv_reset": "リセット",
        "pv_spin": "回転",
        "pv_bone": "ボーン",
        "pv_wire": "ワイヤー",
        "pv_placeholder": "モデルをドロップすると背面部がここに表示されます\n（ドラッグで回転／ホイールで拡大）",
        "pv_stats": "{name}\n頂点 {v} · 三角面 {t}\nボーン {b} · マテリアル {m}{size}",
        "pv_stats_file": "\nファイル {size}",
        "tab_general": "共通",
        "tab_fbx": "FBX",
        "tab_vrm": "VRM",
        "tab_output": "出力",
        "zoom_auto": "自動（システムに合わせる）",
        "msg_title": "注意",
        "msg_pick": ".fbx / .unitypackage / .vrm / .pmx ファイルをウィンドウへドロップするか、ドロップ領域をクリックして選択してください。",
        "msg_unsupported": "非対応のファイル",
        "msg_unsupported_body": "認識できません：\n{files}\n\n対応：.fbx / .unitypackage / .vrm / .pmx",
        "msg_mismatch": "選択したファイルは現在のタスク方向と一致しません。",
        "msg_no_outdir": "まだ出力フォルダがありません。一度変換してください。",
        "msg_open_fail": "開けませんでした",
        "msg_drop_unavail": "注意：このシステムではドラッグ＆ドロップが使えません。「参照」ボタンを使ってください。",
        "ready": "準備完了。.fbx / .unitypackage を上の領域へドロップしてください。",
        "zoom_changed": "表示倍率：{label}（実際 {pct}%）",
        "log_start": "══ {n} 個のファイルを処理 ══",
        "log_done": "══ 完了：PMX を {n} 個生成 ══",
        "log_none": "ファイルは生成されませんでした。",
        "status_process": "{n} 個のファイルを処理中…",
        "status_done": "完了 · {n} 個",
        "status_none": "ファイル未生成",
        "dlg_title": "変換するファイルを選択",
        "dlg_out": "出力フォルダを選択",
    },
}
LANG_NAMES = {"zh_CN": "简体中文", "zh_TW": "繁體中文", "en": "English", "ja": "日本語"}
APP_LANG = "zh_CN"          # 当前界面语言，由设置文件（lang 字段）决定


def t(key, **kw):
    table = LANG.get(APP_LANG, LANG["zh_CN"])
    s = table.get(key)
    if s is None:
        s = LANG["zh_CN"].get(key, key)
    if kw:
        try:
            return s.format(**kw)
        except Exception:
            return s
    return s


# 任务 / 缩放 等下拉选项随语言变化，集中刷新
TASK_CHOICES = []
TASK_BY_LABEL = {}
TASK_LABELS = []
ZOOM_CHOICES = []
ZOOM_BY_LABEL = {}


def refresh_choices():
    global TASK_CHOICES, TASK_BY_LABEL, TASK_LABELS, ZOOM_CHOICES, ZOOM_BY_LABEL
    TASK_CHOICES = [(t("task_auto"), "auto"),
                    (t("task_fbx2pmx"), "fbx2pmx"),
                    (t("task_vrm2pmx"), "vrm2pmx"),
                    (t("task_pmx2vrm"), "pmx2vrm"),
                    (t("task_check"), "check")]
    TASK_BY_LABEL = dict(TASK_CHOICES)
    TASK_LABELS = [lbl for lbl, _ in TASK_CHOICES]
    ZOOM_CHOICES = [(t("zoom_auto"), None), ("100%", 1.0), ("125%", 1.25),
                    ("150%", 1.5), ("175%", 1.75), ("200%", 2.0), ("250%", 2.5)]
    ZOOM_BY_LABEL = dict(ZOOM_CHOICES)


refresh_choices()

WM_DROPFILES = 0x0233
GWLP_WNDPROC = -4

# 拖放状态跨界面重建保留：callback 只构造一次；oldmap 记录「每个」被替换窗口的
# 原始 WndProc。转发消息时必须用该窗口自己的 old，不能用共享变量——否则多个窗口
# 串用同一旧过程，深入子窗口时极易崩溃（这正是早期只挂顶层、不敢挂子窗口的原因）。
_dnd_state = None            # {"callback": WNDPROC, "oldmap": {hwnd: old}}
_dnd_on_files = None         # 文件就绪回调

# ------------------------------------------------------------------- drag ---
def _make_drop_proc():
    """构造 WNDPROC：捕获 WM_DROPFILES 交给回调，其余消息转发给各窗口原始过程。"""
    user32 = ctypes.windll.user32
    shell32 = ctypes.windll.shell32
    ptr = ctypes.sizeof(ctypes.c_void_p)
    LRESULT = ctypes.c_longlong if ptr == 8 else ctypes.c_long
    WPARAM = ctypes.c_ulonglong if ptr == 8 else ctypes.c_uint
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                                 WPARAM, LRESULT)

    def proc(h, msg, wp, lp):
        # 直接读模块全局 _dnd_state（不要捕获局部变量：_make_drop_proc 在
        # _dnd_state 赋值之前被调用，捕获会锁死成 None）
        state = _dnd_state
        if msg == WM_DROPFILES:
            files = []
            try:
                n = shell32.DragQueryFileW(wintypes.HANDLE(wp), 0xFFFFFFFF,
                                           None, 0)
                for i in range(n):
                    buf = ctypes.create_unicode_buffer(2048)
                    if shell32.DragQueryFileW(wintypes.HANDLE(wp), i, buf, 2048):
                        if buf.value:
                            files.append(buf.value)
                shell32.DragFinish(wintypes.HANDLE(wp))
            except Exception:
                traceback.print_exc()
            if files and _dnd_on_files is not None:
                try:
                    _dnd_on_files(files)
                except Exception:
                    traceback.print_exc()
            return 0
        hk = int(getattr(h, "value", h))
        old = state["oldmap"].get(hk)
        if old:
            return user32.CallWindowProcW(old, h, msg, wp, lp)
        return 0

    return WNDPROC(proc)


def enable_drop(hwnd, on_files, register_children=True):
    """让一个（含其全部子控件）Tk 窗口接收 Explorer 拖入的文件。

    根因：tkinter 控件在 Windows 上各是一个原生子窗口，鼠标拖到哪个子控件，
    Explorer 就把 WM_DROPFILES 发给那个子控件。只给顶层 root 注册拖放时，窗口
    内容区（拖放区 Canvas、日志、预览等）都是子控件，落到它们上面 root 收不到
    消息，表现为「拖上去没反应」。这里给 root + 所有子孙窗口都注册，并各自保存
    原始 WndProc 用于转发，于是整个窗口任意位置都能拖入。
    """
    if not HAS_CTYPES or os.name != "nt":
        return False
    global _dnd_state, _dnd_on_files
    _dnd_on_files = on_files
    try:
        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        ptr = ctypes.sizeof(ctypes.c_void_p)
        LRESULT = ctypes.c_longlong if ptr == 8 else ctypes.c_long
        WPARAM = ctypes.c_ulonglong if ptr == 8 else ctypes.c_uint
        setter = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
        user32.SetWindowLongPtrW.restype = ctypes.c_void_p
        user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int,
                                             ctypes.c_void_p]
        try:
            user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int,
                                              ctypes.c_void_p]
        except Exception:
            pass
        user32.CallWindowProcW.restype = LRESULT
        user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND,
                                           wintypes.UINT, WPARAM, LRESULT]
        shell32.DragQueryFileW.restype = wintypes.UINT
        shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT,
                                           ctypes.c_wchar_p, wintypes.UINT]
        shell32.DragFinish.argtypes = [wintypes.HANDLE]
        shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]

        if _dnd_state is None:
            _dnd_state = {"callback": _make_drop_proc(), "oldmap": {}}
        callback = _dnd_state["callback"]

        def hook_one(h):
            h = wintypes.HWND(h)
            hk = h.value
            if hk in _dnd_state["oldmap"]:
                return
            old = setter(h, GWLP_WNDPROC, ctypes.cast(callback, ctypes.c_void_p))
            if old:
                _dnd_state["oldmap"][hk] = old
                shell32.DragAcceptFiles(h, True)

        hook_one(hwnd)
        if register_children:
            WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                            ctypes.c_void_p)

            def enum_proc(h, _lp):
                hook_one(h)
                return True

            user32.EnumChildWindows.argtypes = [wintypes.HWND, WNDENUMPROC,
                                                ctypes.c_void_p]
            user32.EnumChildWindows.restype = wintypes.BOOL
            user32.EnumChildWindows(wintypes.HWND(hwnd),
                                    WNDENUMPROC(enum_proc), None)
        return True
    except Exception:
        traceback.print_exc()
        return False


# ------------------------------------------------------------------ helpers --
def stdout_redirect(q):
    """Route print() from the converters into the GUI log, line by line."""

    class W:
        def __init__(self):
            self.buf = ""

        def write(self, s):
            self.buf += s
            while "\n" in self.buf:
                line, self.buf = self.buf.split("\n", 1)
                q.put(("log", line))

        def flush(self):
            if self.buf.strip():
                q.put(("log", self.buf))
            self.buf = ""

    return W()


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.2f %s" % (n, unit))
        n /= 1024.0


def classify(path):
    if os.path.isdir(path):
        return "dir"
    ext = os.path.splitext(path)[1].lower()
    if ext == ".fbx":
        return "fbx"
    if ext == ".unitypackage":
        return "unitypackage"
    if ext == ".vrm":
        return "vrm"
    if ext == ".pmx":
        return "pmx"
    return "unknown"


AUTO_TASK = {"fbx": "fbx2pmx", "unitypackage": "fbx2pmx",
             "vrm": "vrm2pmx", "pmx": "pmx2vrm"}


def find_fbx(root):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for f in filenames:
            if f.lower().endswith(".fbx"):
                p = os.path.join(dirpath, f)
                if os.path.getsize(p) > 1024:
                    out.append(p)
    return out


def find_models(root):
    """拖入文件夹时，找出里面所有可处理的模型文件。"""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for f in filenames:
            if f.lower().endswith(MODEL_EXTS):
                p = os.path.join(dirpath, f)
                if os.path.getsize(p) > 1024:
                    out.append(p)
    return out


# -------------------------------------------------------------- dpi / zoom --
def declare_dpi_aware():
    """让进程声明 DPI 感知，Windows 才不会对窗口做位图拉伸（发虚）。"""
    if os.name != "nt" or not HAS_CTYPES:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)      # system aware
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def system_dpi():
    """当前系统的实际 DPI（96 = 100%，192 = 200%）。"""
    if os.name != "nt" or not HAS_CTYPES:
        return 96
    user32 = ctypes.windll.user32
    try:
        dpi = int(user32.GetDpiForSystem())                 # Win10 1607+
        if dpi > 0:
            return dpi
    except Exception:
        pass
    try:
        hdc = user32.GetDC(0)
        try:
            return int(ctypes.windll.gdi32.GetDeviceCaps(hdc, 88))
        finally:
            user32.ReleaseDC(0, hdc)
    except Exception:
        return 96


def norm_zoom(v):
    """把设置文件里的值规整成倍率；None 表示跟随系统。"""
    if v is None or v == "" or v == "auto":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if 0.5 <= f <= 4.0 else None


def zoom_label(factor):
    if factor is None:
        return "自动（跟随系统）"
    for lbl, f in ZOOM_CHOICES:
        if f is not None and abs(f - factor) < 1e-6:
            return lbl
    return "%.0f%%" % (factor * 100)


def apply_scaling(root, factor):
    """设定字体缩放。factor=None 跟随系统。返回实际生效的倍率。"""
    global UI_SCALE
    if factor is None:
        factor = max(1.0, min(system_dpi() / 96.0, 3.0))
    UI_SCALE = factor
    try:
        root.tk.call("tk", "scaling", 96.0 * factor / 72.0)
    except Exception:
        pass
    return factor


def load_zoom_pref():
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            return norm_zoom(json.load(f).get("ui_zoom"))
    except Exception:
        return None


def validate_pmx(path):
    """Re-open the written PMX and return (summary_dict, problems_list)."""
    m = pmx_check.read_pmx(path)
    nv, nb = len(m["vertices"]), len(m["bones"])
    bad_v = sum(1 for f in m["faces"] if f >= nv)
    bad_b = 0
    for v in m["vertices"]:
        for b in v[4]:
            if b >= nb or b < 0:
                bad_b += 1
    wsum_bad = 0
    for v in m["vertices"]:
        wt, ws = v[3], v[5]
        if wt == 0:
            s = 1.0
        elif wt == 1:
            s = ws[0] + (1.0 - ws[0])
        else:
            s = sum(ws)
        if abs(s - 1.0) > 0.02:
            wsum_bad += 1
    cover = sum(mm["faces"] for mm in m["materials"])
    problems = []
    if m["consumed"] != m["filesize"]:
        problems.append("字节数不匹配 %d/%d" % (m["consumed"], m["filesize"]))
    if bad_v:
        problems.append("%d 个面引用了不存在的顶点" % bad_v)
    if bad_b:
        problems.append("%d 个权重的骨骼索引越界" % bad_b)
    if wsum_bad:
        problems.append("%d 个顶点权重和偏离 1.0" % wsum_bad)
    if cover != len(m["faces"]):
        problems.append("材质面覆盖不全 %d/%d" % (cover, len(m["faces"])))
    return m, problems


# ------------------------------------------------------------------- app ----
class BigCheck(tk.Frame):
    """比原生 Checkbutton 更大的复选项：放大的勾选框 + 更大的点击区域。"""

    def __init__(self, master, text="", variable=None, command=None,
                 font=None, fg=TXT, bg=CARD, padx=0, **kw):
        super().__init__(master, bg=bg, **kw)
        self.var = variable if variable is not None else tk.BooleanVar()
        self.command = command
        self._bg = bg
        self._size = px(22)
        self._box = tk.Canvas(self, width=self._size, height=self._size,
                              bg=bg, highlightthickness=0, bd=0, cursor="hand2")
        self._box.pack(side="left", padx=(padx, px(8)))
        self._lbl = tk.Label(self, text=text, bg=bg, fg=fg,
                             font=font or F_BODY, cursor="hand2")
        self._lbl.pack(side="left")
        self._box.bind("<Button-1>", self._toggle)
        self._lbl.bind("<Button-1>", self._toggle)
        self.bind("<Button-1>", self._toggle)
        try:
            self.var.trace_add("write", lambda *_a: self._draw())
        except Exception:
            self.var.trace("w", lambda *_a: self._draw())
        self._draw()

    def _toggle(self, event=None):
        self.var.set(not self.var.get())
        self._draw()
        if callable(self.command):
            self.command()

    def _draw(self):
        c = self._box
        c.delete("all")
        s = self._size
        on = bool(self.var.get())
        c.create_rectangle(2, 2, s - 2, s - 2,
                           outline=(ACCENT if on else "#9aa3b0"),
                           width=max(1, px(2)),
                           fill=(ACCENT if on else self._bg))
        if on:
            c.create_line(5, s * 0.52, s * 0.42, s - 6, s - 5, 5,
                          fill="#ffffff", width=max(2, px(3)),
                          capstyle="round", joinstyle="round")

    def configure(self, **kw):
        # 兼容 .config(text=...) 的写法
        if "text" in kw:
            self._lbl.configure(text=kw["text"])
        rest = {k: v for k, v in kw.items() if k != "text"}
        if rest:
            super().configure(**rest)


class ConverterApp:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.busy = False
        self._pending_files = []
        self._drop_event = threading.Event()
        self._pending_drop = None
        self.last_out_dir = None
        self.last_pmx = None
        self.last_model = None
        self.last_mesh = None
        self._img = None
        self._drag_hot = False

        self.var_scale_auto = tk.BooleanVar(value=True)
        self.var_scale_raw = tk.BooleanVar(value=False)
        self.var_scale_custom = tk.BooleanVar(value=False)
        self.var_scale_value = tk.StringVar(value="1.0")
        self.var_flipz = tk.BooleanVar(value=True)
        self.var_bones = tk.BooleanVar(value=False)
        self.var_open_after = tk.BooleanVar(value=False)
        self.var_same_dir = tk.BooleanVar(value=True)
        self.var_outdir = tk.StringVar(value="")
        self.var_zoom = tk.StringVar(value="自动（跟随系统）")
        self.var_task = tk.StringVar(value=TASK_LABELS[0])
        self.var_vrm_spec = tk.StringVar(value="1.0")
        self.var_morphs = tk.BooleanVar(value=True)
        self.var_center = tk.BooleanVar(value=True)
        self.var_fbx_morphs = tk.BooleanVar(value=True)
        self.var_remove_alpha = tk.BooleanVar(value=False)
        self.var_two_sided = tk.BooleanVar(value=False)
        self.var_edge = tk.BooleanVar(value=False)
        self._task_code = "auto"             # 任务方向内部代号（与语言无关）
        self.var_lang = tk.StringVar(value="zh_CN")
        self.zoom = None                      # None = 跟随系统 DPI
        self.dnd_ok = None
        self._split = 0.60                    # 左右分栏比例（预览占右侧 40%）

        self._load_settings()
        self._build()
        self.root.after(60, self._pump)
        self.root.after(80, self._poll_drop)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------- layout --
    def _build(self):
        r = self.root
        r.title(t("app_title"))
        r.configure(bg=BG)
        # 右侧要放整条预览栏，窗口下限宽度得留够
        w = max(px(920), min(px(1240), r.winfo_screenwidth() - px(60)))
        h = max(px(600), min(px(800), r.winfo_screenheight() - px(100)))
        r.geometry("%dx%d" % (w, h))
        r.minsize(min(px(860), w), min(px(560), h))

        outer = tk.Frame(r, bg=BG)
        outer.pack(fill="both", expand=True, padx=px(16), pady=px(14))

        # ---- header
        head = tk.Frame(outer, bg=BG)
        head.pack(fill="x")
        toprow = tk.Frame(head, bg=BG)
        toprow.pack(fill="x")
        tk.Label(toprow, text=t("app_title").split(" · ")[0], font=F_TITLE,
                 bg=BG, fg=TXT).pack(side="left")
        # 右上角：语言 + 界面缩放
        rbox = tk.Frame(toprow, bg=BG)
        rbox.pack(side="right")
        self.var_lang = tk.StringVar(value=LANG_NAMES.get(APP_LANG, "简体中文"))
        tk.Label(rbox, text=t("lang_label"), font=F_SMALL, bg=BG,
                 fg=MUTED).pack(side="left", padx=(0, px(6)))
        self.om_lang = tk.OptionMenu(rbox, self.var_lang,
                                     *list(LANG_NAMES.values()),
                                     command=self._on_lang)
        self._style_om(self.om_lang, width=11)
        self.om_lang.pack(side="left", padx=(0, px(10)))
        tk.Label(rbox, text=t("zoom_label"), font=F_SMALL, bg=BG,
                 fg=MUTED).pack(side="left", padx=(0, px(6)))
        self.om_zoom = tk.OptionMenu(rbox, self.var_zoom,
                                     *[lbl for lbl, _ in ZOOM_CHOICES],
                                     command=self._on_zoom)
        self._style_om(self.om_zoom, width=14)
        self.om_zoom.pack(side="left")
        tk.Label(head, text=t("subtitle"),
                 font=F_SUB, bg=BG, fg=MUTED).pack(anchor="w", pady=(px(2), 0))

        # ---- 主体：左右分栏（Blender 那种可以拖动分割条的排版）
        # 左栏 = 拖放 + 选项 + 按钮 + 日志；右栏 = 整条高度的模型预览。
        # 分割条位置会记进 config.json，下次打开还在原来的地方。
        self.panes = tk.PanedWindow(outer, orient="horizontal", bg="#d3dae4",
                                    bd=0, sashwidth=px(10), sashrelief="flat",
                                    sashpad=0, handlesize=px(6),
                                    opaqueresize=True)
        self.panes.pack(fill="both", expand=True, pady=(px(12), 0))
        left = tk.Frame(self.panes, bg=BG)
        self.panes.add(left, minsize=px(430), stretch="always", width=px(620))
        right = tk.Frame(self.panes, bg=BG)
        self.panes.add(right, minsize=px(300), stretch="always", width=px(420))
        self._restore_split()

        # ---- drop zone
        self.dz = tk.Canvas(left, height=px(126), bg=CARD, highlightthickness=1,
                            highlightbackground=BORDER, cursor="hand2")
        self.dz.pack(fill="x", pady=(px(12), 0))
        self.dz.bind("<Configure>", lambda e: self._draw_dropzone())
        self.dz.bind("<Enter>", lambda e: self._hover(True))
        self.dz.bind("<Leave>", lambda e: self._hover(False))
        self.dz.bind("<Button-1>", lambda e: self.browse())

        # ---- options（Notebook 分页：为将来加功能预留扩展空间）
        nb = ttk.Notebook(left)
        nb.pack(fill="x", pady=(px(10), 0))
        self._style_notebook(nb)

        # 通用
        self.var_task.set(TASK_BY_LABEL.get(self._task_code, TASK_LABELS[0]))
        tab_g = self._tab(nb, t("tab_general"))
        row0 = tk.Frame(tab_g, bg=CARD)
        row0.pack(fill="x", padx=px(14), pady=(px(12), 0))
        tk.Label(row0, text=t("task"), font=F_BOLD, bg=CARD, fg=TXT,
                 width=8, anchor="w").pack(side="left")
        self.om_task = tk.OptionMenu(row0, self.var_task, *TASK_LABELS,
                                     command=self._on_task)
        self._style_om(self.om_task, width=26)
        self.om_task.pack(side="left")
        # 分栏后左栏会变窄，提示文字单独占一行并跟着栏宽自动折行
        self.lbl_hint = tk.Label(tab_g, text="", font=F_SMALL, bg=CARD,
                                 fg=MUTED, justify="left", anchor="w")
        self.lbl_hint.pack(fill="x", padx=px(14), pady=(px(6), 0))
        tab_g.bind("<Configure>",
                   lambda e: self.lbl_hint.configure(
                       wraplength=max(px(150), e.width - px(28))))

        row1 = tk.Frame(tab_g, bg=CARD)
        row1.pack(fill="x", padx=px(14), pady=(px(8), 0))
        tk.Label(row1, text=t("scale"), font=F_BOLD, bg=CARD, fg=TXT,
                 width=8, anchor="w").pack(side="left")
        for text, var in ((t("scale_auto"), self.var_scale_auto),
                          (t("scale_raw"), self.var_scale_raw),
                          (t("scale_custom"), self.var_scale_custom)):
            rb = tk.Radiobutton(row1, text=text, variable=var, value=True,
                                bg=CARD, fg=TXT, font=F_BODY,
                                activebackground=CARD, activeforeground=TXT,
                                selectcolor=CARD, highlightthickness=0, bd=0,
                                command=self._pick_scale)
            rb.pack(side="left", padx=(0, px(14)))
        self.ent_scale = tk.Entry(row1, textvariable=self.var_scale_value,
                                  width=8, font=F_BODY, bg=CARD, fg=TXT,
                                  relief="solid", bd=1, justify="center",
                                  disabledbackground="#f0f2f6",
                                  highlightthickness=0, insertbackground=TXT)
        self.ent_scale.pack(side="left", padx=(px(8), 0))

        self._bigcheck(tab_g, t("opt_flipz"), self.var_flipz)
        self._bigcheck(tab_g, t("opt_bones"), self.var_bones,
                       command=self._rerender)
        self._bigcheck(tab_g, t("opt_open"), self.var_open_after)
        self._bigcheck(tab_g, t("opt_twosided"), self.var_two_sided)

        # FBX 选项
        tab_f = self._tab(nb, t("tab_fbx"))
        self._bigcheck(tab_f, t("opt_center"), self.var_center)
        self._bigcheck(tab_f, t("opt_fbxmorph"), self.var_fbx_morphs)
        self._bigcheck(tab_f, t("opt_alpha"), self.var_remove_alpha)
        tk.Label(tab_f, text=t("note_winding"), font=F_SUB, bg=CARD,
                 fg=MUTED).pack(anchor="w", padx=px(14),
                                pady=(px(2), px(12)))

        # VRM 选项
        tab_v = self._tab(nb, t("tab_vrm"))
        rowv = tk.Frame(tab_v, bg=CARD)
        rowv.pack(anchor="w", padx=px(14), pady=(px(12), 0))
        tk.Label(rowv, text=t("vrm_version"), font=F_BODY, bg=CARD,
                 fg=TXT).pack(side="left")
        self.om_spec = tk.OptionMenu(rowv, self.var_vrm_spec, *VRM_SPECS)
        self._style_om(self.om_spec, width=6)
        self.om_spec.pack(side="left", padx=(px(8), 0))
        self._bigcheck(tab_v, t("opt_morphs"), self.var_morphs)
        self._bigcheck(tab_v, t("opt_edge"), self.var_edge)

        # 输出
        tab_o = self._tab(nb, t("tab_output"))
        self._bigcheck(tab_o, t("opt_samedir"), self.var_same_dir,
                       command=self._toggle_outdir)
        rowo = tk.Frame(tab_o, bg=CARD)
        rowo.pack(fill="x", padx=px(14), pady=(px(8), px(12)))
        self.ent_out = tk.Entry(rowo, textvariable=self.var_outdir, font=F_BODY,
                                bg=CARD, fg=TXT, relief="solid", bd=1,
                                disabledbackground="#f0f2f6",
                                highlightthickness=0, insertbackground=TXT)
        self.ent_out.pack(side="left", fill="x", expand=True,
                          padx=(0, px(6)))
        self.btn_browse_out = self._btn(rowo, t("browse"), self.pick_outdir,
                                        kind="ghost", padx=px(12), pady=px(4))
        self.btn_browse_out.pack(side="left")

        # ---- actions
        act = tk.Frame(left, bg=BG)
        act.pack(fill="x", pady=(px(12), 0))
        self.btn_go = self._btn(act, t("start"), self.start_from_ui,
                                kind="primary")
        self.btn_go.pack(side="left")
        self.btn_open = self._btn(act, t("open_out"), self.open_outdir,
                                  kind="ghost")
        self.btn_open.pack(side="left", padx=(px(8), 0))
        self.btn_clear = self._btn(act, t("clear"), self.clear_log, kind="ghost")
        self.btn_clear.pack(side="left", padx=(px(8), 0))
        self.lbl_status = tk.Label(act, text=t("status_wait"), font=F_SMALL,
                                   bg=BG, fg=MUTED)
        self.lbl_status.pack(side="right")

        # ---- 左栏底部：日志（吃掉剩余高度）
        logbox = self._card(left, t("log_title"))
        logbox.pack(fill="both", expand=True, pady=(px(10), 0))
        self.txt = tk.Text(logbox, font=F_MONO, bg=CARD, fg=TXT, relief="flat",
                           wrap="none", height=14, highlightthickness=0,
                           padx=px(10), pady=px(8), insertbackground=TXT)
        sb = tk.Scrollbar(logbox, command=self.txt.yview, relief="flat",
                          bd=0, width=px(14))
        self.txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.txt.pack(fill="both", expand=True)
        self.txt.tag_configure("info", foreground=MUTED)
        self.txt.tag_configure("ok", foreground=OK)
        self.txt.tag_configure("err", foreground=ERR)
        self.txt.tag_configure("warn", foreground=WARN)
        self.txt.tag_configure("head", foreground=ACCENT,
                               font=(FONT, 10, "bold"))
        self.txt.tag_configure("mono", foreground="#3a4250")
        self.txt.configure(state="disabled")

        # ---- 右栏：模型预览（整列高度，跟 Blender 的 3D 视图区一个位置）
        pvcard = self._card(right, t("preview_title"))
        pvcard.pack(fill="both", expand=True)
        pvbox = tk.Frame(pvcard, bg=CARD,
                         highlightthickness=1, highlightbackground=BORDER)
        pvbox.pack(fill="both", expand=True, padx=px(8), pady=(px(2), px(6)))
        self.pv = model_preview.Preview3D(
            pvbox, size=PREVIEW_BASE, uiscale=UI_SCALE,
            labels=model_preview.pv_labels(
                front=t("pv_front"), left=t("pv_left"), back=t("pv_back"),
                top=t("pv_top"), reset=t("pv_reset"), spin=t("pv_spin"),
                bone=t("pv_bone"), wire=t("pv_wire"),
                placeholder=t("pv_placeholder")))
        self.pv.pack(fill="both", expand=True)
        self.pv.set_bones(bool(self.var_bones.get()))
        self.lbl_stats = tk.Label(pvcard, text=t("preview_stats"),
                                  font=F_SMALL, bg=CARD, fg=MUTED,
                                  justify="left", wraplength=px(360))
        self.lbl_stats.pack(anchor="w", padx=px(10), pady=(0, px(10)))

        # ---- progress
        self.pb = tk.Frame(r, bg=BORDER, height=px(4))
        self.pb.pack(fill="x", side="bottom")
        self.pb_fill = tk.Frame(self.pb, bg=ACCENT, height=px(4))
        self.pb_fill.place(x=0, y=0, relwidth=0.0, relheight=1.0)
        self._anim = None

        self._toggle_outdir()
        self._pick_scale()
        self._update_hint()
        self._draw_dropzone()
        # 分割条位置要等窗口有真实宽度之后才能落到正确的地方
        self.root.after(80, self._poll_split)
        # 拖放注册必须等所有子控件的原生窗口真正创建后再做，见 _schedule_dnd。
        self._schedule_dnd()

    # --------------------------------------------------------- split pane --
    def _restore_split(self):
        """恢复上次记下的左右分栏比例（要到窗口有尺寸之后才生效）。"""
        try:
            frac = getattr(self, "_split", None)
            total = self.panes.winfo_width()
            if total > 1 and frac:
                x = int(total * frac)
                self.panes.sash_place(0, x, 0)
                return True
            return total > 1
        except Exception:
            return True

    def _poll_split(self, tries=0):
        if not self._restore_split() and tries < 20:
            self.root.after(60, lambda: self._poll_split(tries + 1))

    def _save_split(self):
        try:
            total = self.panes.winfo_width()
            x, _y = self.panes.sash_coord(0)
            if total > 1 and x > 0:
                self._split = min(0.85, max(0.28, x / float(total)))
        except Exception:
            pass

    def _card(self, parent, title):
        box = tk.Frame(parent, bg=CARD, highlightthickness=1,
                       highlightbackground=BORDER)
        tk.Label(box, text=title, font=F_BOLD, bg=CARD, fg=TXT,
                 anchor="w").pack(fill="x", padx=px(10), pady=(px(8), px(4)))
        return box

    def _btn(self, parent, text, cmd, kind="ghost", padx=None, pady=None):
        # 默认值在调用时才算，才能在缩放变化后生效
        padx = px(16) if padx is None else padx
        pady = px(8) if pady is None else pady
        if kind == "primary":
            bg, fg, active = ACCENT, "#ffffff", ACCENT_HOVER
        elif kind == "soft":
            bg, fg, active = ACCENT_SOFT, ACCENT, "#d6e4ff"
        else:
            bg, fg, active = CARD, TXT, "#eef1f5"
        b = tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg,
                      activebackground=active, activeforeground=fg,
                      relief="flat", bd=0, highlightthickness=1,
                      highlightbackground=BORDER if kind == "ghost" else bg,
                      font=F_BOLD if kind == "primary" else F_BODY,
                      padx=padx, pady=pady, cursor="hand2")
        if kind == "ghost":
            b.bind("<Enter>", lambda e: b.configure(bg=active))
            b.bind("<Leave>", lambda e: b.configure(bg=bg))
        b._base_bg = bg
        return b

    def _style_om(self, om, width):
        om.configure(bg=CARD, fg=TXT, font=F_BODY, bd=1, relief="solid",
                     highlightthickness=0, activebackground=CARD,
                     activeforeground=TXT, width=width, anchor="w", cursor="hand2")
        om["menu"].configure(bg=CARD, fg=TXT, font=F_BODY,
                             activebackground=ACCENT_SOFT, activeforeground=TXT, bd=0)

    def _style_notebook(self, nb):
        try:
            st = ttk.Style()
            try:
                st.theme_use("clam")
            except Exception:
                pass
            st.configure("TNotebook", background=BG, borderwidth=0)
            st.configure("TNotebook.Tab", font=F_BODY, padding=(px(12), px(6)),
                         background=CARD, foreground=TXT, borderwidth=1,
                         relief="flat")
            st.map("TNotebook.Tab",
                   background=[("selected", ACCENT), ("active", ACCENT_SOFT)],
                   foreground=[("selected", "#ffffff"), ("active", TXT)])
        except Exception:
            pass

    def _tab(self, nb, title):
        f = tk.Frame(nb, bg=CARD, highlightthickness=1,
                     highlightbackground=BORDER)
        nb.add(f, text=title)
        return f

    def _bigcheck(self, parent, text, var, command=None, pady=None):
        bc = BigCheck(parent, text=text, variable=var, command=command,
                      bg=CARD, font=F_BODY)
        bc.pack(anchor="w", padx=px(14),
                pady=(px(6) if pady is None else pady))
        return bc

    def _draw_dropzone(self):
        c = self.dz
        c.delete("all")
        w = max(c.winfo_width(), px(300))
        h = max(c.winfo_height(), px(126))
        hot = self._drag_hot
        c.create_rectangle(px(8), px(8), w - px(8), h - px(8),
                           outline=ACCENT if hot else "#ccd4e0",
                           width=max(1, px(2)), dash=(px(8), px(6)),
                           fill=ACCENT_SOFT if hot else "#fbfcfe")
        c.create_text(w / 2, h * 0.30, text="⇩", font=F_ICON,
                      fill=ACCENT)
        c.create_text(w / 2, h * 0.58, text=t("drop_hint"),
                      font=F_DROP, fill=TXT if not hot else ACCENT_HOVER)
        c.create_text(w / 2, h * 0.80,
                      text=t("drop_formats"),
                      font=F_DROP2, fill=MUTED)

    def _hover(self, state):
        if state != self._drag_hot:
            self._drag_hot = state
            self._draw_dropzone()

    def _schedule_dnd(self):
        """等窗口真正映射、子控件原生窗口都创建好之后，再注册拖放。

        关键点：`_build()` 里刚 pack 完控件时，Tk 还没把它们的原生子窗口创建
        出来（winfo_id() 此时只是父窗口 id，EnumChildWindows 也枚举不到）。若在此
        刻注册，会退化成「只有顶层注册」，拖到内容区仍然没反应。所以延迟到窗口
        映射之后，并先 update_idletasks() 强制创建所有子窗口，再枚举注册。
        """
        self._dnd_tries = 0
        self.root.after(120, self._do_install_dnd)

    def _do_install_dnd(self):
        try:
            # 强制 Tk 建好所有待创建的原生子窗口
            self.root.update_idletasks()
            ok = enable_drop(self.root.winfo_id(), self._on_drop_files,
                             register_children=True)
            self.dnd_ok = ok
            if not ok:
                self.log(t("msg_drop_unavail"), "warn")
                return
            # 窗口可能尚未映射（winfo_viewable 为假），此时枚举到的子窗口不全。
            # 隔一拍再补注册一次，覆盖后创建的控件；重建界面同理。
            self._dnd_tries += 1
            if not self.root.winfo_viewable() and self._dnd_tries < 8:
                self.root.after(200, self._do_install_dnd)
            else:
                # 追加一个延迟补注册，兜住布局稳定后才创建的子控件
                self.root.after(400, self._dnd_topup)
        except Exception:
            traceback.print_exc()

    def _dnd_topup(self):
        """布局稳定后再枚举一次，把此后新建的子控件也纳入拖放。"""
        try:
            if self.dnd_ok:
                enable_drop(self.root.winfo_id(), self._on_drop_files,
                            register_children=True)
        except Exception:
            traceback.print_exc()

    # ------------------------------------------------------------- ui zoom --
    def _on_zoom(self, label):
        self._set_zoom(ZOOM_BY_LABEL.get(label))

    def _on_lang(self, name):
        global APP_LANG
        code = None
        for k, v in LANG_NAMES.items():
            if v == name:
                code = k
                break
        if code is None or code == APP_LANG:
            return
        APP_LANG = code
        refresh_choices()
        try:
            self.var_zoom.set(zoom_label(self.zoom))
        except Exception:
            pass
        self._save_settings()
        self._rebuild()

    def _on_task(self, label):
        self._task_code = TASK_BY_LABEL.get(self.var_task.get(), "auto")
        self._save_settings()
        self._update_hint()

    def _update_hint(self):
        task = self._task_code
        tips = {
            "auto": t("hint_auto"),
            "fbx2pmx": t("hint_fbx2pmx"),
            "vrm2pmx": t("hint_vrm2pmx"),
            "pmx2vrm": t("hint_pmx2vrm"),
            "check": t("hint_check"),
        }
        if getattr(self, "lbl_hint", None) is not None:
            self.lbl_hint.configure(text=tips.get(task, ""))

    def _set_zoom(self, factor):
        old = UI_SCALE
        apply_scaling(self.root, factor)
        self.zoom = factor
        try:
            self.var_zoom.set(zoom_label(factor))
        except Exception:
            pass
        self._save_settings()
        if abs(UI_SCALE - old) < 1e-6:
            return
        self._rebuild()
        self.log(t("zoom_changed", label=zoom_label(factor),
                   pct=round(UI_SCALE * 100)), "info")

    def _rebuild(self):
        """按新的缩放倍率重建界面；日志内容和预览会保留。"""
        text = None
        if getattr(self, "txt", None) is not None:
            try:
                text = self.txt.get("1.0", "end-1c")
            except Exception:
                text = None
        self._progress(False)
        try:
            self._save_split()          # 重建前先记下当前分割位置
        except Exception:
            pass
        for child in self.root.winfo_children():
            child.destroy()
        self._build()
        if text:
            self.txt.configure(state="normal")
            self.txt.insert("1.0", text)
            self.txt.see("end")
            self.txt.configure(state="disabled")
        if self.busy:
            self.btn_go.configure(state="disabled", bg="#a9bde8")
            self._progress(True)
        self._restore_preview()

    def _restore_preview(self):
        if getattr(self, "last_mesh", None) is not None:
            self._show_mesh(self.last_mesh, self.last_pmx)

    # -------------------------------------------------------- settings io --
    def _load_settings(self):
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                s = json.load(f)
            lang = s.get("lang", "zh_CN")
            if lang in LANG:
                global APP_LANG
                APP_LANG = lang
            refresh_choices()
            self.var_lang.set(LANG_NAMES.get(APP_LANG, "简体中文"))
            self.var_flipz.set(bool(s.get("flip_z", True)))
            self.var_bones.set(bool(s.get("show_bones", False)))
            self.var_open_after.set(bool(s.get("open_after", False)))
            self.var_same_dir.set(bool(s.get("same_dir", True)))
            self.var_outdir.set(s.get("outdir", "") or "")
            self.var_scale_value.set(str(s.get("scale_value", "1.0")))
            mode = s.get("scale_mode", "auto")
            self.var_scale_auto.set(mode == "auto")
            self.var_scale_raw.set(mode == "raw")
            self.var_scale_custom.set(mode == "custom")
            self.zoom = norm_zoom(s.get("ui_zoom"))
            self.var_zoom.set(zoom_label(self.zoom))
            try:
                sp = float(s.get("split", 0.6))
                if 0.2 < sp < 0.9:
                    self._split = sp
            except Exception:
                pass
            self.var_vrm_spec.set(s.get("vrm_spec", "1.0"))
            self.var_morphs.set(bool(s.get("export_morphs", True)))
            self.var_two_sided.set(bool(s.get("force_two_sided", False)))
            self.var_edge.set(bool(s.get("vrm2pmx_edge", False)))
            self.var_center.set(bool(s.get("center", True)))
            self.var_fbx_morphs.set(bool(s.get("fbx_morphs", True)))
            self.var_remove_alpha.set(bool(s.get("remove_alpha", False)))
            code = s.get("task", "auto")
            if code not in {c for _, c in TASK_CHOICES}:
                code = TASK_BY_LABEL.get(code, "auto")  # 兼容旧版以标签存储的设置
            if code not in {c for _, c in TASK_CHOICES}:
                code = "auto"
            self._task_code = code
            self.var_task.set(TASK_BY_LABEL.get(code, TASK_LABELS[0]))
        except Exception:
            pass

    def _save_settings(self):
        try:
            mode = ("auto" if self.var_scale_auto.get() else
                    "raw" if self.var_scale_raw.get() else "custom")
            try:
                self._save_split()
            except Exception:
                pass
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump({"flip_z": self.var_flipz.get(),
                           "show_bones": self.var_bones.get(),
                           "open_after": self.var_open_after.get(),
                           "same_dir": self.var_same_dir.get(),
                           "outdir": self.var_outdir.get(),
                           "scale_mode": mode,
                           "scale_value": self.var_scale_value.get(),
                           "ui_zoom": ("auto" if self.zoom is None
                                       else self.zoom),
                           "lang": APP_LANG,
                           "task": self._task_code,
                           "vrm_spec": self.var_vrm_spec.get(),
                           "export_morphs": self.var_morphs.get(),
                           "force_two_sided": self.var_two_sided.get(),
                           "vrm2pmx_edge": self.var_edge.get(),
                           "center": self.var_center.get(),
                           "fbx_morphs": self.var_fbx_morphs.get(),
                           "remove_alpha": self.var_remove_alpha.get(),
                           "split": getattr(self, "_split", 0.6)},
                          f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ------------------------------------------------------------ logging --
    def log(self, text, tag="mono"):
        self.txt.configure(state="normal")
        self.txt.insert("end", text + "\n", tag)
        self.txt.see("end")
        self.txt.configure(state="disabled")

    def clear_log(self):
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.configure(state="disabled")

    def status(self, text):
        self.lbl_status.configure(text=text)

    def _progress(self, on):
        if on:
            self.pb_fill.place_configure(relwidth=0.15)
            self._animate()
        else:
            if self._anim:
                self.root.after_cancel(self._anim)
                self._anim = None
            self.pb_fill.place_configure(relwidth=0.0)

    def _animate(self):
        try:
            x = float(self.pb_fill.place_info().get("x") or 0)
        except Exception:
            x = 0
        width = self.pb.winfo_width() or 900
        step = 9
        x = (x + step) % (width + 200)
        self.pb_fill.place_configure(x=x - 140, relwidth=0.15)
        self._anim = self.root.after(28, self._animate)

    # ---------------------------------------------------------- ui handlers --
    def _pick_scale(self):
        custom = self.var_scale_custom.get()
        self.ent_scale.configure(state="normal" if custom else "disabled",
                                 fg=TXT if custom else FAINT)

    def _toggle_outdir(self):
        same = self.var_same_dir.get()
        self.ent_out.configure(state="disabled" if same else "normal",
                               fg=FAINT if same else TXT)
        self.btn_browse_out.configure(state="disabled" if same else "normal")
        if same:
            self.ent_out.delete(0, "end")
            self.ent_out.insert(0, t("outdir_placeholder"))

    def browse(self):
        types = [("支持的模型文件", "*.fbx *.unitypackage *.vrm *.pmx"),
                 ("FBX 模型", "*.fbx"),
                 ("Unity 资源包", "*.unitypackage"),
                 ("VRM 模型", "*.vrm"),
                 ("PMX 模型", "*.pmx"),
                 ("所有文件", "*.*")]
        paths = filedialog.askopenfilenames(title=t("dlg_title"),
                                            filetypes=types)
        if paths:
            self.accept_paths(list(paths))

    def pick_outdir(self):
        d = filedialog.askdirectory(title=t("dlg_out"))
        if d:
            self.var_outdir.set(d)

    def open_outdir(self):
        target = self.last_out_dir
        if not target:
            if not self.var_same_dir.get() and self.var_outdir.get().strip():
                target = self.var_outdir.get().strip()
            else:
                target = BASE
        try:
            if os.path.isdir(target):
                os.startfile(target)
            else:
                messagebox.showinfo(t("msg_title"), t("msg_no_outdir"))
        except Exception as e:
            messagebox.showerror(t("msg_open_fail"), str(e))

    def start_from_ui(self):
        if self.busy:
            return
        if not self._pending_files:
            messagebox.showinfo(t("msg_title"), t("msg_pick"))
            return
        self._run(self._pending_files)

    # ------------------------------------------------------------- pipeline --
    def _on_drop_files(self, paths):
        # 由原生窗口回调（ctypes WINFUNCTYPE）调用：此时 GIL 持有但【没有 Tcl 锁】，
        # 因此严禁调用任何 Tk 函数。只把路径暂存到属性并置事件，主线程的轮询循环
        # （_poll_drop，运行在 mainloop 内）会取走并处理。
        self._pending_drop = paths
        self._drop_event.set()

    def _poll_drop(self):
        try:
            if self._drop_event.is_set():
                self._drop_event.clear()
                files = self._pending_drop
                self._pending_drop = None
                if files and not self.busy:
                    self.accept_paths(files)
        except Exception:
            traceback.print_exc()
        finally:
            self.root.after(60, self._poll_drop)

    def accept_paths(self, paths):
        if self.busy:
            return
        task = self._task_code
        files = []
        for p in paths:
            if classify(p) == "dir":
                files.extend(find_models(p) if task == "auto" else find_fbx(p))
            else:
                files.append(p)
        if not files:
            return
        unknown = [os.path.basename(f) for f in files
                   if classify(f) == "unknown"]
        if unknown:
            messagebox.showwarning(t("msg_unsupported"),
                                   t("msg_unsupported_body",
                                     files="\n".join(unknown[:5])))
            files = [f for f in files if classify(f) != "unknown"]
        if not files:
            return
        if task != "auto":
            # 指定了方向就只挑对应扩展名的文件
            want = {"fbx2pmx": ("fbx", "unitypackage"),
                    "vrm2pmx": ("vrm",),
                    "pmx2vrm": ("pmx",),
                    "check": ("pmx",)}[task]
            files = [f for f in files if classify(f) in want]
            if not files:
                messagebox.showinfo(t("msg_title"), t("msg_mismatch"))
                return
        self._pending_files = files
        self._run(files)

    def _snapshot_cfg(self):
        if self.var_scale_auto.get():
            scale = "mmd"
        elif self.var_scale_raw.get():
            scale = 1.0
        else:
            try:
                scale = float(self.var_scale_value.get().strip())
            except ValueError:
                scale = "mmd"
        return {
            "scale": scale,
            "flip_z": bool(self.var_flipz.get()),
            "outdir": (None if self.var_same_dir.get()
                       else (self.var_outdir.get().strip() or None)),
            "open_after": bool(self.var_open_after.get()),
            "show_bones": bool(self.var_bones.get()),
            "task": self._task_code,
            "vrm_spec": (self.var_vrm_spec.get() or "1.0"),
            "export_morphs": bool(self.var_morphs.get()),
            "force_two_sided": bool(self.var_two_sided.get()),
            "edge": bool(self.var_edge.get()),
            "center": bool(self.var_center.get()),
            "fbx_morphs": bool(self.var_fbx_morphs.get()),
            "remove_alpha": bool(self.var_remove_alpha.get()),
        }

    def _run(self, files):
        cfg = self._snapshot_cfg()
        self._save_settings()
        self.busy = True
        self.btn_go.configure(state="disabled", bg="#a9bde8")
        self._progress(True)
        self.status(t("status_process", n=len(files)))
        self.log("")
        self.log(t("log_start", n=len(files)), "head")
        # 先把第一个文件渲染成背视图，进转换前就能看到模型
        self._kick_preview(files)
        thr = threading.Thread(target=self._worker, args=(files, cfg),
                               daemon=True)
        thr.start()

    # -- worker thread ------------------------------------------------------
    def _worker(self, files, cfg):
        old_out = sys.stdout
        sys.stdout = stdout_redirect(self.q)
        made = []
        try:
            for path in files:
                made.extend(self._convert_one(path, cfg))
        except Exception:
            self.q.put(("log", traceback.format_exc()))
            self.q.put(("err", "转换失败，详见日志"))
        finally:
            try:
                sys.stdout.flush()
            except Exception:
                pass
            sys.stdout = old_out
            self.q.put(("done", made, cfg))

    def _logfn(self):
        """给转换脚本用的日志回调（在 worker 线程里跑，只能往队列里塞）。"""
        q = self.q

        def fn(msg, tag=None):
            q.put(("log", msg, tag or "mono"))
        return fn

    def _convert_one(self, path, cfg):
        q = self.q
        kind = classify(path)
        task = cfg.get("task", "auto")
        if task == "auto":
            task = AUTO_TASK.get(kind, "")
        if not task:
            q.put(("log", "跳过不支持的文件：%s" % os.path.basename(path),
                   "warn"))
            return []
        folder = cfg["outdir"] or os.path.dirname(os.path.abspath(path)) or BASE
        # 输出目录可能还不存在（例如用户指定的 out/ 子目录），先建好再写文件
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as e:
            q.put(("log", "无法创建输出目录 %s：%s" % (folder, e), "err"))
            return []

        if task == "check":
            q.put(("log", ""))
            q.put(("log", "→ %s（仅校验）" % os.path.basename(path), "head"))
            self._report(path, os.path.dirname(os.path.abspath(path)), cfg)
            return [path]

        if task == "vrm2pmx":
            return self._do_vrm2pmx(path, folder, cfg)

        if task == "pmx2vrm":
            return self._do_pmx2vrm(path, folder, cfg)

        return self._do_fbx2pmx(path, kind, folder, cfg)

    # -- VRM → PMX ---------------------------------------------------------
    def _do_vrm2pmx(self, path, folder, cfg):
        q = self.q
        stem = os.path.splitext(os.path.basename(path))[0]
        out = os.path.join(folder, stem + ".pmx")
        q.put(("log", ""))
        q.put(("log", "→ %s" % os.path.basename(path), "head"))
        q.put(("log", "读取 VRM…", "info"))
        st = vrm2pmx.convert(path, out, scale_mode=cfg["scale"],
                             log=self._logfn(), name=stem,
                             enable_edge=cfg.get("edge", False),
                             force_double_sided=cfg.get("force_two_sided",
                                                        False))
        q.put(("log", "已写出 %s（%s）"
               % (os.path.basename(out), human(st["bytes"])), "ok"))
        self._report(out, folder, cfg)
        return [out]

    # -- PMX → VRM ---------------------------------------------------------
    def _do_pmx2vrm(self, path, folder, cfg):
        q = self.q
        stem = os.path.splitext(os.path.basename(path))[0]
        out = os.path.join(folder, stem + ".vrm")
        q.put(("log", ""))
        q.put(("log", "→ %s" % os.path.basename(path), "head"))
        q.put(("log", "读取 PMX…", "info"))
        st = pmx2vrm.convert(
            path, out, spec=cfg["vrm_spec"], scale_mode=cfg["scale"],
            meta={"title": stem}, log=self._logfn(),
            max_morphs=(None if cfg.get("export_morphs", True) else 0),
            force_double_sided=cfg.get("force_two_sided", False))
        q.put(("log", "已写出 %s（%s）"
               % (os.path.basename(out), human(st["bytes"])), "ok"))
        q.put(("log", "VRM 版本 %s · 节点 %d · primitive %d"
               % (cfg["vrm_spec"], st["nodes"], st["primitives"]), "mono"))
        # 预览用源 PMX 渲染（VRM 本身没有渲染器）
        self._report(path, os.path.dirname(os.path.abspath(path)), cfg)
        return [out]

    # -- FBX / unitypackage → PMX -----------------------------------------
    def _do_fbx2pmx(self, path, kind, folder, cfg):
        q = self.q
        made = []
        if kind == "unitypackage":
            q.put(("log", "解包 %s" % os.path.basename(path)))
            q.put(("log", "", "mono"))
            dest = os.path.join(folder, os.path.splitext(
                os.path.basename(path))[0] + "_extracted")
            unpack_unitypackage(path, dest)
            fbxs = find_fbx(dest)
            if not fbxs:
                q.put(("log", "包里没有找到 FBX。", "err"))
                return made
            fbxs.sort(key=lambda p: -os.path.getsize(p))
            q.put(("log", "包内 FBX（按体积排序）：", "info"))
            for p in fbxs:
                q.put(("log", "    %-46s %s"
                       % (os.path.relpath(p, dest)[:46], human(os.path.getsize(p))),
                       "info"))
            folder = dest
        elif kind == "fbx":
            fbxs = [path]
        else:
            return made

        for fbx in fbxs:
            stem = os.path.splitext(os.path.basename(fbx))[0]
            out = os.path.join(folder, stem + ".pmx")
            q.put(("log", ""))
            q.put(("log", "→ %s" % os.path.basename(fbx), "head"))
            q.put(("log", "读取 FBX…", "info"))
            scene = fbx2pmx.Scene(fbx)
            nb = sum(1 for m in scene.models.values() if m.cls == "LimbNode")
            ngeo = sum(1 for n in scene.objects if n.name == "Geometry")
            q.put(("log", "   FBX 7.%d：%d 个网格，%d 根骨骼"
                   % (scene.ver, ngeo, nb), "info"))
            q.put(("log", "转换中…", "info"))
            st = fbx2pmx.convert(scene, out, scale_mode=cfg["scale"],
                                 flip_z=cfg["flip_z"], name=stem,
                                 center=cfg.get("center", True),
                                 morphs=cfg.get("fbx_morphs", True),
                                 remove_alpha=cfg.get("remove_alpha", False))
            _extra = ""
            if st.get("morphs"):
                _extra = "，%d 个表情" % st["morphs"]
            q.put(("log", "已写出 %s（%s%s）"
                   % (os.path.basename(out), human(st["bytes"]), _extra), "ok"))
            self._report(out, folder, cfg)
            made.append(out)

        return made

    def _report(self, pmx, folder, cfg):
        q = self.q
        q.put(("log", "校验中…", "info"))
        try:
            m, problems = validate_pmx(pmx)
        except Exception as e:
            q.put(("log", "校验读取失败：%s" % e, "err"))
            return
        q.put(("log", "   顶点 %d · 三角面 %d · 骨骼 %d · 材质 %d"
               % (len(m["vertices"]), len(m["faces"]) // 3,
                  len(m["bones"]), len(m["materials"])), "mono"))
        if m["morphs"]:
            q.put(("log", "   表情 %d 个" % len(m["morphs"]), "mono"))
        if problems:
            for p in problems:
                q.put(("log", "   ✗ " + p, "err"))
        else:
            q.put(("log", "   ✓ 结构校验全部通过（索引、权重、字节数、材质覆盖）",
                   "ok"))
        if len(m["textures"]) == 0:
            q.put(("log", "   提示：无贴图引用，进 MMD 是纯色模型", "warn"))
        if len(m["morphs"]) == 0:
            q.put(("log", "   提示：无表情数据，需在 PMXEditor 里手动添加", "warn"))

        # preview：读取 PMX 网格 → 实时背视图预览（只显示，不落盘保存图片）
        try:
            q.put(("log", "生成预览…", "info"))
            mesh = model_preview.mesh_from_pmx(pmx)
            q.put(("mesh", mesh, pmx))
        except Exception as e:
            q.put(("log", "预览渲染失败：%s" % e, "warn"))

    # -- UI pump ------------------------------------------------------------
    def _pump(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self.log(msg[1], msg[2] if len(msg) > 2 else "mono")
                elif kind == "mesh":
                    self._show_mesh(msg[1], msg[2], reflow=True)
                elif kind == "mesh_tex":
                    self._show_mesh(msg[1], msg[2], reflow=False)
                elif kind == "drop":
                    self.accept_paths(msg[1])
                elif kind == "err":
                    self.log(msg[1], "err")
                elif kind == "done":
                    self._finish(msg[1], msg[2])
        except queue.Empty:
            pass
        self.root.after(60, self._pump)

    def _show_mesh(self, mesh, pmx=None, reflow=True):
        """把加载好的预览网格交给实时预览控件。

        reflow=True：首次装载（重置视角、计算 LOD）。
        reflow=False：仅贴图到位后重绘，不打断用户当前视角。
        """
        if mesh is None:
            return
        if pmx:
            self.last_pmx = pmx
        self.last_mesh = mesh
        try:
            if reflow:
                self.pv.set_mesh(mesh)
                self.pv.set_bones(bool(self.var_bones.get()))
            else:
                self.pv.redraw()
        except Exception as e:
            self.log("预览显示失败：%s" % e, "warn")
            return
        if not reflow:
            return
        st = mesh.stats()
        name = os.path.basename(pmx) if pmx else mesh.name
        extra = ""
        if pmx and os.path.exists(pmx):
            extra = t("pv_stats_file", size=human(os.path.getsize(pmx)))
        self.lbl_stats.configure(
            text=t("pv_stats", name=name, v=st["verts"], t=st["tris"],
                   b=st["bones"], m=st["mats"], size=extra))

    def _preview_sources(self, files):
        """挑出能直接预览的文件（排除目录）。"""
        out = []
        for f in files:
            if os.path.isfile(f) and model_preview.classify(f) != "unknown":
                out.append(f)
        return out

    def _kick_preview(self, files):
        """转换开始前先把第一个文件的背视图显示出来（实时反馈）。

        对 VRM / PMX 这类可能内嵌大量贴图的格式，先以“基色”立刻出背视图，
        再在后台线程解码贴图、到位后只重绘（不打断用户视角）——保证拖入即见。
        """
        srcs = self._preview_sources(files)
        if not srcs:
            return
        first = srcs[0]
        kind = model_preview.classify(first)

        def job():
            try:
                if kind in ("vrm", "pmx"):
                    mesh = model_preview.load_preview(first, with_textures=False)
                    self.q.put(("mesh", mesh, first))     # 立刻出背视图（基色）
                    if mesh.load_textures():
                        self.q.put(("mesh_tex", mesh, first))  # 贴图到位后重绘
                else:
                    mesh = model_preview.load_preview(first)
                    self.q.put(("mesh", mesh, first))
            except Exception as e:
                self.q.put(("log", "预览失败：%s" % e, "warn"))
        threading.Thread(target=job, daemon=True).start()

    def _rerender(self):
        # “预览叠加骨骼点”勾选后即时重绘
        if getattr(self, "pv", None) is not None:
            self.pv.set_bones(bool(self.var_bones.get()))

    def _finish(self, made, cfg):
        self.busy = False
        self.btn_go.configure(state="normal", bg=ACCENT)
        self._progress(False)
        self.last_out_dir = os.path.dirname(made[0]) if made else None
        if made:
            self.log("")
            self.log(t("log_done", n=len(made)), "head")
            for p in made:
                self.log("   " + p, "ok")
            self.status(t("status_done", n=len(made)))
            if cfg.get("open_after") and self.last_out_dir:
                try:
                    os.startfile(self.last_out_dir)
                except Exception:
                    pass
        else:
            self.log(t("log_none"), "err")
            self.status(t("status_none"))
        self._save_settings()

    def _on_close(self):
        self._save_settings()
        self.root.destroy()


def main():
    declare_dpi_aware()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    root = tk.Tk()
    # 必须在建立任何控件之前把字体缩放定好。
    # 自动 = 跟随系统 DPI：200% 缩放的屏幕就是 2.0，字号与其它程序一致。
    apply_scaling(root, load_zoom_pref())
    app = ConverterApp(root)
    app.log(t("ready"), "info")
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        err = traceback.format_exc()
        try:
            import tkinter.messagebox as mb
            mb.showerror("启动失败", err)
        except Exception:
            sys.stderr.write(err)
        raise
