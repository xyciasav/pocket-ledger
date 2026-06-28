@echo off
py -m pip install -r requirements.txt
py -m PyInstaller --noconfirm --clean --windowed --hidden-import pypdf --name "Pocket Ledger" app.py
echo.
echo Build complete: dist\Pocket Ledger\Pocket Ledger.exe
pause
