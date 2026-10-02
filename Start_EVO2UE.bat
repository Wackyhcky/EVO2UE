@echo off
rem Launches the EVO2UE app (installs numpy + pillow the first time if needed).
cd /d "%~dp0"
where python >nul 2>nul || (echo Python 3 is not installed. Get it from https://www.python.org/downloads/ ^(tick "Add to PATH"^) & pause & exit /b 1)
python -c "import numpy, PIL, tkinter" 2>nul || python -m pip install --user numpy pillow
start "" pythonw "%~dp0evo2ue_gui.py"
