"""Final DB status check."""
import requests, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SUPABASE_URL = "https://rmsxnajcudkjmtqsfhot.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InJtc3huYWpjdWRram10cXNmaG90Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4MDUyNDM5NSwiZXhwIjoyMDk2MTAwMzk1fQ.CvLi4fkjjSMbRaeKi85xC_d5MDCCkv2tcz4iuKinOgU"

headers = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Prefer": "count=exact",
}

r1 = requests.get(f"{SUPABASE_URL}/rest/v1/phimngan_movies", headers=headers, params={"select": "id", "limit": "1"}, timeout=30)
movies_total = int(r1.headers.get("Content-Range", "/0").rsplit("/", 1)[-1]) if r1.ok else 0

r2 = requests.get(f"{SUPABASE_URL}/rest/v1/phimngan_episodes", headers=headers, params={"select": "id", "limit": "1"}, timeout=30)
eps_total = int(r2.headers.get("Content-Range", "/0").rsplit("/", 1)[-1]) if r2.ok else 0

print(f"=== DATABASE STATUS ===")
print(f"Movies:     {movies_total}")
print(f"Episodes:  {eps_total}")
print(f"Avg ep/m:  {eps_total / movies_total:.1f}" if movies_total else "N/A")

# Movies with most episodes
r3 = requests.get(f"{SUPABASE_URL}/rest/v1/phimngan_movies", headers={
    "apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"
}, params={"select": "slug,name,episode_count,total_episode,views", "order": "views.desc", "limit": "10"}, timeout=30)
if r3.ok:
    rows = r3.json()
    print(f"\nTop 10 phim nhieu views:")
    for i, m in enumerate(rows, 1):
        name = str(m.get("name") or "")[:45]
        ep = m.get("episode_count", 0) or 0
        tot = m.get("total_episode", 0) or 0
        views = m.get("views", 0) or 0
        print(f"  [{i:2d}] ep={ep:3d}/{tot:<3d}  views={views:>10,}  {name}")

# Movies missing episodes
r4 = requests.get(f"{SUPABASE_URL}/rest/v1/phimngan_movies", headers={
    "apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
    "Prefer": "count=exact"
}, params={
    "select": "slug,name",
    "episode_count": "gte.1",
    "limit": "5"
}, timeout=30)
# Count movies with episode_count > 0
r5 = requests.get(f"{SUPABASE_URL}/rest/v1/phimngan_movies", headers={
    "apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
}, params={
    "select": "slug",
    "episode_count": "gte.1",
    "order": "episode_count.desc",
    "limit": "5"
}, timeout=30)
if r5.ok:
    rows = r5.json()
    print(f"\nTop 5 phim nhieu tap nhat:")
    for m in rows:
        print(f"  - {m.get('slug')}  ep={m.get('episode_count', 0)}")
