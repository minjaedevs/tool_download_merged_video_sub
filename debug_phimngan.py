"""Debug phimngan.tv API response."""
import sys, os, re, json, requests
from urllib.parse import quote

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "app"))

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

state = quote(json.dumps([
    "", {"children": [
        "(root)", {
            "children": [
                "movies", {
                    "children": [
                        "__PAGE__", {}, "/movies", "refresh"
                    ]
                }
            ]
        }
    ]}], separators=(",", ":")), safe="")

url = "https://phimngan.tv/movies"
r = requests.get(url, headers=HEADERS, params={"_rsc": "f0wvx"}, timeout=20)
text = r.content.decode("utf-8", errors="replace")

print(f"Status: {r.status_code}, Length: {len(text)}")
with open("phimngan_debug.html", "w", encoding="utf-8") as f:
    f.write(text)
print(f"Full HTML written to phimngan_debug.html ({len(text)} bytes)")

results = []
results.append(f"Status: {r.status_code}, Length: {len(text)}")
results.append(f"Has __NEXT_DATA__: {'__NEXT_DATA__' in text}")
results.append(f'Has "RSC": {"RSC" in text}')

# Pattern 1
p1 = re.compile(r'"movie":\s*\{.*?\}(?=,\s*"|$)', re.DOTALL)
m1 = p1.findall(text)
results.append(f'Pattern "movie":{{...}} : {len(m1)} matches')
if m1:
    results.append("First: " + m1[0][:500])

# Slugs
slugs = re.findall(r'"slug":"([^"]+)"', text)
results.append(f'"slug" patterns: {len(slugs)}')
for s in slugs[:5]:
    results.append(f"  slug={s}")

# episodeCount
ep_counts = re.findall(r'"episodeCount":(\d+)', text)
results.append(f'"episodeCount" patterns: {len(ep_counts)}')
for e in ep_counts[:5]:
    results.append(f"  episodeCount={e}")

# id+title
id_titles = re.findall(r'"id":"([^"]+)","title":"([^"]+)"', text)
results.append(f'"id","title" pairs: {len(id_titles)}')
for t in id_titles[:5]:
    results.append(f"  id={t[0][:30]}, title={t[1][:50]}")

# RSC DATA
rsc_json = re.findall(r'<script[^>]+id="__RSC_DATA__"[^>]*>(.*?)</script>', text, re.DOTALL)
results.append(f'RSC DATA script tags: {len(rsc_json)}')
if rsc_json:
    results.append("First 500: " + rsc_json[0][:500])

# Write results
with open("phimngan_debug_results.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(results))
print("Done. Check phimngan_debug.html and phimngan_debug_results.txt")
