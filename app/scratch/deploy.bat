@echo off
chcp 65001 >nul 2>&1
title Tool Download Movie Pro - Deploy

echo ============================================
echo   Tool Download Movie Pro - Build and Deploy
echo ============================================
echo.

:: Check admin rights
net session >nul 2>&1
if %errorLevel%==0 (
    echo [OK] Dang chay voi quyen Admin
) else (
    echo [!] Nen chay voi quyen Admin de cai dat tot nhat
)
echo.

echo [1/4] Kiem tra FFmpeg...
ffmpeg -version >nul 2>&1
if %errorLevel%==0 (
    for /f "tokens=3" %%v in ('ffmpeg -version 2^>nul ^| find "ffmpeg version"') do echo     FFmpeg: %%v
    echo [OK] FFmpeg da duoc cai dat
) else (
    echo [!] FFmpeg chua duoc cai dat!
    echo.
    echo     Huong dan cai dat FFmpeg:
    echo     1. Tai FFmpeg: https://github.com/BtbN/FFmpeg-Builds/releases
    echo     2. Chon: ffmpeg-master-latest-win64-gpl.zip
    echo     3. Giai nen vao thu muc (VD: C:\ffmpeg)
    echo     4. Them PATH: C:\ffmpeg\bin
    echo.
)
echo.

echo [2/4] Kiem tra Python...
python --version >nul 2>&1
if %errorLevel%==0 (
    for /f "tokens=*" %%v in ('python --version 2^>nul') do echo     Python: %%v
    echo [OK] Python da duoc cai dat
) else (
    echo [!] Python chua duoc cai dat!
    echo     Tai Python: https://www.python.org/downloads/
    echo     Can Python 3.10+ de chay source code.
    echo.
    echo     Neu chi can chay EXE, bo qua buoc nay.
)
echo.

echo [3/4] Build EXE...
if not exist "rebuild.bat" (
    echo [!] Khong tim thay rebuild.bat!
    echo     Vui long tao file rebuild.bat trong thu muc app.
    goto :build_skip
)

echo     Dang goi rebuild.bat...
call rebuild.bat >nul 2>&1

:build_skip
echo.

echo [4/4] Kiem tra ket qua...
if exist "dist\yt-dlp-gui.exe" (
    for %%A in (dist\yt-dlp-gui.exe) do echo     File: %%~nxA
    for %%A in (dist\yt-dlp-gui.exe) do echo     Kich thuoc: %%~zA bytes
    echo [OK] File EXE da san sang
) else (
    echo [!] Build that bai! Khong tim thay file EXE.
    echo     Vui long kiem tra loi build.
)
echo.

echo ============================================
echo   Hoan tat!
echo ============================================
echo.
echo De su dung:
echo   - User: Chay dist\yt-dlp-gui.exe
echo   - Developer: Chay python app.py
echo.
echo.

echo Dang mo ung dung...
timeout /t 2 /nobreak >nul 2>&1
if exist "dist\yt-dlp-gui.exe" (
    start "" "dist\yt-dlp-gui.exe"
    echo Da mo ung dung!
) else (
    echo [!] Khong the mo! File khong ton tai.
    pause
)
