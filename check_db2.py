"""Check movies with episode_count=0 vs ones that actually have episodes in DB."""
import requests, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SUPABASE_URL = "https://rmsxnajcudkjmtqsfhot.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InJtc3huYWpjdWRram10cXNmaG90Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4MDUyNDM5NSwiZXhwIjoyMDk2MTAwMzk1fQ.CvLi4fkjjSMbRaeKi85xC_d5MDCCkv2tcz4iuKinOgU"
HEADERS = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}

# Check movies with episode_count = 0 or null
r1 = requests.get(f"{SUPABASE_URL}/rest/v1/phimngan_movies", headers=HEADERS, params={
    "select": "slug,name,episode_count,total_episode",
    "episode_count": "eq.0",
    "order": "id.asc",
    "limit": "20"
}, timeout=30)
if r1.ok:
    rows = r1.json()
    print(f"Movies with episode_count=0: {len(rows)}")
    for m in rows:
        print(f"  slug={m.get('slug')}  name={m.get('name','')[:40]}  ep={m.get('episode_count')}/{m.get('total_episode')}")
else:
    print(f"Error: {r1.text[:200]}")

# Check movies with episode_count > 0
r2 = requests.get(f"{SUPABASE_URL}/rest/v1/phimngan_movies", headers=HEADERS, params={
    "select": "slug,name,episode_count",
    "episode_count": "gt.0",
    "order": "episode_count.desc",
    "limit": "10"
}, timeout=30)
if r2.ok:
    rows2 = r2.json()
    print(f"\nMovies with episode_count>0: {len(rows2)}")
    for m in rows2[:10]:
        print(f"  slug={m.get('slug')}  ep={m.get('episode_count')}  name={m.get('name','')[:40]}")

# Check episode counts per movie (actual data in episodes table)
r3 = requests.get(f"{SUPABASE_URL}/rest/v1/phimngan_episodes", headers={
    **HEADERS, "Prefer": "count=exact"
}, params={
    "select": "movie_slug",
    "limit": "1"
}, timeout=30)
total_eps = 0
if r3.ok:
    cr = r3.headers.get("Content-Range", "")
    total_eps = int(cr.rsplit("/", 1)[-1]) if "/" in cr else 0
print(f"\nTotal episodes in DB: {total_eps}")
