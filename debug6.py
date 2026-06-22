"""Check if page 2 API returns data."""
import sys, os, re, json, requests
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "app"))
from urllib.parse import quote

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

out = open("phimngan_debug_page2.txt", "w", encoding="utf-8")

for page in [1, 2, 3, 13]:
    state = quote(json.dumps([
        "", {"children": [
            "(root)", {
                "children": [
                    "movies", {
                        "children": [
                            f"__PAGE__?{{\"page\":\"{page}\"}}",
                            {}, f"/movies?page={page}", "refetch"
                        ]
                    }
                ]
            }
        ]}], separators=(",", ":")), safe="")

    params = {"page": str(page), "_rsc": "test123"}
    r = requests.get("https://phimngan.tv/movies", headers=HEADERS, params=params, timeout=20)
    text = r.content.decode("utf-8", errors="replace")
    out.write(f"\n=== PAGE {page} ===\n")
    out.write(f"Status: {r.status_code}, Length: {len(text)}\n")

    # Count movies
    ids = re.findall(r'"id":"([^"]{10,50})","title":"([^"]{3,100})","slug":"([^"]{3,200})"', text)
    out.write(f"Movies: {len(ids)}\n")
    if ids:
        for i, (mid, title, slug) in enumerate(ids[:2]):
            out.write(f"  [{i}] {slug} | {title[:50]}\n")
    else:
        # Try brace counting
        count = text.count('{"id":"')
        out.write(f'Raw {{"id":" count: {count}\n')

out.close()
print("Done")
