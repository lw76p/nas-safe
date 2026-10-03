# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['scripts/desktop_agent.py'],
    pathex=[],
    binaries=[],
    datas=[('agent/nassafe_agent.ico', '.'), ('agent/nassafe_agent_alert.ico', '.')],
    hiddenimports=[],
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
    a.binaries,
    a.datas,
    [],
    name='桌面助手',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version='agent/version-file.txt',
    icon=['agent/nassafe_agent.ico'],
)
