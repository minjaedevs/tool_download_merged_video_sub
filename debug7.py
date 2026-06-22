"""Debug: fetch detail page for a specific movie."""
import sys, os, re, requests, json
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

out = open("phimngan_debug_detail.txt", "w", encoding="utf-8")

slug = "ep-ga-mot-thoi-duyen-tinh-mot-kiep"

# Try different URL/header combinations
tests = [
    # Test 1: Basic URL with _rsc param
    {
        "name": "basic_url",
        "url": f"https://phimngan.tv/movies/{slug}?_rsc=abc123",
        "extra_headers": {},
    },
    # Test 2: With Referer pointing to /movies/{slug}
    {
        "name": "with_referer",
        "url": f"https://phimngan.tv/movies/{slug}?_rsc=abc123",
        "extra_headers": {"Referer": f"https://phimngan.tv/movies/{slug}"},
    },
    # Test 3: With Next-Router-State-Tree (from workers.py format)
    {
        "name": "with_rsc_state",
        "url": f"https://phimngan.tv/movies/{slug}?_rsc=abc123",
        "extra_headers": {
            "Referer": f"https://phimngan.tv/movies/{slug}",
            "Next-Router-State-Tree": quote(json.dumps([
                "", {"children": [
                    "(root)", {
                        "children": [
                            "movies", {
                                "children": [
                                    ["id", slug, "c"],
                                    {"children": ["__PAGE__", {}, f"/movies/{slug}", "refresh"]},
                                    None,
                                    "refetch",
                                ]
                            }
                        ]
                    }
                ]}], separators=(",", ":")), safe=""),
        },
    },
    # Test 4: From debug HTML - see what works for list page
    {
        "name": "without_rsc",
        "url": f"https://phimngan.tv/movies/{slug}",
        "extra_headers": {"Referer": f"https://phimngan.tv/movies/{slug}"},
    },
]

for test in tests:
    headers = dict(HEADERS)
    headers.update(test["extra_headers"])
    try:
        r = requests.get(test["url"], headers=headers, timeout=20)
        text = r.content.decode("utf-8", errors="replace")
        out.write(f"\n=== {test['name']} ===\n")
        out.write(f"URL: {test['url']}\n")
        out.write(f"Status: {r.status_code}, Length: {len(text)}\n")
        out.write(f"First 500: {text[:500]}\n")
        out.write(f"Last 200: {text[-200:]}\n")

        # Check for movie object
        movie_match = re.search(r'"movie":\{"id":"([^"]+)"', text)
        if movie_match:
            out.write(f"MOVIE FOUND: {movie_match.group(1)}\n")
        else:
            out.write("NO movie object found\n")

        # Check for episode patterns
        eps = re.findall(r'"id":"([^"]{10,})","title":"([^"]*)","order":(\d+)', text)
        out.write(f"Episodes (id+title+order): {len(eps)}\n")
        for e in eps[:3]:
            out.write(f"  order={e[2]}: {e[1][:30]}, id={e[0][:20]}\n")

        # Check for videoUrl
        video_urls = re.findall(r'"videoUrl":"([^"]+)"', text)
        out.write(f"videoUrl count: {len(video_urls)}\n")
        if video_urls:
            out.write(f"  First: {video_urls[0]}\n")

    except Exception as e:
        out.write(f"\n=== {test['name']} ===\nERROR: {e}\n")

out.close()
print("Done")
