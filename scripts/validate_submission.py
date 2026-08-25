#!/usr/bin/env python3
"""Validate a submission issue and, if it passes, write it into apps/.

Run by .github/workflows/submit.yml on every issue labelled `submission`.
On pass it writes the app's own file under apps/ and prints a PR body; on fail it prints the
reason, which the workflow posts back as an issue comment.

The checks exist for specific reasons, not as generic hygiene:

* exactly one .apk per release -- two assets is precisely how an updater ends up
  installing a debug build (different signing cert) over the release one, which
  surfaces to the user as an opaque "Failure: Invalid".
* tag shape -- versionCode is derived from the tag's trailing segment, and the
  client compares it against PackageManager to decide "update available". A repo
  that doesn't follow the convention would be indexed with a wrong version
  forever, which is worse than being rejected.
* applicationId uniqueness -- Android identifies an app by (applicationId, cert).
  Two index entries claiming one applicationId means the store would offer to
  "update" one app with a completely different one.
"""

import os
import re
import sys
import json
import zipfile
import io
import urllib.request
import urllib.error

import yaml

API = "https://api.github.com"
HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "brightmarket-validator",
}
if os.environ.get("GH_TOKEN"):
    HEADERS["Authorization"] = f"Bearer {os.environ['GH_TOKEN']}"

VALID_CATEGORIES = {"reading", "utilities", "games", "media", "productivity", "hardware"}

# The display name is free text from the issue body, and it does not stay text:
# it is written into a YAML file, into a PR title, and into a `git commit -m`
# in a job holding contents:write. Unlike `category` it had nothing checking it
# at all, so `$(...)` in a Name field executed in CI. Letters (any script),
# digits, spaces, dots and hyphens -- everything a real app is called, and
# nothing a shell or a YAML parser treats as syntax.
NAME_RE = re.compile(r"[\w .\-]{1,60}", re.UNICODE)

# The summary is only ever data, but it is data written into YAML that the
# published index carries, so it gets a length bound rather than a shape.
SUMMARY_MAX = 200

# Refuse to pull an arbitrarily large file into memory over an unauthenticated
# URL. The releases API already told us how big the asset is, so the check is
# free; the read is bounded as well, because the size field is the submitter's
# server's word rather than a fact.
MAX_APK_BYTES = 200 * 1024 * 1024


class Reject(Exception):
    pass


def get(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def field(body: str, label: str) -> str:
    """Read one `**Label:** value` line written by the portal.

    Anchored to the start of a line and stopping at the end of it, because the
    portal strips newlines out of free text for exactly this reason -- a summary
    that could contain its own `**Category:**` line would otherwise be able to
    set a field the submitter never chose.
    """
    m = re.search(rf"^\s*\*\*{label}:\*\*\s*(.+?)\s*$", body, re.M | re.I)
    return m.group(1).strip() if m else ""


ADB_MODES = {"allow", "deny", "ignore", "default"}

_PKG = r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+"
_CLS = r"[A-Za-z0-9_.$]+"

ADB_FORMS = (
    re.compile(rf"^pm grant ({_PKG}) ([A-Za-z0-9_.]+)$"),
    re.compile(rf"^appops set ({_PKG}) ([A-Za-z0-9_]+) ([A-Za-z]+)$"),
    re.compile(rf"^cmd notification allow_listener ({_PKG})/({_CLS})$"),
    re.compile(rf"^(?:enable )?accessibility(?: service)? ({_PKG})/({_CLS})$", re.I),
)

# The one form that names no package, so it is checked separately below.
#
# Everything above is "do X to yourself" and is validated by comparing a package name. This is
# "start somebody else's service", so there is no package to compare and the safety comes from
# the shape instead: it is a verb, not a command. BrightControl writes the actual shell line, and
# a request carrying a path, an argument or a second command does not match this and is refused.
SHIZUKU_FORM = re.compile(r"^(?:start|run) shizuku$", re.I)


def normalize_adb(line: str) -> str:
    """Strip the parts of a README line that are about running it from a computer.

    `adb shell pm grant ...` and `pm grant ...` are the same request, and so is a line pasted
    with a `$` prompt or wrapped in backticks. Whitespace collapses first: a line copied out of
    a README can carry `adb  shell` with two spaces, and peeling prefixes before normalising the
    spacing leaves a stray `shell` on the front.
    """
    s = " ".join(line.split())
    # Peeled until nothing changes, rather than in one pass: the decorations nest and interleave
    # in whatever order the person pasting them happened to produce -- `` `adb shell ...`; ``
    # has a semicolon outside the backticks, so stripping backticks once leaves both.
    while True:
        before = s
        s = s.strip().strip("`").strip().rstrip(";").strip()
        if s == "$":
            return ""
        for prefix in ("$ ", "adb ", "shell "):
            if s.startswith(prefix):
                s = s[len(prefix):]
                break
        s = " ".join(s.split())
        if s == before:
            return s


def parse_adb(raw: str, pkg: str) -> list[str]:
    """The ADB setup a submitter asked for, checked against the app they actually shipped.

    Two gates, and the second is the one that matters:

    1. **A known shape.** Only a permission, an app op, a notification listener or an
       accessibility service. Anything else is refused rather than carried, so the catalogue
       cannot come to hold `pm install` or a `settings put` that overwrites a system-wide value.
    2. **Their own package, checked against the APK.** `pkg` here was read out of the uploaded
       APK, not typed into the form, so a submitter cannot ask for a grant on somebody else's
       app -- which is the whole attack this field would otherwise open. BrightControl checks
       this again before running anything, but a bad line should never reach the catalogue in
       the first place.

    Commas separate commands, which is what the portal asks for. A command in any of the forms
    above cannot contain one.
    """
    out: list[str] = []
    for piece in raw.split(","):
        line = normalize_adb(piece)
        if not line:
            continue
        if SHIZUKU_FORM.match(line):
            # Normalised to one spelling so the index carries a verb rather than whichever of
            # "run shizuku" / "Start Shizuku" the README happened to use.
            out.append("start shizuku")
            continue
        for form in ADB_FORMS:
            m = form.match(line)
            if not m:
                continue
            if m.group(1) != pkg:
                raise Reject(
                    f"`{piece.strip()}` names `{m.group(1)}`, but this submission is for "
                    f"`{pkg}`. An app can only ask for setup on itself."
                )
            if form is ADB_FORMS[1] and m.group(3).lower() not in ADB_MODES:
                raise Reject(
                    f"`{piece.strip()}` — an app op mode has to be one of: "
                    f"{', '.join(sorted(ADB_MODES))}."
                )
            out.append(line if line.startswith("accessibility") else f"adb shell {line}")
            break
        else:
            raise Reject(
                f"I can't accept `{piece.strip()}`. ADB setup can only be a permission "
                f"(`pm grant`), an app op (`appops set`), a notification listener "
                f"(`cmd notification allow_listener`), an accessibility service "
                f"(`accessibility {pkg}/.YourService`) or `start shizuku`. Anything else has "
                f"to be done by hand."
            )
    # Order is kept as written; duplicates are not, since running one twice does nothing.
    seen, unique = set(), []
    for c in out:
        if c not in seen:
            seen.add(c)
            unique.append(c)
    return unique


def parse_issue(body: str) -> dict:
    """Pull the request out of either the issue-form rendering or the portal's
    plain `**Repo:** <url>` format.

    Returns an action as well as a repo now: the portal can ask to edit or
    remove a listing, not only to add one, and all three arrive as issues on
    this repo so they go through the same checks and leave the same trail.
    """
    repo_m = re.search(r"github\.com/([\w.-]+/[\w.-]+)", body) or re.search(
        r"^\s*([\w.-]+/[\w.-]+)\s*$", body, re.M
    )
    if not repo_m:
        raise Reject("I couldn't find a repo in this issue. Give me `owner/name` or a GitHub URL.")

    action = (field(body, "Action") or "submit").lower()
    if action not in ("submit", "edit", "remove"):
        raise Reject(f"Unknown action `{action}`.")

    # Prefer the explicit field; fall back to finding the word anywhere, which
    # is what the hand-written issue form produces.
    category = field(body, "Category").lower()
    if category and category not in VALID_CATEGORIES:
        raise Reject(f"`{category}` isn't a category. Pick one of: {', '.join(sorted(VALID_CATEGORIES))}.")
    if not category:
        cat_m = re.search(r"(reading|utilities|games|media|productivity|hardware)", body, re.I)
        category = cat_m.group(1).lower() if cat_m else ""
    if action == "submit" and not category:
        raise Reject(f"No category found. Pick one of: {', '.join(sorted(VALID_CATEGORIES))}.")

    # Both are optional -- a submission that names neither falls back to the
    # repo's own name and description -- so an empty one is not a rejection.
    # A present one has to be something an app is plausibly called.
    name = field(body, "Name")
    if name and not NAME_RE.fullmatch(name):
        raise Reject(
            f"`{name}` isn't usable as an app name. Names can be up to 60 characters of "
            "letters, digits, spaces, dots, underscores and hyphens — nothing else."
        )

    summary = field(body, "Summary")
    if len(summary) > SUMMARY_MAX:
        raise Reject(
            f"That summary is {len(summary)} characters. Keep it under {SUMMARY_MAX} — it has "
            "to fit on a phone screen."
        )

    return {
        "action": action,
        "repo": repo_m.group(1).removesuffix(".git"),
        "category": category,
        "name": name,
        "summary": summary,
        "adb": field(body, "ADB"),
    }


def validate(repo: str) -> dict:
    try:
        meta = get(f"{API}/repos/{repo}")
    except urllib.error.HTTPError as e:
        raise Reject(f"Can't read `{repo}` — is it public? (GitHub said {e.code}.)")

    if meta.get("private"):
        raise Reject(f"`{repo}` is private. BrightMarket only indexes public repos.")
    if meta.get("archived"):
        raise Reject(f"`{repo}` is archived, so it can't ship updates.")

    releases = [r for r in get(f"{API}/repos/{repo}/releases?per_page=100")
                if not r["draft"] and not r["prerelease"]]
    if not releases:
        raise Reject(
            f"`{repo}` has no published release. Cut a release with one signed `.apk` "
            "attached, then reopen this issue."
        )

    latest = releases[0]
    apks = [a for a in latest["assets"] if a["name"].endswith(".apk")]
    if not apks:
        raise Reject(f"The latest release (`{latest['tag_name']}`) has no `.apk` attached.")
    if len(apks) > 1:
        names = ", ".join(f"`{a['name']}`" for a in apks)
        raise Reject(
            f"The latest release has {len(apks)} `.apk` assets ({names}). Attach exactly one — "
            "a debug APK alongside the release one is how an updater installs the wrong "
            "signature and users get `Failure: Invalid`."
        )

    # Read the applicationId straight out of the APK the user is actually shipping,
    # rather than trusting anything they typed. AndroidManifest.xml inside an APK is
    # binary XML, but the applicationId appears verbatim in the string pool, so a
    # targeted scan avoids needing aapt2 (which is x86_64-only and awkward in CI).
    size = apks[0].get("size") or 0
    if size > MAX_APK_BYTES:
        raise Reject(
            f"`{apks[0]['name']}` is {size // (1024 * 1024)} MB. BrightMarket won't index an "
            f"APK over {MAX_APK_BYTES // (1024 * 1024)} MB — it has to download onto a phone."
        )
    req = urllib.request.Request(apks[0]["browser_download_url"], headers=HEADERS)
    with urllib.request.urlopen(req, timeout=300) as r:
        # One byte past the cap, so a release whose asset metadata understates the
        # real file still cannot fill the runner's memory.
        blob = r.read(MAX_APK_BYTES + 1)
    if len(blob) > MAX_APK_BYTES:
        raise Reject(
            f"`{apks[0]['name']}` is larger than {MAX_APK_BYTES // (1024 * 1024)} MB. "
            "BrightMarket won't index an APK that big — it has to download onto a phone."
        )
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            manifest = z.read("AndroidManifest.xml")
    except (zipfile.BadZipFile, KeyError):
        raise Reject("That `.apk` isn't a readable Android package.")

    # Parse the binary manifest properly. The old approach scanned the string
    # pool for dotted identifiers and took the most frequent one that wasn't
    # android./androidx./etc -- which is wrong for any app derived from sample
    # code. The first real outside submission was a whoBIRD fork built on the
    # TensorFlow Lite sound-classifier example, and
    # `org.tensorflow.lite.examples.soundclassifier` appears throughout it far
    # more often than its actual applicationId does. The count picked the
    # ancestor.
    #
    # That is not a cosmetic mistake: pkg is the key the published index is
    # stored under and the one the client compares against PackageManager, so a
    # wrong one means the app never registers as installed and its signing
    # certificate never pins.
    app_id = None
    version_code = None
    try:
        import tempfile
        from pyaxmlparser import APK
        with tempfile.NamedTemporaryFile(suffix=".apk", delete=False) as f:
            f.write(blob)
            tmp = f.name
        try:
            apk = APK(tmp)
            app_id = apk.package or None
            version_code = int(apk.version_code)
        finally:
            os.unlink(tmp)
    except Exception:
        app_id = None

    # There used to be a check here that the tag ended in a number, on the
    # grounds that versionCode came from the tag's last segment. It hasn't since
    # the index builder started reading it out of the APK -- and the check was
    # rejecting shapes the builder handles perfectly well. BrightLibrary has been
    # in the catalogue for days under the tag `build-58`; BrightMusic was turned
    # away for `build-65`, the identical shape.
    #
    # The APK is the authority, as it is everywhere else. The tag is only
    # consulted when the APK can't be parsed at all, and then only to establish
    # that *something* here is a number that can move forwards.
    if version_code is None:
        try:
            version_code = int(latest["tag_name"].lstrip("v").rsplit(".", 1)[-1])
        except ValueError:
            raise Reject(
                f"I couldn't read a versionCode out of that APK, and the tag "
                f"`{latest['tag_name']}` doesn't end in a number to fall back on. "
                "Android needs a versionCode that increases every release or it "
                "refuses the update."
            )

    if not app_id:
        # Kept only as a fallback for the case where the parser itself fails.
        # Still a guess, and still capable of being wrong in the same way.
        text = manifest.decode("utf-16-le", errors="ignore") + manifest.decode("latin-1", errors="ignore")
        candidates = re.findall(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){2,}", text)
        ignore = ("android.", "androidx.", "com.google.", "kotlin.", "java.")
        pkgs = [c for c in candidates if not c.startswith(ignore)]
        if not pkgs:
            raise Reject("Couldn't read an applicationId out of that APK's manifest.")
        app_id = max(set(pkgs), key=pkgs.count)

    return {
        "pkg": app_id,
        "repo": repo,
        "name": meta["name"],
        "summary": (meta.get("description") or "").strip(),
        "version": latest["tag_name"],
        "versionCode": version_code,
        "apk": apks[0]["name"],
    }


def write_entry(path: str, entry: dict) -> None:
    with open(path, "w") as f:
        yaml.safe_dump(entry, f, sort_keys=False, default_flow_style=False, allow_unicode=True)


def slug(pkg: str) -> str:
    """Filename for an app. The applicationId, which is already unique here."""
    return re.sub(r"[^a-z0-9.]+", "-", pkg.lower()) + ".yml"


def load_catalogue(apps_dir: str) -> list[tuple[str, dict]]:
    """Every listed app as (path, entry)."""
    out = []
    for name in sorted(os.listdir(apps_dir)):
        if not name.endswith((".yml", ".yaml")):
            continue
        path = os.path.join(apps_dir, name)
        with open(path) as f:
            entry = yaml.safe_load(f)
        if entry and "pkg" in entry:
            out.append((path, entry))
    return out


def main() -> int:
    body = os.environ.get("ISSUE_BODY", "")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # One file per app, so two submissions never write the same bytes and can
    # never conflict with each other. This was a single apps.yml with one list;
    # every submission appended to its last line, so whichever was merged second
    # always conflicted, no matter how carefully either was written.
    apps_dir = os.path.join(root, "apps")

    try:
        req = parse_issue(body)
        repo, action = req["repo"], req["action"]

        catalogue = load_catalogue(apps_dir)
        existing = [entry for _, entry in catalogue]
        path_of = {id(entry): path for path, entry in catalogue}

        if action == "remove":
            match = next((a for a in existing if a["repo"].lower() == repo.lower()), None)
            if not match:
                raise Reject(f"`{repo}` isn't in BrightMarket, so there's nothing to remove.")
            os.remove(path_of[id(match)])
            summary = f"Removed **{match['name']}** (`{match['pkg']}`) at the owner's request."
            out_pkg, out_name = match["pkg"], match["name"]

        elif action == "edit":
            match = next((a for a in existing if a["repo"].lower() == repo.lower()), None)
            if not match:
                raise Reject(f"`{repo}` isn't in BrightMarket yet — submit it first.")
            changed = []
            for key in ("name", "summary", "category"):
                value = req.get(key)
                if value and value != match.get(key):
                    changed.append(f"{key}: `{match.get(key)}` → `{value}`")
                    match[key] = value
            if req.get("adb"):
                adb = parse_adb(req["adb"], match["pkg"])
                if adb != match.get("adb", []):
                    changed.append(f"adb: {len(adb)} command(s)")
                    match["adb"] = adb
            if not changed:
                raise Reject("Nothing in that request differs from what's already listed.")
            summary = f"Updated **{match['name']}** — " + "; ".join(changed)
            out_pkg, out_name = match["pkg"], match["name"]

        else:
            # A new entry is the only action that has to touch the network: it
            # is the only one making a claim about a repo we have never checked.
            info = validate(repo)
            for a in existing:
                if a["repo"].lower() == repo.lower():
                    raise Reject(f"`{repo}` is already in BrightMarket.")
                if a["pkg"] == info["pkg"]:
                    raise Reject(
                        f"Package `{info['pkg']}` is already indexed as **{a['name']}** "
                        f"(`{a['repo']}`). Two apps can't share an applicationId."
                    )
            entry = {
                "pkg": info["pkg"],
                # What the submitter asked to be called, falling back to the
                # repo's own name and description when they said nothing.
                "name": req["name"] or info["name"],
                "repo": repo,
                "category": req["category"],
                "summary": req["summary"] or info["summary"],
            }
            # Checked against the applicationId read out of the APK, so a submitter can only
            # ask for grants on the app they actually shipped.
            adb = parse_adb(req["adb"], info["pkg"])
            if adb:
                entry["adb"] = adb
            write_entry(os.path.join(apps_dir, slug(info["pkg"])), entry)
            summary = (
                f"Validated **{req['name'] or info['name']}** (`{info['pkg']}`) — latest "
                f"release `{info['version']}`, one asset `{info['apk']}`."
            )
            out_pkg, out_name = info["pkg"], req["name"] or info["name"]

        # An edit rewrites only its own file; a new listing and a removal have
        # already written or deleted theirs.
        if action == "edit":
            write_entry(path_of[id(match)], match)

        with open(os.environ.get("GITHUB_OUTPUT", "/dev/null"), "a") as f:
            f.write("status=pass\n")
            f.write(f"action={action}\n")
            f.write(f"pkg={out_pkg}\n")
            f.write(f"name={out_name}\n")
        print(summary)
        return 0

    except Reject as e:
        with open(os.environ.get("GITHUB_OUTPUT", "/dev/null"), "a") as f:
            f.write("status=fail\n")
            # Multi-line safe: GitHub Actions heredoc form.
            f.write(f"reason<<EOF\n{e}\nEOF\n")
        print(str(e), file=sys.stderr)
        return 0  # the workflow reports this as a comment, not as a red run


if __name__ == "__main__":
    sys.exit(main())
