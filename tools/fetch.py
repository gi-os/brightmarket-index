import json, os, sys, urllib.request
repo, dest = sys.argv[1], sys.argv[2]
h = {"Authorization": f"Bearer {os.environ['GH_TOKEN']}", "Accept": "application/vnd.github+json"}
def get(u):
    return json.load(urllib.request.urlopen(urllib.request.Request(u, headers=h)))
rels = get(f"https://api.github.com/repos/gi-os/{repo}/releases?per_page=20")
# Prefer the newest non-prerelease, else anything; skip debug builds.
for want_stable in (True, False):
    for r in rels:
        if want_stable and (r["prerelease"] or r["draft"]):
            continue
        apks = [a for a in r["assets"] if a["name"].endswith(".apk") and "debug" not in a["name"].lower()]
        if apks:
            a = apks[0]
            print(repo, r["tag_name"], a["name"])
            urllib.request.urlretrieve(a["browser_download_url"], dest)
            open("out/version.txt", "w").write(f"{r['tag_name']} {a['name']}\n")
            sys.exit(0)
sys.exit(f"no apk for {repo}")
