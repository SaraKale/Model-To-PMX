# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['main.py'],
    # 引擎模块已归类到 formats / convert / gfx 子目录，且 main.py 里是用裸名
    # import（import fbx2pmx 等）。PyInstaller 只做静态分析、看不到 main.py
    # 运行期才加的 sys.path，所以必须在这里把子目录加进 pathex，否则打包后
    # 运行到 import 处会报 ModuleNotFoundError。
    pathex=['.', 'formats', 'convert', 'gfx'],
    binaries=[],
    datas=[],
    hiddenimports=['fbx2pmx', 'pmx_check', 'pmx2vrm', 'vrm2pmx', 'preview',
                   'unitypackage_unpack', 'fbx_reader', 'pmxio', 'vrmio',
                   # UEFormat（.uemodel）双向转换 + ASCII FBX 写出器
                   'uemodel2pmx', 'pmx2uemodel', 'uemodelio', 'fbxout',
                   # 解压 ZSTD 压缩体（.uemodel 运行期懒加载）
                   'zstandard'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='ModelConvert.exe',
    icon='图标2.ico',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='ModelConvert.exe',
)
