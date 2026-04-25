#!/usr/bin/env python3
"""
Fetch data từ API xemshort.top - Hỗ trợ cả JSON sạch và encrypted
"""
import argparse, base64, json, sys, gzip, zlib
from pathlib import Path

try:
    import requests
except ImportError:
    print("❌ pip install requests", file=sys.stderr); sys.exit(1)

try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

API_URL = "https://api.xemshort.top/allepisode?shortPlayId={movie_id}"

HEADERS = {
    "accept": "*/*",
    "accept-language": "vi,en;q=0.9",
    "origin": "https://xemshort.top",
    "referer": "https://xemshort.top/",
    "user-agent": "Mozilla/5.0 (Linux; Android 10; Mobile) AppleWebKit/537.36 Chrome/120.0.0.0 Mobile Safari/537.36",
    "short-source": "web",  # QUAN TRỌNG: phải là "web" để nhận JSON sạch
}

def b64_decode_safe(s: str) -> bytes:
    s = s.strip().strip('"').replace("\\/", "/").replace("\n","").replace(" ","")
    padding = 4 - (len(s) % 4)
    if padding < 4: s += "=" * padding
    return base64.b64decode(s)

def try_decrypt_bytes(raw: bytes) -> dict | list | None:
    """Thử nhiều chiến lược giải mã."""
    # 1. JSON thuần
    try:
        return json.loads(raw.decode('utf-8'))
    except: pass
    
    # 2. GZIP
    if raw[:2] == b'\x1f\x8b':
        try: return json.loads(gzip.decompress(raw).decode('utf-8'))
        except: pass
    
    # 3. ZLIB
    if raw[:1] == b'\x78':
        try: return json.loads(zlib.decompress(raw).decode('utf-8'))
        except: pass
    
    # 4. AES (nếu có pycryptodome)
    if HAS_CRYPTO and len(raw) % 16 == 0:
        keys = ["xemshort-secret-", "xemshortSecret16", "netshort12345678", 
                "0123456789abcdef", "xemshort.top/api", "1234567890123456"]
        for k in keys:
            key = k.encode()
            if len(key) not in [16,24,32]: continue
            for mode, iv in [(AES.MODE_ECB, None), (AES.MODE_CBC, b"\x00"*16)]:
                try:
                    cipher = AES.new(key, mode, iv=iv) if iv else AES.new(key, mode)
                    plain = unpad(cipher.decrypt(raw), 16)
                    return json.loads(plain.decode('utf-8'))
                except: pass
    return None

def parse_response(resp: requests.Response) -> dict | None:
    """Parse response, hỗ trợ cả encrypted và plain JSON."""
    # Thử JSON trực tiếp trước
    try:
        data = resp.json()
        # Nếu có shortPlayEpisodeInfos → JSON sạch
        if "shortPlayEpisodeInfos" in data:
            return {"success": True, "data": data["shortPlayEpisodeInfos"], "meta": {
                "name": data.get("shortPlayName"), "id": data.get("shortPlayId")
            }}
        # Nếu có data field là string → có thể encrypted
        if isinstance(data.get("data"), str) and len(data["data"]) > 100:
            raw = b64_decode_safe(data["data"])
            decrypted = try_decrypt_bytes(raw)
            if decrypted:
                if isinstance(decrypted, list):
                    return {"success": True, "data": decrypted}
                if isinstance(decrypted, dict):
                    return decrypted
    except json.JSONDecodeError:
        pass
    return None

def normalize_episodes(episodes: list) -> list:
    """Chuẩn hóa danh sách tập cho GUI."""
    result = []
    for ep in episodes:
        if not isinstance(ep, dict): continue
        subs = ep.get("subtitleList") or []
        sub_url = subs[0].get("url") if subs else None
        result.append({
            "episode": ep.get("episodeNo"),
            "name": ep.get("episodeName") or f"Tập {ep.get('episodeNo')}",
            "play": ep.get("playVoucher"),
            "subtitle": sub_url,
            "isLock": ep.get("isLock", False),
            "episodeId": ep.get("episodeId"),
            "cover": ep.get("episodeCover"),
        })
    return result

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("movie_id")
    parser.add_argument("-o", "--output")
    parser.add_argument("--short-source", default="web")
    parser.add_argument("--raw", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    
    output = Path(args.output or f"{args.movie_id}.json")
    headers = {**HEADERS, "short-source": args.short_source}
    url = API_URL.format(movie_id=args.movie_id)
    
    print(f"📡 GET {url}\n   short-source: {args.short_source}")
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"❌ Network error: {e}"); return 1
    
    # Lưu raw để debug
    raw_path = output.with_suffix(".raw.txt")
    raw_path.write_bytes(resp.content)
    
    if args.raw or args.debug:
        print(f"💾 Raw: {raw_path}\n📦 Content-Type: {resp.headers.get('content-type')}")
        preview = resp.text[:200]
        print(f"🔍 Preview: {preview}...")
        if args.raw: return 0
    
    result = parse_response(resp)
    if not result or not result.get("success"):
        print("❌ Không parse được response")
        print("💡 Thử: python fetch_xemshort.py <id> --short-source web --debug")
        print("💡 Hoặc kiểm tra JS website để tìm key giải mã")
        return 2
    
    episodes = normalize_episodes(result["data"])
    output_data = {"success": True, "data": episodes, "meta": result.get("meta")}
    
    with open(output, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)
    
    print(f"✅ Lưu {len(episodes)} tập vào: {output}")
    if episodes:
        e = episodes[0]
        print(f"   🎬 Tập #{e['episode']}: play={'✓' if e['play'] else '✗'} sub={'✓' if e['subtitle'] else '✗'}")
    return 0

if __name__ == "__main__":
    sys.exit(main())