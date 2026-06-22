import re
text = open("phimngan_debug.html", encoding="utf-8").read()

out = open("phimngan_debug_pagination.txt", "w", encoding="utf-8")

# Find pagination-related content
out.write("=== page=N patterns ===")
for m in re.finditer(r'page=\d+', text):
    start = max(0, m.start()-20)
    end = min(len(text), m.end()+20)
    out.write(f"pos={m.start()}: {repr(text[start:end])}\n")

# Find "Trang"
out.write("\n=== Trang patterns ===")
for m in re.finditer(r'Trang[^<]{0,100}', text):
    out.write(f"pos={m.start()}: {repr(m.group(0))}\n")

# Find next page button / href
out.write("\n=== a href with page ===")
for m in re.finditer(r'<a[^>]+href=[^>]+>[^<]*trang[^<]*</a>', text, re.IGNORECASE):
    out.write(repr(m.group(0)) + "\n")

# Check if the data has an API endpoint
out.write("\n=== API URLs in HTML ===")
apis = re.findall(r'https?://[^\s"\'<>]+', text)
for a in apis[:20]:
    if "api" in a.lower() or "phimngan" in a.lower():
        out.write(a + "\n")

# Check if there are next/prev buttons
out.write("\n=== Buttons/links ===")
for m in re.finditer(r'<button[^>]*>[^<]*trang[^<]*</button>', text, re.IGNORECASE):
    out.write(repr(m.group(0)) + "\n")
for m in re.finditer(r'<a[^>]*>[^<]*(?:trang|tiếp|prev|next)[^<]*</a>', text, re.IGNORECASE):
    out.write(repr(m.group(0))[:200] + "\n")

out.close()
print("Done")
