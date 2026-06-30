@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "PYTHON=C:\Users\Pc\AppData\Local\Python\pythoncore-3.14-64\python.exe"
set "APP_NAME=tool-download-movie"
set "RELEASE_ROOT=%CD%\release"
set "CACHE_DIR=%TEMP%\%APP_NAME%-release-cache"

if not exist "%PYTHON%" (
    echo Build FAILED. Python not found:
    echo   %PYTHON%
    exit /b 1
)

if not exist "assets\mp42hls.exe" (
    echo Build FAILED. Missing Bento4 mp42hls:
    echo   %CD%\assets\mp42hls.exe
    exit /b 1
)

if not exist "assets\yt-dlp-gui.ico" (
    echo Build FAILED. Missing icon:
    echo   %CD%\assets\yt-dlp-gui.ico
    exit /b 1
)

if exist "%CACHE_DIR%" rmdir /s /q "%CACHE_DIR%"
mkdir "%CACHE_DIR%" >nul 2>nul
if exist "dist\ffmpeg.exe" copy /y "dist\ffmpeg.exe" "%CACHE_DIR%\ffmpeg.exe" >nul
if exist "dist\ffprobe.exe" copy /y "dist\ffprobe.exe" "%CACHE_DIR%\ffprobe.exe" >nul
if exist "dist\fonts" xcopy /s /i /y "dist\fonts" "%CACHE_DIR%\fonts" >nul

taskkill /f /im "%APP_NAME%.exe" 2>nul
if exist dist rmdir /s /q dist
if exist build rmdir /s /q build

%PYTHON% -m PyInstaller tool-download-movie.spec --clean --noconfirm

if exist "dist\%APP_NAME%.exe" (
    for /f %%v in ('%PYTHON% -c "from _version import __version__; print(__version__)"') do set "VERSION=%%v"
    set "RELEASE_DIR=%RELEASE_ROOT%\tool-download-movie-pro-v!VERSION!"
    if exist "!RELEASE_DIR!\ffmpeg.exe" copy /y "!RELEASE_DIR!\ffmpeg.exe" "%CACHE_DIR%\ffmpeg.exe" >nul
    if exist "!RELEASE_DIR!\ffprobe.exe" copy /y "!RELEASE_DIR!\ffprobe.exe" "%CACHE_DIR%\ffprobe.exe" >nul
    if exist "!RELEASE_DIR!\fonts" xcopy /s /i /y "!RELEASE_DIR!\fonts" "%CACHE_DIR%\fonts" >nul
    if exist "!RELEASE_DIR!" rmdir /s /q "!RELEASE_DIR!"
    if not exist "!RELEASE_DIR!" mkdir "!RELEASE_DIR!"
    copy /y "dist\%APP_NAME%.exe" "!RELEASE_DIR!\%APP_NAME%.exe" >nul

    :: mp42hls.exe is also bundled inside the one-file exe by tool-download-movie.spec.
    :: Keep a visible copy in release\assets so the package is self-documenting
    :: and easy to verify before distribution.
    mkdir "!RELEASE_DIR!\assets" >nul 2>nul
    copy /y "assets\mp42hls.exe" "!RELEASE_DIR!\assets\mp42hls.exe" >nul
    copy /y "assets\yt-dlp-gui.ico" "!RELEASE_DIR!\assets\yt-dlp-gui.ico" >nul

    :: Restore optional runtime tools for users who run features that need ffmpeg.
    if exist "%CACHE_DIR%\ffmpeg.exe" copy /y "%CACHE_DIR%\ffmpeg.exe" "!RELEASE_DIR!\ffmpeg.exe" >nul
    if exist "%CACHE_DIR%\ffprobe.exe" copy /y "%CACHE_DIR%\ffprobe.exe" "!RELEASE_DIR!\ffprobe.exe" >nul
    if exist "%CACHE_DIR%\fonts" xcopy /s /i /y "%CACHE_DIR%\fonts" "!RELEASE_DIR!\fonts" >nul

    if not exist "!RELEASE_DIR!\ffmpeg.exe" (
        for /f "delims=" %%p in ('where ffmpeg.exe 2^>nul') do (
            if not exist "!RELEASE_DIR!\ffmpeg.exe" copy /y "%%p" "!RELEASE_DIR!\ffmpeg.exe" >nul
        )
    )
    if not exist "!RELEASE_DIR!\ffprobe.exe" (
        for /f "delims=" %%p in ('where ffprobe.exe 2^>nul') do (
            if not exist "!RELEASE_DIR!\ffprobe.exe" copy /y "%%p" "!RELEASE_DIR!\ffprobe.exe" >nul
        )
    )

    > "!RELEASE_DIR!\README_DISTRIBUTION.txt" echo Tool Download Movie v!VERSION!
    >> "!RELEASE_DIR!\README_DISTRIBUTION.txt" echo.
    >> "!RELEASE_DIR!\README_DISTRIBUTION.txt" echo Run: %APP_NAME%.exe
    >> "!RELEASE_DIR!\README_DISTRIBUTION.txt" echo.
    >> "!RELEASE_DIR!\README_DISTRIBUTION.txt" echo Included:
    >> "!RELEASE_DIR!\README_DISTRIBUTION.txt" echo - %APP_NAME%.exe
    >> "!RELEASE_DIR!\README_DISTRIBUTION.txt" echo - assets\mp42hls.exe for MP4 to M3U8/Bento4 verification
    >> "!RELEASE_DIR!\README_DISTRIBUTION.txt" echo - assets\yt-dlp-gui.ico
    >> "!RELEASE_DIR!\README_DISTRIBUTION.txt" echo - ffmpeg.exe and ffprobe.exe if they were found during build
    >> "!RELEASE_DIR!\README_DISTRIBUTION.txt" echo.
    >> "!RELEASE_DIR!\README_DISTRIBUTION.txt" echo Local user settings are stored by Windows under the app data folder,
    >> "!RELEASE_DIR!\README_DISTRIBUTION.txt" echo for example: %%LOCALAPPDATA%%\Tool Download Movie

    powershell -NoProfile -ExecutionPolicy Bypass -Command "Compress-Archive -Path '!RELEASE_DIR!\*' -DestinationPath '!RELEASE_DIR!.zip' -Force"

    echo === Contents of dist ===
    dir /b "dist"
    echo.
    echo === Contents of release ===
    dir /b "!RELEASE_DIR!"
    echo Build completed. Copied to !RELEASE_DIR!
) else (
    echo Build FAILED. No exe found.
    exit /b 1
)

if exist "%CACHE_DIR%" rmdir /s /q "%CACHE_DIR%"
endlocal
