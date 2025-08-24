@echo off
REM Build script for SiteQARunner
python -m PyInstaller --noconfirm --clean --onedir --windowed ^
  --name "SiteQARunner" ^
  --add-data "proxies.txt;." ^
  --collect-all selenium ^
  --collect-all undetected_chromedriver ^
  --collect-all certifi ^
  --collect-all urllib3 ^
  main.py
echo.
echo Build complete! Find exe in dist\SiteQARunner\SiteQARunner.exe
pause
