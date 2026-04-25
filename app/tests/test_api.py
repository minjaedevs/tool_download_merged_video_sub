import requests

url = "https://api.xemshort.top/allepisode?shortPlayId=2041683155433291777"
headers = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Mobile Safari/537.36",
    "Referer": "https://xemshort.top/",
    "Origin": "https://xemshort.top",
    "short-source": "web",
}
r = requests.get(url, headers=headers, timeout=15)
print(f"Status: {r.status_code}")
print(f"Content-Type: {r.headers.get('content-type')}")
print(f"Raw (first 500): {r.text[:500]}")
data = r.json()
print(f"Keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")
if isinstance(data, dict):
    print(f"success: {data.get('success')}")
    print(f"data len: {len(data.get('data', []))}")
    if data.get("data"):
        print(f"First item keys: {list(data['data'][0].keys())}")
        print(f"First item: {data['data'][0]}")
