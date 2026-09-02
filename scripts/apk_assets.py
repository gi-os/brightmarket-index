"""Which `.apk` on a release is the one to index.

Both the validator and the index builder used to refuse any release carrying more
than one `.apk`, on the grounds that a debug build sitting next to the release one
is how an updater installs the wrong signing certificate and the user gets an
opaque `Failure: Invalid`.

That reasoning is right about the danger and wrong about the remedy. The danger is
*ambiguity* -- two candidates and no rule for choosing -- not the presence of a
second file. A release holding `tide-debug.apk` and `tide-release.apk` is not
ambiguous to a human for one second, and it is not ambiguous here either: one of
those names says what it is. KEZO555/Tide was turned away for a release that had a
perfectly good `tide-release.apk` on it (submission #112), which is a rejection
that taught the submitter nothing except that the store is fussy.

So: name the variants that are never the thing to install, drop them, and require
that exactly one candidate is left. Ambiguity is still refused -- per-ABI splits
(`app-arm64-v8a.apk`, `app-armeabi-v7a.apk`) reach the same dead end they always
did, because there really is no way to pick between those without knowing the
phone. What changes is only the case where the release itself has already said
which file is which.

The choice has to be made identically in both scripts. The validator decides
whether a submission is admitted; build_index.py decides, every hour forever,
which bytes the phone downloads. If those two disagreed, an app would pass its
submission and then be skipped by every build after it -- listed and permanently
un-installable, which is worse than a clean rejection. Hence one module, imported
by both, rather than the same three lines written twice.
"""

import re

# Matched against the filename with separators normalised away, so `tide-debug.apk`,
# `tide_debug.apk`, `TideDebug.apk` and `tide.debug.apk` are all the same word.
#
# `test` covers `app-androidTest.apk`, which Gradle emits next to a debug build and
# which is not an app at all -- it is the instrumentation harness.
NEVER_INSTALL = ("debug", "unsigned", "androidtest", "test")


def _normalise(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def apk_assets(release: dict) -> list[dict]:
    """Every `.apk` on a release, in the order GitHub returned them."""
    return [a for a in release.get("assets", []) if a["name"].lower().endswith(".apk")]


def pick_apk(release: dict) -> tuple[dict | None, str]:
    """The one `.apk` to index, or `(None, reason)` saying why there isn't one.

    The reason is written to be read by the person who filed the submission, so it
    names the files it saw and says what to do about them.
    """
    apks = apk_assets(release)
    tag = release.get("tag_name", "?")

    if not apks:
        return None, f"The latest release (`{tag}`) has no `.apk` attached."

    if len(apks) == 1:
        return apks[0], ""

    installable = [
        a for a in apks
        if not any(word in _normalise(a["name"]) for word in NEVER_INSTALL)
    ]
    names = ", ".join(f"`{a['name']}`" for a in apks)

    if len(installable) == 1:
        return installable[0], ""

    if not installable:
        return None, (
            f"The latest release (`{tag}`) has {len(apks)} `.apk` assets ({names}), and every "
            "one of them looks like a debug or test build. Attach the release APK — a debug "
            "build is signed with a different certificate, so installing it and then updating "
            "to a real release fails with `Failure: Invalid`."
        )

    remaining = ", ".join(f"`{a['name']}`" for a in installable)
    return None, (
        f"The latest release (`{tag}`) has {len(apks)} `.apk` assets ({names}), and I can't "
        f"tell which one to install — {remaining} all look like release builds. If those are "
        "per-architecture splits, attach a single universal APK instead; BrightMarket hands the "
        "phone one file and can't choose an ABI for it."
    )
