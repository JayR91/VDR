# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    # Unpacked Chromium/Firefox extension, so the frozen build can refresh
    # the stable copy under ~/Library/Application Support/VDR on launch (see
    # extension_install.stage_unpacked). VDR-windows.spec bundles the same
    # tree; the DMG build also puts a copy in the disk image.
    datas=[
        ("browser_extension", "browser_extension"),
    ],
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
    [],
    exclude_binaries=True,
    name='VDR',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    # Receives web links dropped on the Dock icon in the frozen macOS app.
    argv_emulation=True,
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
    name='VDR',
)
app = BUNDLE(
    coll,
    name='VDR.app',
    icon='AppIcon.icns',
    bundle_identifier='com.vdr.app',
    info_plist={
        'CFBundleDisplayName': 'VDR',
        # Deliberately NOT LSUIElement: VDR keeps a normal Dock icon. Closing
        # the window only hides it (see gui.py's WM_DELETE_WINDOW), so
        # downloads and the local server keep running in the background and
        # clicking the Dock icon brings the window back. The Dock tile is also
        # what carries the download-percentage badge -- that only renders on a
        # visible tile.
        'CFBundleURLTypes': [
            {
                'CFBundleURLName': 'Download URL',
                'CFBundleURLSchemes': ['http', 'https'],
            },
            {
                # Lets the browser extension launch the app when it isn't
                # running, the same way zoommtg:// or slack:// do -- the
                # extension navigates to vdr://launch and macOS starts
                # (or foregrounds) this app in response. This scheme must stay
                # in sync with APP_LAUNCH_URL in browser_extension/background.js
                # and launchAppViaLink() in content.js.
                'CFBundleURLName': 'VDR Launch',
                'CFBundleURLSchemes': ['vdr'],
            },
        ],
    },
)
