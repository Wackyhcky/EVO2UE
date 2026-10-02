@echo off
rem Builds a standalone EVO2UE.exe (no Python needed on the machine that runs it).
cd /d "%~dp0"
python -m pip install --upgrade pyinstaller numpy pillow || goto :err
python -m PyInstaller --noconfirm --clean --onefile --windowed --name EVO2UE ^
  --add-data "evo_ue_import.py;." evo2ue_gui.py || goto :err
echo.
echo Done: dist\EVO2UE.exe
pause
exit /b 0
:err
echo Build failed.
pause
exit /b 1
