@echo off
cd /d D:\sports_data\yt-dlp-gui\app
if exist dist\yt-dlp-gui.exe taskkill /f /im yt-dlp-gui.exe 2>nul
if exist dist rmdir /s /q dist
if exist build rmdir /s /q build
if exist yt-dlp-gui.spec del /f yt-dlp-gui.spec
pyinstaller --name=yt-dlp-gui --onefile --windowed --icon=assets/yt-dlp-gui.ico --add-data=assets;assets --hidden-import=httpx --hidden-import=anyio --hidden-import=charset_normalizer --hidden-import=certifi --hidden-import=platformdirs --hidden-import=httpx_sse app.py --distpath=dist --noconfirm
