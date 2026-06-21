@echo off
cd /d D:\sports_data\yt-dlp-gui\app

set "PYTHON=C:\Users\Pc\AppData\Local\Python\pythoncore-3.14-64\python.exe"

if exist dist\tool-download-movie.exe taskkill /f /im tool-download-movie.exe 2>nul
if exist dist rmdir /s /q dist
if exist build rmdir /s /q build

%PYTHON% -m PyInstaller tool-download-movie.spec --clean --noconfirm

if exist dist\tool-download-movie.exe (
    for /f %%v in ('%PYTHON% -c "from _version import __version__; print(__version__)"') do set "VERSION=%%v"
    setlocal enabledelayedexpansion
    set "RELEASE_DIR=D:\sports_data\yt-dlp-gui\app\release\tool-download-movie-pro-v!VERSION!"
    if not exist "!RELEASE_DIR!" mkdir "!RELEASE_DIR!"
    copy /y dist\tool-download-movie.exe "!RELEASE_DIR!\tool-download-movie.exe"

    :: Copy ffmpeg, ffprobe, fonts vao dist de san san dong goi
    copy /y "!RELEASE_DIR!\ffmpeg.exe" "dist\ffmpeg.exe" 2>nul
    copy /y "!RELEASE_DIR!\ffprobe.exe" "dist\ffprobe.exe" 2>nul
    if exist "!RELEASE_DIR!\fonts" (
        if not exist "dist\fonts" mkdir "dist\fonts"
        xcopy /s /y "!RELEASE_DIR!\fonts" "dist\fonts\" >nul
    )

    echo === Contents of dist ===
    dir /b dist
    echo Build completed. Copied to !RELEASE_DIR!
    endlocal
) else (
    echo Build FAILED. No exe found.
    exit /b 1
)
