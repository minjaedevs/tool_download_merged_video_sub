#!/usr/bin/env python3
"""
Fetch data từ API xemshort.top bằng curl + xử lý JSON bằng Python.
Dùng curl vì curl có TLS fingerprint giống trình duyệt thật.
"""
import json
import subprocess
import sys
import tempfile
import re
from pathlib import Path


def fetch_with_curl(movie_id: str) -> dict:
    """
    Gọi curl với OPTIONS preflight rồi GET, parse JSON output.
    """
    url = f"https://api.xemshort.top/allepisode?shortPlayId={movie_id}"

    # Tách headers ra list để truyền cho subprocess
    curl_cmd = [
        "curl", "-s", "-L",
        "-X", "OPTIONS",
        "--url", url,
        "-H", "accept: */*",
        "-H", "accept-language: en-AU,en;q=0.9,vi;q=0.8,fr-FR;q=0.7,fr;q=0.6,en-US;q=0.5,hi;q=0.4,ar;q=0.3",
        "-H", "access-control-request-headers: short-source",
        "-H", "access-control-request-method: GET",
        "-H", "origin: https://xemshort.top",
        "-H", "priority: u=1, i",
        "-H", "referer: https://xemshort.top/",
        "-H", "sec-fetch-dest: empty",
        "-H", "sec-fetch-mode: cors",
        "-H", "sec-fetch-site: same-site",
        "-H", 'user-agent: Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Mobile Safari/537.36',
        # GET headers
        "-H", "accept: */*",
        "-H", "accept-language: vi,en;q=0.9",
        "-H", "origin: https://xemshort.top",
        "-H", "referer: https://xemshort.top/",
        "-H", "short-source: web",
    ]

    try:
        result = subprocess.run(
            curl_cmd,
            capture_output=True,
            text=True,
            timeout=30,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        print("ERROR: curl not found. Please install curl.", file=sys.stderr)
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print("ERROR: curl timed out.", file=sys.stderr)
        sys.exit(1)

    if result.returncode != 0:
        print(f"ERROR: curl exited with code {result.returncode}", file=sys.stderr)
        print(f"stderr: {result.stderr}", file=sys.stderr)
        sys.exit(1)

    return result.stdout


def fetch_and_save(movie_id: str, output: Path) -> int:
    raw = fetch_with_curl(movie_id)
    print(f"Raw response length: {len(raw)} chars")

    # Save raw
    raw_path = output.with_suffix(".raw.txt")
    raw_path.write_text(raw, encoding="utf-8")
    print(f"Saved raw: {raw_path}")

    # Parse JSON
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON: {e}", file=sys.stderr)
        print(f"Preview: {raw[:300]}", file=sys.stderr)
        return 1

    # Check for obfuscated
    api_data = data.get("data") if isinstance(data, dict) else None
    if isinstance(api_data, str) and len(api_data) > 100:
        print("ERROR: API tra ve du lieu bi ma hoa (obfuscated).", file=sys.stderr)
        print(f"  First 100 chars: {api_data[:100]}", file=sys.stderr)
        return 2

    if isinstance(data, dict) and not data.get("success", True):
        print(f"ERROR: API báo lỗi: {data.get('message', 'unknown')}", file=sys.stderr)
        return 3

    episodes = data.get("data", []) if isinstance(data, dict) else data
    if not isinstance(episodes, list):
        print(f"ERROR: data không phải list, type={type(episodes)}", file=sys.stderr)
        return 4

    # Chuẩn hóa
    clean_data = []
    for ep in episodes:
        if not isinstance(ep, dict):
            continue
        sub_url = None
        subs = ep.get("subtitle") or []
        if ep.get("onSubtitle") and subs:
            sub_url = subs[0].get("url") if isinstance(subs, list) else None

        clean_data.append({
            "id": ep.get("id", ""),
            "name": ep.get("name", "Unknown"),
            "episode": ep.get("episode", 0),
            "play": ep.get("play", ""),
            "onSubtitle": ep.get("onSubtitle", False),
            "subtitle": [sub_url] if sub_url else [],
        })

    output_data = {"success": True, "data": clean_data}
    with open(output, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    print(f"Success: {len(clean_data)} episodes saved to {output}")
    if clean_data:
        first = clean_data[0]
        print(f"  First: {first['name']} - ep{first['episode']}")

    return 0


def main():
    if len(sys.argv) < 2:
        movie_id = input("Enter movie ID: ").strip()
    else:
        movie_id = sys.argv[1]

    output = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(f"{movie_id}.json")

    print(f"Fetching movie ID: {movie_id}")
    print(f"Output: {output}")
    print("-" * 40)

    code = fetch_and_save(movie_id, output)
    sys.exit(code)


if __name__ == "__main__":
    main()
