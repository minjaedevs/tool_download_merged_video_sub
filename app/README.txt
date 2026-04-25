============================================
   TOOL DOWNLOAD MOVIE PRO
   Huong dan su dung
============================================

YEU CAU HE THONG:
- Windows 10/11
- FFmpeg (neu chua co, xem huong dan ben duoi)

============================================
   CACH 1: Su dung file EXE (khuyen nghi)
============================================

1. Chay file: yt-dlp-gui.exe
   - Neu co loi FFmpeg, chay setup_check.bat de kiem tra

2. Hoan tat!


============================================
   HUONG DAN CAI DAT FFMPEG (neu can)
============================================

1. Tai FFmpeg tai:
   https://github.com/BtbN/FFmpeg-Builds/releases

2. Chon: ffmpeg-master-latest-win64-gpl.zip

3. Giai nen vao thu muc (VD: C:\ffmpeg)

4. Them vao PATH:
   - Mo System Properties > Environment Variables
   - Trong System Variables > Path > Edit
   - Them: C:\ffmpeg\bin
   - OK > OK > OK

5. Khoi dong lai may

6. Chay setup_check.bat de xac nhan FFmpeg da cai dat


============================================
   SU DUNG PHAN MEM
============================================

Tab chinh:
- Nhap URL video vao o "Video URL(s)"
- Chon preset (mp4, mkv, webm...)
- Nhap link phu de (neu co)
- Nhap duong dan luu file
- Nhan "Download"

Tab NewSite (trang phim):
1. Nhap Movie ID vao o "Movie ID"
2. Nhan "Fetch Data" de tai thong tin
3. Danh sach phim hien ra, tick chon tap muon tai
4. Dat tuy chon:
   - Tich "Tai phu de" neu can phu de
   - Tich "Auto Merged Sub" bat buoc khi tai phu de
   - Tich "Hardcode sub" de burn phu de vao video
5. Nhan "Start Download & Merge"


============================================
   HUONG DAN CAU HINH (neu can)
============================================

File cau hinh: config.toml

[PRESETS]
- mp4:  Tuong thich tot nhat (mac dinh)
- mkv:  Chat luong cao hon
- webm: Dung cho web

[DOWNLOAD]
- concurrent: So luong tai cung luc (mac dinh: 3)
- save_dir: Thu muc luu mac dinh

============================================
   GIAI QUYET SU CO
============================================

Q: "FFmpeg not found"
A: Cai dat FFmpeg theo huong dan ben tren

Q: "Access denied" khi mo file
A: Chay voi quyen Admin (chuot phai > Run as administrator)

Q: Video tai nhung khong ghep phu de
A: Khi tai phu de, phai tick ca "Auto Merged Sub" va "Hardcode sub"

============================================
   THONG TIN PHIEN BAN
============================================

Version: 1.0
Ngay cap nhat: 2026-04-24

============================================
