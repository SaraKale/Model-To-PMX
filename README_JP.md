# FBX / VRM / PMX モデル変換ツール（純 Python）

`.fbx`・`.unitypackage`・`.vrm`・`.pmx` を相互に変換します。**すべて Python 標準ライブラリだけで実装**しています。
Blender / Autodesk FBX SDK / Unity 3D は不要、mmd_tools / UniVRM といったプラグインも不要です。
バイナリ形式を直接読み書きし、ウィンドウへドラッグするだけで変換できます。

[English](README.md) | [简体中文](README_SC.md) | [繁體中文](README_TC.md) | [日本語](README_JP.md)

![Preview](Document\Preview-jp.jpg)
---

## ダウンロード

最新バージョンは [releases](https://github.com/SaraKale/Model-to-PMX/releases/latest) からダウンロードしてください。

## 一、機能特徴

### 対応している変換方向

| 方向 | 説明 |
|---|---|
| FBX / unitypackage → PMX | MMD 用 PMX 2.0、四角面は自動的にファン三角形化 |
| VRM（0.x / 1.0） → PMX | humanoid ボーンを自動認識し、不足分はプレースホルダー骨で補完 |
| PMX → VRM（0.x / 1.0） | VRM メタ・humanoid ボーン映射・morph target を自動書き込み |
| PMX → 検証 + プレビューのみ | 構造の読み取り専用検証、ファイルは出力しない |

### コア機能

- **外部依存ゼロ**：純 Python 標準ライブラリ（tkinter + ctypes）。`Pillow` は任意の高速化項目で、
  なくてもテクスチャのデコード速度と一部プレビューにしか影響せず、メインフローには影響しません。
- **ドラッグ＆ドロップで即利用**：Windows ネイティブの `WM_DROPFILES` を使用。ウィンドウ内のどこにでもドロップでき、
  tkinterdnd2 のようなサードパーティ製ライブラリは不要です。
- **リアルタイム背面プレビュー**：モデルをドラッグすると即座に 3D 背面図が表示され、ドラッグで回転 / ホイールで拡縮、
  ボーン点を重ねて位置合わせを確認できます。
- **多言語 UI**：右上で 简体中文 / 繁體中文 / English / 日本語 を切り替えられ、選択は設定に記憶されます。
- **高解像度ディスプレイ対応**：システム DPI に自動追従。右上の「界面缩放（UI 拡縮）」で倍率を手動指定も可。
- **タブ式オプション**：オプション欄は「通用 / FBX / VRM / 出力」の 4 タブに分かれ、拡張しやすいです。
- **巻き順の自動判定**：三角形の巻き順を「幾何面法線 vs 頂点法線」の投票で自動判定し、
  大面積の抜け / 輪郭線が黒い塊になるのを防ぎます。
- **テクスチャ処理**：PNG/JPEG はそのまま透過、BMP/TGA はその場で PNG に変換、GLB へ埋め込むか PMX と同じフォルダへ書き出し。

---

## 二、環境要件

- **Windows**（ドラッグ＆ドロップと GUI は Windows ネイティブ API に依存）
- **Python 3.10+**（3.12 / 3.13 / 3.14 推奨、tkinter 同梱が必要）
- **任意**：`Pillow`（テクスチャのデコードとプレビューを高速化）

---

## 三、ディレクトリ構成

```
Model-to-PMX/
├── main.py                      # ★ グラフィカル UI の入口（完全実装）
├── main.spec                    # ★ PyInstaller のパッケージ設定
├── config.json                  # UI 設定（言語 / 拡縮 / オプション等）
│
├── formats/                     # フォーマット読み書き / 解凍
│   ├── fbx_reader.py            #   バイナリ FBX 7.x 解析ライブラリ
│   ├── fbx_probe.py             #   FBX 構造の調査（メッシュ/ボーン/スキン一覧）
│   ├── pmxio.py                 #   完全な PMX 2.0 読み書き（表情/IK/付与/剛体/ジョイント含む）
│   ├── vrmio.py                 #   GLB/glTF コンテナ読み書き + PNG エンコード + BMP/TGA デコード
│   └── unitypackage_unpack.py   #   .unitypackage の解凍（gzip tar）
│
├── convert/                     # 変換エンジン
│   ├── fbx2pmx.py               #   FBX → PMX
│   ├── vrm2pmx.py               #   VRM → PMX
│   ├── pmx2vrm.py               #   PMX → VRM
│   └── pmx_check.py             #   PMX 検証 + ソフトウェア描画のプレビュー画像
│
├── gfx/
│   └── preview.py               # リアルタイム 3D 背面プレビュー部品
│
```

> エンジン・モジュールは役割ごとに `formats/` `convert/` `gfx/` サブディレクトリにまとめられており、実行時に `main.py` が
> この 3 ディレクトリを `sys.path` に挿入するため、モジュール間は依然として裸名の `import`（例：`import fbx2pmx`）を使っています。

---

## 四、使い方

### 方法 1：グラフィカル UI（推奨）

1. 実行：`python main.py`
2. `.fbx` / `.unitypackage` / `.vrm` / `.pmx` ファイルを**ウィンドウ内の任意の場所へドラッグ**、またはドロップ領域をクリックしてファイルを選択。
1. 「タスク」ドロップダウンで変換方向を手動指定可能。既定は拡張子から自動判定。
2. 変換ログは左下、モデルプレビューは右の全高カラム。

UI のポイント：

- **左右分割**：Blender のエリアのように中央の仕切りをドラッグして幅を変更でき、位置は `config.json` に保存されます
- **右カラム全体がプレビュー**：背面部のリアルタイム表示。下のツールバーは 正面 / 左面 / 背面 / 上面 / リセット / 回転 / ボーン / ワイヤー
- **プレビュー右下の描画バックエンド表示**：`Pillow` か純 Python か、および前フレームの所要時間（ms）
- **右上「言語」**：简体中文 / 繁體中文 / English / 日本語（プレビューのツールバーも追従）
- **右上「界面缩放（UI 拡縮）」**：システム DPI に自動追従、または倍率を手動指定
- **オプションタブ**：「通用 / FBX / VRM / 出力」の 4 タブ
- **チェックボックス拡大**：チェック枠とクリック領域が押しやすくなっています

### 重いモデルでもプレビューが軽い理由

リアルタイムプレビューは OpenGL を使わない純 Python のソフトウェア・ラスタライザなので、
3 つの仕組みで滑らかさを保っています。

1. **ドラッグ中は間引きメッシュ** —— 操作中は約 12,000 面に間引いて描画。
   90k 面のモデルでも 1 フレーム約 40ms；
2. **手を止めたら分割リファイン** —— 本描画は 1 スライス 1500 面ずつ、
   1 回あたり最大 24ms に制限して進めるため、UI が固まらず絵が少しずつ精細になります；
3. **ピクセル予算の自動調整** —— 実測フレーム時間に応じて解像度を上下させ、
   Pillow があれば低解像度の結果をキャンバスサイズへ拡大する処理を C 側で行います。

### 方法 2：コマンドライン

> スペースや日本語を含むパスは必ず二重引用符で囲むこと。エンジン・スクリプトは `convert/` `formats/` サブディレクトリにあります。

```powershell
# グラフィカル UI を開く
python main.py

# FBX → PMX
python convert/fbx2pmx.py "model.fbx" -o "model.pmx"

# unitypackage の解凍（--list は内容のみ一覧表示して解凍しない）
python formats/unitypackage_unpack.py "pack.unitypackage" --list
python formats/unitypackage_unpack.py "pack.unitypackage"

# FBX 構造の調査
python formats/fbx_probe.py "model.fbx"

# PMX → VRM 1.0（既定）/ 0.x
python convert/pmx2vrm.py "model.pmx" -o "model.vrm"
python convert/pmx2vrm.py "model.pmx" --spec 0x --title "名前" --author "作者"

# VRM → PMX（テクスチャは PMX と同じフォルダへ自動書き出し）
python convert/vrm2pmx.py "model.vrm" -o "model.pmx"

# PMX 検証 + プレビュー画像生成
python convert/pmx_check.py "model.pmx"
python convert/pmx_check.py "model.pmx" --bones   # ボーン位置を重ねる
```

#### よく使うオプション早見表

`fbx2pmx.py`：

| オプション | 説明 |
|---|---|
| `--scale mmd` | 既定。MMD 標準身長（約 20 単位）へ自動スケーリング |
| `--scale raw` | FBX の元のサイズ（メートル）を維持 |
| `--scale 12.5` | 倍率を手動指定 |
| `--no-flip-z` | 右手系→左手系の変換を行わない（既定では変換する） |
| `--info` | 構造情報のみ表示し、変換は行わない |

`pmx2vrm.py`：`--spec 1.0|0x`、`--scale auto|倍率`、`--rotate auto|none|y180`、
`--flip-winding`、`--force-double-sided`、`--max-morphs N`、`--title` / `--author`。

`vrm2pmx.py`：`--scale`、`--rotate`、`--flip-winding`、`--edge`（輪郭線を有効化）、
`--force-double-sided`、`--name`。

より詳しいオプションとフォーマット映射については `FBX转PMX_使用说明.md` と `VRM互转_使用说明.md` を参照してください。

---

## 五、ビルドとパッケージ化

### 1. PyInstaller のインストール

```powershell
pip install pyinstaller
pip install pillow          # 任意。同梱するビルドにもテクスチャ高速化を効かせる
```

### 2. spec を使ったビルド（推奨）

> **必ず spec を使うこと**。直接 `pyinstaller main.py` はしないでください。

```powershell
pyinstaller main.spec
```

成果物：`dist/ModelConvert.exe/ModelConvert.exe`（one-folder モード）。

### 3. なぜ spec が必須か

エンジン・モジュールは `formats/` `convert/` `gfx/` サブディレクトリにまとめられており、`main.py` 内では
`import fbx2pmx` のような裸名 import を使っています。サブディレクトリが `sys.path` に加わるのは**実行時**です。
**PyInstaller は静的解析しか行わず、実行時のパス注入を見ることができません**——`pathex` にこれらの
サブディレクトリがなければ、ビルド段階で「見つからないモジュール」として破棄されます。ビルドは成功しますが、実行すると次のエラーになります：

```
ModuleNotFoundError: No module named 'fbx2pmx'
```

`main.spec` にはすでに正しく設定されています：

```python
Analysis(
    ['main.py'],
    pathex=['.', 'formats', 'convert', 'gfx'],     # 静的解析でサブディレクトリのモジュールを見つける
    hiddenimports=['fbx2pmx', 'pmx_check', 'pmx2vrm', 'vrm2pmx', 'preview',
                   'unitypackage_unpack', 'fbx_reader', 'pmxio', 'vrmio'],
    ...
)
```

spec を使わない場合の同等の書き方：

```powershell
pyinstaller --paths formats --paths convert --paths gfx -w main.py
```

### 4. よく使うオプション

| オプション | 説明 |
|---|---|
| `-F` | 単一の実行ファイルにパッケージ化 |
| `-D` | 複数ファイルを含むフォルダにパッケージ化（既定） |
| `-w` | コンソールウィンドウを非表示（GUI アプリ） |
| `-i icon.ico` | 実行ファイルのアイコンを指定 |
| `-n 名前` | 生成する実行ファイル名を指定 |
| `--add-data "元:先"` | リソースファイルを追加 |
| `--hidden-import モジュール名` | 隠し依存を手動で補完 |

---

## 六、注意事項

### 共通

1. **スペース / 日本語を含むパス**：コマンドライン呼び出し時は必ず二重引用符で囲む。
2. **設定ファイル `config.json`**：言語・UI 拡縮・タスク方向・各オプションなどを記録。削除すると既定値で再作成される。
1. **ショートカットと操作**：プレビュー欄はドラッグで回転、ホイールで拡縮。ドロップ領域をクリックでファイル選択ダイアログを開く。

### モデルが真っ黒 / 黒い縁 / 抜けがある

- **変換後モデルに黒い線が入る**：本ツールはデフォルトで PMX 出力時に MMD の輪郭線を無効化します（頂点 edge=0、材質 edge_size=0、edge_color alpha=0）。それでも黒い縁が残る場合は、MMD / PMXEditor で全材質を選択し**輪郭線をオフ**にし、さらに**「両面描画」**にチェックを入れる。
- **それでも抜けがある**：まずログの `绕序自动判定`（巻き順自動判定）の行を見る。判定が誤っているなら `--flip-winding` を追加。薄い幾何（髪 / スカート）が両面描画に依存する場合は `--force-double-sided` を追加、あるいは UI の「材质强制双面（材質の両面化を強制）」にチェック。

### 既知の制限

- **FBX 構造**：バイナリ FBX 7.x のみ対応。ASCII FBX は非対応。
- **テクスチャ形式**：DDS / KTX2 / WebP はスキップされる（材質は単色に退化）。
- **物理**：PMX 剛体/ジョイント ↔ VRM SpringBone は**相互変換されない**。
- **材質効果**：球環境マップ（.sph/.spa）・toon マップは VRM 側に保持されない。
- **ボーン名**：FBX 変換は英語ボーン名（`Hips`、`Spine` …）を維持するため、MMD の既存モーション（.vmd）と一致せず、PMXEditor で日本語標準名へ一括変更が必要。
- **表情**：ソースモデルに BlendShape / morph がない場合、PMX の表情も 0 となり、手作業での作成が必要。

### ドラッグ＆ドロップ関連

- ドラッグ＆ドロップは Windows ネイティブの `WM_DROPFILES` に依存し、**Windows でのみ利用可能**。他 OS では「参照」ボタンでファイルを選択してください。
- ログに「系统未启用拖放接口（ドラッグ＆ドロップ接口が有効化されていません）」と表示された場合、現在の環境はネイティブなドラッグ＆ドロップに対応していません。ボタンからの選択に切り替えてください。

### パッケージ化関連

1. **`pyinstaller main.spec` を使うこと**（または `--paths` 付きの同等コマンド）。さもないとサブディレクトリ内のエンジン・モジュールが漏れ、実行時に `No module named 'fbx2pmx'` となる。
1. **ソースを変更したら再ビルドが必要**。古い exe をダブルクリックしても反映されない。
2. パッケージ化環境の Python には tkinter が含まれている必要がある（Windows 公式インストーラーは既定で含む）。

---

## 七、著作権の注意

VRM の meta にはライセンス情報が含まれます。本ツールは最も保守的な「表示（帰属）必須・再配布禁止・改変禁止」を既定で書き込みます。

**変換前に、そのモデルを利用する権利があるか自身で確認してください**。本ツールは著作権チェックを行いません。
