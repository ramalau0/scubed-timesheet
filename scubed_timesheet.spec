# PyInstaller spec — used by GitHub Actions to build per-platform binaries.
# Build locally: pyinstaller scubed_timesheet.spec

import sys
from PyInstaller.building.build_main import Analysis, PYZ, EXE, BUNDLE, COLLECT

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
        '_playwright',
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

# Pull in everything from the playwright package
from PyInstaller.utils.hooks import collect_all
playwright_datas, playwright_binaries, playwright_hiddenimports = collect_all('playwright')
a.datas    += playwright_datas
a.binaries += playwright_binaries
a.hiddenimports += playwright_hiddenimports

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
    console=False,          # no terminal window on Windows/Mac
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

# Mac: wrap in a .app bundle
if sys.platform == 'darwin':
    app = BUNDLE(
        exe,
        name='ScubedTimesheet.app',
        icon=None,
        bundle_identifier='co.za.datacentrix.scubed-timesheet',
    )
