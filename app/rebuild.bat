@echo off
cd /d D:\sports_data\yt-dlp-gui\app
if exist dist\tool-download-movie.exe taskkill /f /im tool-download-movie.exe 2>nul
if exist dist rmdir /s /q dist
if exist build rmdir /s /q build
if exist tool-download-movie.spec del /f tool-download-movie.spec
pyinstaller --name=tool-download-movie --onefile --windowed --icon=assets/yt-dlp-gui.ico --add-data=assets;assets --add-data=root;root --hidden-import=httpx --hidden-import=anyio --hidden-import=charset_normalizer --hidden-import=certifi --hidden-import=platformdirs --hidden-import=httpx_sse --collect-all=qtawesome --noupx app.py --distpath=dist --noconfirm

if exist dist\tool-download-movie.exe (
    setlocal enabledelayedexpansion
    set "RELEASE_DIR=D:\sports_data\yt-dlp-gui\app\release\tool-download-movie-pro-v1.0.0"
    if not exist "!RELEASE_DIR!" mkdir "!RELEASE_DIR!"
    copy /y dist\tool-download-movie.exe "!RELEASE_DIR!\tool-download-movie.exe"
    echo Build completed. Copied to !RELEASE_DIR!
    endlocal
) else (
    echo Build FAILED. No exe found.
    exit /b 1
)
