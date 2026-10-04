@echo off
cd /d "%~dp0"
python -m PyInstaller --noconfirm --clean "SCAD-Sammler.spec"
if errorlevel 1 exit /b %errorlevel%
echo Fertig: dist\SCAD Generator Collector\SCAD Generator Collector.exe
