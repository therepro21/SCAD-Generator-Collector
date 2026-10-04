# -*- mode: python ; coding: utf-8 -*-
a = Analysis(['scad_sammler.py'], pathex=[], binaries=[], datas=[('ui-template.html','.'),('desktop.js','.'),('desktop.css','.'),('assets','assets')], hiddenimports=['PIL._tkinter_finder','webview.platforms.edgechromium','webview.platforms.winforms','pystray._win32'], hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[], noarchive=False, optimize=0)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='SCAD Generator Collector', icon='assets/logo.ico', debug=False, bootloader_ignore_signals=False, strip=False, upx=True, console=False, disable_windowed_traceback=False, contents_directory='Programm')
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=True, name='SCAD Generator Collector')
