import re
text = open("phimngan_debug.html", encoding="utf-8").read()
out = open("phimngan_debug_results3.txt", "w", encoding="utf-8")

# Try movie regex
MOVIE_RE = re.compile(
    r'\{"id":"(?P<id>[^"]+)","title":"(?P<title>[^"]*)","slug":"(?P<slug>[^"]+)",'
    r'"description":"[^"]*","tags":"[^"]*","cover":"(?P<cover>[^"]+)",'
    r'"episodeCount":(?P<episode_count>\d+),'
    r'"totalEpisode":(?P<total_episode>\d+),'
    r'"viewCount":(?P<view_count>\d+),"favoriteCount":(?P<favorite_count>\d+),'
    r'"isHot":(?P<is_hot>true|false),"isFeatured":(?P<is_featured>true|false)\}',
    re.DOTALL
)

matches = list(MOVIE_RE.finditer(text))
out.write("Movie regex matches: " + str(len(matches)) + "\n")
for i, m in enumerate(matches[:5]):
    out.write("  [" + str(i) + "] id=" + m.group("id")[:30] + "\n")
    out.write("       title=" + m.group("title")[:50] + "\n")
    out.write("       slug=" + m.group("slug") + "\n")
    out.write("       ep=" + m.group("episode_count") + "/" + m.group("total_episode") + "\n")
    out.write("       views=" + m.group("view_count") + "\n")
    out.write("       isHot=" + m.group("is_hot") + ", isFeatured=" + m.group("is_featured") + "\n")
    out.write("       full: " + m.group(0)[:200] + "\n")

# Also try without description
MOVIE_RE2 = re.compile(
    r'\{"id":"(?P<id>[^"]+)","title":"(?P<title>[^"]*)","slug":"(?P<slug>[^"]+)"',
    re.DOTALL
)
matches2 = list(MOVIE_RE2.finditer(text))
out.write("\nSimple id+title+slug regex matches: " + str(len(matches2)) + "\n")
for i, m in enumerate(matches2[:3]):
    out.write("  [" + str(i) + "] " + m.group(0)[:200] + "\n")

out.close()
print("Done")
