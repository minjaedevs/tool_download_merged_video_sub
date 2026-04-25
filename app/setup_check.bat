@echo off
chcp 65001 >nul 2>&1
title Tool Download Movie Pro - Setup

echo ============================================
echo   Tool Download Movie Pro - Setup ^& Install
echo ============================================
echo.

:: ============================================
:: Auto-elevate to Admin if needed
:: ============================================
net session >nul 2>&1
if not %errorLevel%==0 (
    echo [!] Can quyen Admin de cai dat. Dang khoi dong lai...
    timeout /t 2 /nobreak >nul
    powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
echo [OK] Dang chay voi quyen Admin
echo.

:: ============================================
:: [1/2] Check & Auto-Install FFmpeg
:: ============================================
echo [1/2] Kiem tra FFmpeg...
ffmpeg -version >nul 2>&1
if %errorLevel%==0 (
    for /f "tokens=*" %%v in ('ffmpeg -version 2^>nul ^| findstr /B "ffmpeg version"') do echo     %%v
    echo [OK] FFmpeg da san sang
    goto :check_exe
)

echo [!] FFmpeg chua duoc cai dat. Dang tu dong cai...
echo.

:: --- Thu winget truoc (nhanh nhat) ---
winget --version >nul 2>&1
if %errorLevel%==0 (
    echo     [winget] Dang cai FFmpeg...
    winget install --id Gyan.FFmpeg -e --silent --accept-package-agreements --accept-source-agreements
    if %errorLevel%==0 (
        echo [OK] FFmpeg da cai xong qua winget
        call :refresh_path
        goto :check_exe
    )
    echo     [!] winget that bai. Chuyen sang tai thu cong...
    echo.
)

:: --- Tai thu cong qua PowerShell ---
echo     Dang tai FFmpeg tu GitHub (co the mat vai phut)...
set "FFMPEG_ZIP=%TEMP%\ffmpeg_setup.zip"
set "FFMPEG_EXTRACT=%TEMP%\ffmpeg_extract_tmp"
set "FFMPEG_DEST=C:\ffmpeg"

powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip' -OutFile '%FFMPEG_ZIP%' -UseBasicParsing"

if not exist "%FFMPEG_ZIP%" (
    echo [!] Tai that bai! Kiem tra ket noi internet.
    echo     Tai thu cong: https://github.com/BtbN/FFmpeg-Builds/releases
    pause
    goto :check_exe
)

echo     Dang giai nen vao %FFMPEG_DEST%\bin ...
if exist "%FFMPEG_EXTRACT%" rd /s /q "%FFMPEG_EXTRACT%"
if exist "%FFMPEG_DEST%"    rd /s /q "%FFMPEG_DEST%"

powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -Path '%FFMPEG_ZIP%' -DestinationPath '%FFMPEG_EXTRACT%' -Force; $bin = (Get-ChildItem '%FFMPEG_EXTRACT%' -Recurse -Filter 'ffmpeg.exe' | Select-Object -First 1).DirectoryName; if ($bin) { New-Item -ItemType Directory '%FFMPEG_DEST%\bin' -Force | Out-Null; Copy-Item \"$bin\*\" '%FFMPEG_DEST%\bin\' -Force; Write-Host 'Copy OK' } else { Write-Host 'COPY_FAIL' }"

:: Xoa file tam
if exist "%FFMPEG_ZIP%"     del /q "%FFMPEG_ZIP%"
if exist "%FFMPEG_EXTRACT%" rd /s /q "%FFMPEG_EXTRACT%"

if not exist "%FFMPEG_DEST%\bin\ffmpeg.exe" (
    echo [!] Cai dat FFmpeg that bai! Vui long cai thu cong.
    echo     Tai: https://github.com/BtbN/FFmpeg-Builds/releases
    pause
    goto :check_exe
)

:: Them C:\ffmpeg\bin vao System PATH
powershell -NoProfile -Command "$p=[Environment]::GetEnvironmentVariable('PATH','Machine'); if ($p -notlike '*%FFMPEG_DEST%\bin*') { [Environment]::SetEnvironmentVariable('PATH',$p+';%FFMPEG_DEST%\bin','Machine'); Write-Host 'PATH updated' }"

call :refresh_path
echo [OK] FFmpeg da duoc cai tai %FFMPEG_DEST%
echo.

:: ============================================
:: [2/2] Kiem tra & Chay EXE
:: ============================================
:check_exe
echo [2/2] Kiem tra ung dung...
set "APP_EXE=%~dp0yt-dlp-gui.exe"

if not exist "%APP_EXE%" (
    echo [!] Khong tim thay yt-dlp-gui.exe!
    echo     Dam bao yt-dlp-gui.exe nam cung thu muc voi file nay.
    pause
    exit /b 1
)

for %%A in ("%APP_EXE%") do echo     File: %%~nxA ^(%%~zA bytes^)
echo [OK] Ung dung san sang
echo.

echo ============================================
echo   Hoan tat! Dang khoi dong ung dung...
echo ============================================
timeout /t 2 /nobreak >nul
start "" "%APP_EXE%"
exit /b 0

:: ============================================
:: Subroutine: Refresh PATH tu registry
:: ============================================
:refresh_path
for /f "tokens=2*" %%a in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path 2^>nul') do set "PATH=%%b;%PATH%"
exit /b 0
