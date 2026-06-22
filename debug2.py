"""Debug: analyze HTML structure around movie data."""
import re

text = open("phimngan_debug.html", encoding="utf-8").read()

# All counts
slugs = re.findall(r'"slug":"([^"]+)"', text)
ids_titles = re.findall(r'"id":"([^"]{10,50})","title":"([^"]{3,100})"', text)
eps = re.findall(r'"episodeCount":(\d+)', text)
views = re.findall(r'"views":"([^"]+)"', text)
likes = re.findall(r'"like":(\d+)', text)
comments = re.findall(r'"comment":(\d+)', text)
isHot = re.findall(r'"isHot":(true|false)', text)
isFeatured = re.findall(r'"isFeatured":(true|false)', text)
isVoice = re.findall(r'"isVoice":(true|false)', text)
updatedAt = re.findall(r'"updatedAt":"([^"]+)"', text)
cover = re.findall(r'"cover":"([^"]+)"', text)

results = []
results.append(f"slugs: {len(slugs)}")
results.append(f"ids+titles: {len(ids_titles)}")
results.append(f"episodeCount: {len(eps)}")
results.append(f"views: {len(views)}")
results.append(f"likes: {len(likes)}")
results.append(f"comments: {len(comments)}")
results.append(f"isHot: {len(isHot)}")
results.append(f"isFeatured: {len(isFeatured)}")
results.append(f"isVoice: {len(isVoice)}")
results.append(f"updatedAt: {len(updatedAt)}")
results.append(f"cover: {len(cover)}")

results.append("\n=== First 3 movies ===")
for i, (mid, title) in enumerate(ids_titles[:3]):
    results.append(f"  [{i}] id={mid}")
    results.append(f"       title={title}")
    if i < len(eps): results.append(f"       episodeCount={eps[i]}")
    if i < len(views): results.append(f"       views={views[i]}")
    if i < len(likes): results.append(f"       like={likes[i]}")
    if i < len(updatedAt): results.append(f"       updatedAt={updatedAt[i]}")

# Find context around first movie ID
results.append("\n=== Context around first movie ===")
idx = text.find('"id":"f609ecca')
if idx >= 0:
    # Get 600 chars around it
    start = max(0, idx - 100)
    end = min(len(text), idx + 600)
    snippet = text[start:end]
    results.append(snippet)

# Show ALL unique field keys near movie objects
results.append("\n=== Field patterns around movies ===")
# Find segments that contain both id and title
all_segments = []
for m in re.finditer(r'"id":"([^"]{10,50})","title":"([^"]{3,100})"', text):
    start = max(0, m.start() - 10)
    end = min(len(text), m.end() + 500)
    seg = text[start:end]
    all_segments.append(seg)

results.append(f"Total segments with id+title: {len(all_segments)}")
for i, seg in enumerate(all_segments[:2]):
    results.append(f"\n--- Segment {i} ---")
    results.append(seg)

# Also check: what structure surrounds slug
results.append("\n=== Context around first slug ===")
idx2 = text.find('"slug":"sieu-cuong"')
if idx2 >= 0:
    start = max(0, idx2 - 100)
    end = min(len(text), idx2 + 400)
    results.append(text[start:end])

# Check: is there a "movie" wrapper or is it all flat?
results.append("\n=== 'movie' keyword count ===")
movie_count = text.count('"movie":')
results.append(f'"movie": count: {movie_count}')
movieobj_count = text.count('"movie":{')
results.append(f'"movie":{{ count: {movieobj_count}')

# Write
output = "\n".join(results)
with open("phimngan_debug_results2.txt", "w", encoding="utf-8") as f:
    f.write(output)
print("Written to phimngan_debug_results2.txt")
