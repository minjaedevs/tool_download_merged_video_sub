@echo off
cd /d D:\sports_data\yt-dlp-gui\app
if exist dist\yt-dlp-gui.exe taskkill /f /im yt-dlp-gui.exe 2>nul
if exist dist rmdir /s /q dist
if exist build rmdir /s /q build
if exist yt-dlp-gui.spec del /f yt-dlp-gui.spec
pyinstaller --name=yt-dlp-gui --onefile --windowed --icon=assets/yt-dlp-gui.ico --add-data=assets;assets --add-data=root;root --hidden-import=httpx --hidden-import=anyio --hidden-import=charset_normalizer --hidden-import=certifi --hidden-import=platformdirs --hidden-import=httpx_sse --collect-all=qtawesome --noupx app.py --distpath=dist --noconfirm

if exist dist\yt-dlp-gui.exe (
    setlocal enabledelayedexpansion
    set "RELEASE_DIR=D:\sports_data\yt-dlp-gui\app\release\yt-dlp-gui-v1.0.0"
    if not exist "!RELEASE_DIR!" mkdir "!RELEASE_DIR!"
    copy /y dist\yt-dlp-gui.exe "!RELEASE_DIR!\yt-dlp-gui.exe"
    echo Build completed. Copied to !RELEASE_DIR!
    endlocal
) else (
    echo Build FAILED. No exe found.
    exit /b 1
)
