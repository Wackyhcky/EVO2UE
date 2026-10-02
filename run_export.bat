@echo off
rem ---- edit these three lines ----
set EVO_CONTENT=K:\SteamLibrary\steamapps\common\Assetto Corsa EVO\content
set TRACK=laguna_seca
set OUT=C:\EVO_export\%TRACK%
rem --------------------------------
python -c "import numpy, PIL" 2>nul || pip install numpy pillow
python "%~dp0evo_track_export.py" "%EVO_CONTENT%" %TRACK% "%OUT%" --lods --preview
pause
