"""Test detail fetch with proper path setup."""
import sys, os, requests, re
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "app"))
sys.path.insert(0, os.path.dirname(__file__))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Import directly from phimngan_db
from phimngan_db import _fetch_detail_page, LIST_HEADERS, DETAIL_API_URL, _build_detail_rsc_state

out = open("phimngan_debug_detail2.txt", "w", encoding="utf-8")

test_slugs = [
    "ep-ga-mot-thoi-duyen-tinh-mot-kiep",
    "ph-l-m-cng-chiu",
]

for slug in test_slugs:
    out.write(f"\n=== Testing: {slug} ===\n")
    try:
        html = _fetch_detail_page(slug)
        out.write(f"Success! Length: {len(html)}\n")
        eps = re.findall(r'"videoUrl":"([^"]+)"', html)
        out.write(f"Episodes: {len(eps)}\n")
        if eps:
            out.write(f"First: {eps[0][:100]}\n")
    except Exception as e:
        out.write(f"Error: {e}\n")

out.close()
print("Done")
