"""
Benchmark: đo thời gian encode và CPU usage với các cấu hình khác nhau.

Chạy từ thư mục app/:
    python tests/benchmark_encode.py

Yêu cầu:
    - ffmpeg trong PATH hoặc trong thư mục hiện tại
    - pip install psutil  (nếu muốn đo CPU%)

Output: bảng so sánh thời gian và CPU trung bình.
"""
from __future__ import annotations

import os
import shutil
import subprocess as sp
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

# ── Optional psutil for CPU sampling ─────────────────────────────────────────
try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

# ── Locate ffmpeg ─────────────────────────────────────────────────────────────
def _find_ffmpeg() -> str:
    for name in ("ffmpeg", "ffmpeg.exe"):
        candidate = Path(sys.executable).parent / name
        if candidate.exists():
            return str(candidate)
    found = shutil.which("ffmpeg")
    if found:
        return found
    raise RuntimeError("Không tìm thấy ffmpeg. Thêm vào PATH hoặc đặt cạnh script.")

FFMPEG = _find_ffmpeg()
CFLAGS = {"creationflags": sp.CREATE_NO_WINDOW} if sys.platform == "win32" else {}

# ── Test video / subtitle generation ─────────────────────────────────────────
def _create_test_assets(tmp_dir: Path, duration_s: int = 30) -> tuple[Path, Path]:
    """
    Tạo video test (30s, 1280x720, 30fps) và subtitle .srt đơn giản bằng ffmpeg lavfi.
    Trả về (video_path, sub_path).
    """
    video = tmp_dir / "test_input.mp4"
    sub   = tmp_dir / "test_sub.srt"

    print(f"[setup] Tạo video test {duration_s}s @ 1280x720 30fps...")
    sp.run([
        FFMPEG, "-y",
        "-f", "lavfi",
        "-i", f"testsrc=duration={duration_s}:size=1280x720:rate=30",
        "-f", "lavfi",
        "-i", f"sine=frequency=440:duration={duration_s}",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
        "-c:a", "aac", "-b:a", "128k",
        str(video),
    ], capture_output=True, check=True, **CFLAGS)

    # Subtitle đơn giản: 1 dòng mỗi 3 giây
    srt_lines = []
    for i in range(duration_s // 3):
        start = i * 3
        end   = start + 2
        s_fmt = f"00:00:{start:02d},000"
        e_fmt = f"00:00:{end:02d},000"
        srt_lines.append(f"{i+1}\n{s_fmt} --> {e_fmt}\nBenchmark subtitle line {i+1}\n")
    sub.write_text("\n".join(srt_lines), encoding="utf-8")

    print(f"[setup] Video: {video.stat().st_size // 1024} KiB  Sub: {sub.stat().st_size} bytes")
    return video, sub

# ── CPU sampler ───────────────────────────────────────────────────────────────
class CpuSampler:
    """Sample system CPU% in a background thread while a process runs."""

    def __init__(self, interval: float = 0.5):
        self.interval = interval
        self._samples: list[float] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self) -> list[float]:
        self._stop.set()
        self._thread.join(timeout=2)
        return self._samples

    def _run(self):
        if HAS_PSUTIL:
            while not self._stop.is_set():
                self._samples.append(psutil.cpu_percent(interval=None))
                time.sleep(self.interval)
        # Without psutil: no-op

# ── Single encode run ─────────────────────────────────────────────────────────
def run_encode(
    label: str,
    video: Path,
    sub: Path,
    out: Path,
    encoder: str,
    encoder_params: list[str],
    threads: Optional[int],
) -> dict:
    """Run one ffmpeg encode; return timing + CPU stats."""
    # Escape subtitle path for ffmpeg filter
    sub_str = str(sub).replace("\\", "/").replace(":", "\\:")
    vf = f"subtitles='{sub_str}'"

    cmd = [FFMPEG, "-y"]
    if threads:
        cmd += ["-threads", str(threads)]
    cmd += [
        "-i", str(video),
        "-vf", vf,
        "-c:v", encoder,
        *encoder_params,
        "-c:a", "copy",
        "-loglevel", "warning",
        str(out),
    ]

    sampler = CpuSampler()
    sampler.start()
    t0 = time.perf_counter()

    result = sp.run(cmd, capture_output=True, **CFLAGS)

    elapsed = time.perf_counter() - t0
    samples = sampler.stop()

    ok = result.returncode == 0 and out.exists()
    size_kb = out.stat().st_size // 1024 if ok else 0
    avg_cpu = sum(samples) / len(samples) if samples else float("nan")
    max_cpu = max(samples) if samples else float("nan")

    return {
        "label":    label,
        "ok":       ok,
        "time_s":   elapsed,
        "size_kb":  size_kb,
        "avg_cpu":  avg_cpu,
        "max_cpu":  max_cpu,
        "error":    result.stderr[-300:] if not ok else "",
    }

# ── GPU encoder detection (mirrors workers.py logic) ─────────────────────────
_GPU_CANDIDATES = [
    ("h264_nvenc", "nvenc", lambda crf: ["-preset", "p4", "-rc", "vbr", "-cq", str(crf)]),
    ("h264_amf",   "amf",   lambda crf: ["-quality", "balanced", "-qp_i", str(crf)]),
    ("h264_qsv",   "qsv",   lambda crf: ["-preset", "fast", "-global_quality", str(crf)]),
]

def detect_gpu_encoder() -> Optional[tuple[str, list[str]]]:
    """Return (encoder_name, quality_params) or None if no GPU encoder available."""
    try:
        enc_list = sp.run(
            [FFMPEG, "-encoders", "-v", "quiet"],
            capture_output=True, text=True, timeout=8, **CFLAGS,
        )
        for enc_name, keyword, param_fn in _GPU_CANDIDATES:
            if keyword not in enc_list.stdout:
                continue
            probe = sp.run(
                [FFMPEG,
                 "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.04:r=1",
                 "-c:v", enc_name, "-frames:v", "1",
                 "-f", "null", "-"],
                capture_output=True, timeout=10, **CFLAGS,
            )
            if probe.returncode == 0:
                return enc_name, param_fn(22)
    except Exception:
        pass
    return None

# ── Main benchmark ────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("  BENCHMARK ENCODE — XemShort merge configurations")
    print(f"  ffmpeg : {FFMPEG}")
    print(f"  psutil : {'yes' if HAS_PSUTIL else 'no (pip install psutil để đo CPU%)'}")
    cpu_cores = os.cpu_count() or 4
    print(f"  CPU    : {cpu_cores} cores")
    print("=" * 60)

    tmp = Path(tempfile.mkdtemp(prefix="xs_bench_"))
    try:
        video, sub = _create_test_assets(tmp, duration_s=30)
        print()

        CRF = 22
        half_cores = max(2, cpu_cores // 2)
        gpu = detect_gpu_encoder()

        # Build test configurations
        configs: list[dict] = [
            {
                "label":   "CPU libx264 medium (baseline)",
                "encoder": "libx264",
                "params":  ["-preset", "medium", "-crf", str(CRF)],
                "threads": None,
            },
            {
                "label":   "CPU libx264 fast",
                "encoder": "libx264",
                "params":  ["-preset", "fast", "-crf", str(CRF)],
                "threads": None,
            },
            {
                "label":   f"CPU libx264 fast + threads={half_cores} (50% cores)",
                "encoder": "libx264",
                "params":  ["-preset", "fast", "-crf", str(CRF)],
                "threads": half_cores,
            },
            {
                "label":   "CPU libx264 veryfast",
                "encoder": "libx264",
                "params":  ["-preset", "veryfast", "-crf", str(CRF)],
                "threads": None,
            },
        ]

        if gpu:
            enc_name, enc_params = gpu
            configs.append({
                "label":   f"GPU {enc_name} (auto-detected)",
                "encoder": enc_name,
                "params":  enc_params,
                "threads": None,
            })
        else:
            print("[info] Không phát hiện GPU encoder (NVENC/AMF/QSV) — bỏ qua test GPU")

        # Run all configs
        results = []
        for i, cfg in enumerate(configs):
            out = tmp / f"out_{i}.mp4"
            print(f"[{i+1}/{len(configs)}] {cfg['label']} ...")
            r = run_encode(
                label=cfg["label"],
                video=video,
                sub=sub,
                out=out,
                encoder=cfg["encoder"],
                encoder_params=cfg["params"],
                threads=cfg.get("threads"),
            )
            results.append(r)
            status = "OK" if r["ok"] else f"FAIL: {r['error'][:80]}"
            print(f"       → {r['time_s']:.1f}s  {r['size_kb']} KiB  "
                  f"CPU avg={r['avg_cpu']:.0f}% max={r['max_cpu']:.0f}%  {status}")
            print()

        # Summary table
        print("=" * 60)
        print(f"{'Config':<42} {'Time':>6} {'Size':>7} {'AvgCPU':>8} {'MaxCPU':>8} {'vs base':>8}")
        print("-" * 60)
        baseline_time = results[0]["time_s"] if results else 1
        for r in results:
            vs = f"{(r['time_s'] / baseline_time - 1) * 100:+.0f}%" if r["ok"] else "FAIL"
            label = r["label"][:42]
            print(f"{label:<42} {r['time_s']:>5.1f}s {r['size_kb']:>6}K "
                  f"{r['avg_cpu']:>7.0f}% {r['max_cpu']:>7.0f}% {vs:>8}")
        print("=" * 60)
        if not HAS_PSUTIL:
            print("Lưu ý: CPU% = N/A vì không có psutil. Chạy: pip install psutil")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

if __name__ == "__main__":
    main()
