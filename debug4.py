import re, json

text = open("phimngan_debug.html", encoding="utf-8").read()

# Try the full regex
MOVIE_RE = re.compile(
    r'\{"id":"(?P<id>[^"]+)","title":"(?P<title>[^"]*)","slug":"(?P<slug>[^"]+)",'
    r'"description":"(?P<description>[^"]*)","tags":"(?P<tags>[^"]*)","cover":"(?P<cover>[^"]+)",'
    r'"episodeCount":(?P<episode_count>\d+),'
    r'"totalEpisode":(?P<total_episode>\d+),'
    r'"viewCount":(?P<view_count>\d+),"favoriteCount":(?P<favorite_count>\d+),'
    r'"isHot":(?P<is_hot>true|false),"isFeatured":(?P<is_featured>true|false)\}',
    re.DOTALL
)

matches = list(MOVIE_RE.finditer(text))
out = open("phimngan_debug_results4.txt", "w", encoding="utf-8")
out.write("Full regex matches: " + str(len(matches)) + "\n")

# Try a minimal regex first
MINI_RE = re.compile(r'\{"id":"[^"]+","title":"[^"]*","slug":"[^"]+"')
mini = list(MINI_RE.finditer(text))
out.write("Minimal regex matches: " + str(len(mini)) + "\n")

# Try without description field
NO_DESC_RE = re.compile(
    r'\{"id":"(?P<id>[^"]+)","title":"(?P<title>[^"]*)","slug":"(?P<slug>[^"]+)",'
    r'"description":"[^"]*","tags":"[^"]*","cover":"(?P<cover>[^"]+)",'
    r'"episodeCount":(?P<episode_count>\d+),'
    r'"totalEpisode":(?P<total_episode>\d+),'
    r'"viewCount":(?P<view_count>\d+),"favoriteCount":(?P<favorite_count>\d+),'
    r'"isHot":(?P<is_hot>true|false),"isFeatured":(?P<is_featured>true|false)\}',
    re.DOTALL
)
no_desc = list(NO_DESC_RE.finditer(text))
out.write("No-desc regex matches: " + str(len(no_desc)) + "\n")

# Check: description might have unescaped " chars that break our [^"]*
# Let's see what the actual description looks like in the raw text
idx_desc = text.find('"description":"')
if idx_desc >= 0:
    out.write("\nDescription context (raw):\n")
    # Find the closing of the description value
    # It starts with "description":" and ends with ","tags":"
    desc_end = text.find('","tags":"', idx_desc)
    if desc_end >= 0:
        out.write("Description: " + text[idx_desc:desc_end] + "\n")

# Check: description contains unescaped " (quotes in Vietnamese text)
# The description contains "Kinh Hôn Bất Thục" with quotes INSIDE the string
# This means the JSON has \" escaped quotes... let's check
idx = text.find("Kinh")
if idx >= 0:
    out.write("\nContext around 'Kinh Hon':\n")
    start = max(0, idx - 50)
    end = min(len(text), idx + 150)
    raw = text[start:end]
    out.write(repr(raw) + "\n")

# Also check: does the movie object end with } or something else?
idx = text.find('"id":"f609ecca')
if idx >= 0:
    # Find the closing brace of this object
    # Start from the opening brace
    open_brace = text.rfind("{", 0, idx)
    if open_brace >= 0:
        out.write("\nMovie object start: " + repr(text[open_brace:open_brace+50]) + "\n")
        # Try to find the closing }
        # Use brace counting
        depth = 0
        end_pos = open_brace
        for i in range(open_brace, min(len(text), open_brace + 2000)):
            if text[i] == '{':
                depth += 1
            elif text[i] == '}':
                depth -= 1
                if depth == 0:
                    end_pos = i
                    break
        obj_str = text[open_brace:end_pos+1]
        out.write("Full object (" + str(len(obj_str)) + " chars):\n")
        out.write(obj_str[:500] + "\n...")
        out.write(obj_str[-100:] + "\n")
        
        # Try to parse as JSON
        try:
            obj = json.loads(obj_str)
            out.write("JSON parsed OK!\n")
            out.write("Keys: " + str(list(obj.keys())) + "\n")
        except Exception as e:
            out.write("JSON parse error: " + str(e) + "\n")

out.close()
print("Done")
