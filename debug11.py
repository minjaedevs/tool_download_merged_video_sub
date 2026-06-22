"""Test detail fetch standalone."""
import sys, os, requests, re, json
from urllib.parse import quote
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "RSC": "1",
    "Referer": "https://phimngan.tv/movies",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "sec-ch-ua": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
}

def _build_rsc(slug):
    path = f"/movies/{slug}"
    state = ["", {"children": ["(root)", {"children": ["movies", {"children": [["id", slug, "c"], {"children": ["__PAGE__", {}, path, "refresh"]}, None, "refetch"]}]}]}]
    return quote(json.dumps(state, separators=(",", ":")), safe="")

slug = "ep-ga-mot-thoi-duyen-tinh-mot-kiep"
url = f"https://phimngan.tv/movies/{slug}?_rsc=h1khq"
headers = dict(HEADERS)
headers["Referer"] = f"https://phimngan.tv/movies/{slug}"
headers["Next-Router-State-Tree"] = _build_rsc(slug)

print(f"URL: {url}")
print(f"RSC length: {len(headers['Next-Router-State-Tree'])}")

r = requests.get(url, headers=headers, timeout=20)
print(f"Status: {r.status_code}")
print(f"Content length: {len(r.content)}")
if r.status_code != 200:
    print(f"Body: {r.text[:300]}")
else:
    text = r.content.decode("utf-8", errors="replace")
    eps = re.findall(r'"videoUrl":"([^"]+)"', text)
    print(f"Episodes: {len(eps)}")
    if eps:
        print(f"First: {eps[0][:80]}")
