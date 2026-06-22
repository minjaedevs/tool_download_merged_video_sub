"""Quick test: check what slug values are stored in DB."""
import requests, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SUPABASE_URL = "https://rmsxnajcudkjmtqsfhot.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InJtc3huYWpjdWRram10cXNmaG90Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4MDUyNDM5NSwiZXhwIjoyMDk2MTAwMzk1fQ.CvLi4fkjjSMbRaeKi85xC_d5MDCCkv2tcz4iuKinOgU"

headers = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "count=exact",
}

resp = requests.get(
    f"{SUPABASE_URL}/rest/v1/phimngan_movies",
    headers=headers,
    params={"select": "slug,name", "limit": "5"},
    timeout=30,
)
print(f"Status: {resp.status_code}")
if resp.ok:
    rows = resp.json()
    print(f"Count: {len(rows)}")
    for r in rows[:5]:
        print(f"  slug={repr(r.get('slug'))} | name={r.get('name', '')[:40]}")
else:
    print(f"Error: {resp.text[:200]}")
