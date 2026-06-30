# PyInstaller spec — used by GitHub Actions to build per-platform binaries.
# Build locally: pyinstaller scubed_timesheet.spec
# Playwright ships its own PyInstaller hooks; no collect_all needed.

import sys

block_cipher = None

a = Analysis(
    ['gui.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('.env.example', '.'),
    ],
    hiddenimports=[
        'playwright',
        'playwright.async_api',
        'playwright.sync_api',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='ScubedTimesheet',
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
    icon=None,
)

if sys.platform == 'darwin':
    app = BUNDLE(
        exe,
        name='ScubedTimesheet.app',
        icon=None,
        bundle_identifier='co.za.datacentrix.scubed-timesheet',
    )
