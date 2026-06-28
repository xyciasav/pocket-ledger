@echo off
setlocal

where py >nul 2>nul
if errorlevel 1 (
  echo Python launcher "py" was not found.
  echo Install Python for Windows from python.org and make sure "py launcher" is selected.
  pause
  exit /b 1
)

py -c "import tkinter" >nul 2>nul
if errorlevel 1 (
  echo This Python install does not include Tkinter/Tcl-Tk.
  echo Pocket Ledger needs Tkinter. Install the standard Windows Python from python.org,
  echo or modify this script to point at a Python that can run: py -c "import tkinter"
  pause
  exit /b 1
)

for /f "delims=" %%i in ('py -c "import sys; print(sys.base_prefix)"') do set "PYBASE=%%i"

py -m pip install -r requirements.txt
py -m PyInstaller --noconfirm --clean --windowed --onefile ^
  --hidden-import pypdf ^
  --hidden-import tkinter ^
  --runtime-hook pyi_rth_tkinter_local.py ^
  --add-binary "%PYBASE%\DLLs\_tkinter.pyd;." ^
  --add-binary "%PYBASE%\DLLs\tcl86t.dll;." ^
  --add-binary "%PYBASE%\DLLs\tk86t.dll;." ^
  --add-data "%PYBASE%\Lib\tkinter;tkinter" ^
  --add-data "%PYBASE%\tcl;tcl" ^
  --name "Pocket Ledger" ^
  --distpath dist_release ^
  --workpath build_release ^
  app.py
echo.
echo Build complete: dist_release\Pocket Ledger.exe
pause
