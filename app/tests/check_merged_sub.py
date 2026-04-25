import subprocess as sp, sys, json, tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FFPROBE = r"C:\ffmpeg\ffmpeg-2025-06-08-git-5fea5e3e11-full_build\bin\ffprobe.exe"
FFMPEG = r"C:\ffmpeg\ffmpeg-2025-06-08-git-5fea5e3e11-full_build\bin\ffmpeg.exe"
VIDEO = r"D:\B1\oQEYEw7faK9I1wqNOp4peQFP1lwD13qAfBp5hC.mkv"
TMP = Path(tempfile.gettempdir())

# Dump subtitle using ffmpeg to get raw content
print("=== Dumping subtitle ===")
srt_out = TMP / "sub_merged.srt"
cp = sp.run([FFMPEG, "-y", "-i", VIDEO,
               "-map", "0:s", "-f", "srt", str(srt_out)],
            capture_output=True, text=True)
if cp.returncode != 0:
    print(f"Failed: {cp.stderr[-100:]}")
else:
    content = srt_out.read_text(encoding="utf-8", errors="replace")
    srt_out.unlink()
    print(f"Content ({len(content)} chars):")
    print(content)

# Try to extract subtitle using ffprobe -show_frames for full data
print("\n=== Full subtitle frames ===")
cp2 = sp.run([FFPROBE, "-v", "quiet", "-show_frames",
                "-select_streams", "s", "-of", "json", VIDEO],
             capture_output=True, text=True)
if cp2.returncode == 0:
    data = json.loads(cp2.stdout)
    frames = data.get("frames", [])
    print(f"Total subtitle frames: {len(frames)}")
    for f in frames:
        pts = f.get("pkt_pts_time", "?")
        data2 = f.get("pkt_data", "")
        print(f"  [{pts}] {repr(data2[:300])}")

# Check ALL metadata tags including stream-level
print("\n=== All format tags ===")
cp3 = sp.run([FFPROBE, "-v", "quiet", "-show_format", "-of", "json", VIDEO],
             capture_output=True, text=True)
fmt = json.loads(cp3.stdout)["format"]
for k, v in fmt.get("tags", {}).items():
    print(f"  {k}: {v}")

# Check video stream tags for title/description
print("\n=== Video stream tags ===")
cp4 = sp.run([FFPROBE, "-v", "quiet", "-show_streams",
                "-select_streams", "v", "-of", "json", VIDEO],
             capture_output=True, text=True)
if cp4.returncode == 0:
    vs = json.loads(cp4.stdout)["streams"][0]
    for k, v in vs.get("tags", {}).items():
        print(f"  {k}: {v}")
    # Check side data
    sd = vs.get("side_data_list", [])
    if sd:
        for sdd in sd:
            print(f"  side_data: {sdd}")

# Search for filename anywhere in the container
print("\n=== Searching for 'oQEYEw' in container ===")
import struct, zlib

# Read first 1MB of file and search
data_bytes = VIDEO.read_bytes()[:1024*1024]
pos = data_bytes.find(b"oQEYEw")
if pos >= 0:
    print(f"  FOUND at byte offset {pos}")
    print(f"  Context: {data_bytes[max(0,pos-20):pos+60]}")
else:
    print("  NOT found in first 1MB")

# Search subtitle data
pos2 = data_bytes.find(b"oQEYEw".decode("latin1").encode())
# Try UTF-8 search
for enc in ["utf-8", "latin1", "utf-16-le", "utf-16-be"]:
    try:
        decoded = data_bytes.decode(enc, errors="replace")
        if "oQEYEw" in decoded:
            idx = decoded.find("oQEYEw")
            print(f"  FOUND in {enc} at char offset {idx}")
            print(f"  Context: ...{decoded[max(0,idx-30):idx+60]}...")
            break
    except:
        pass

print("\n=== DONE ===")
