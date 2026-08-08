# brightmarket-index

The database behind [BrightMarket](https://github.com/gi-os/BrightMarket). There is
no server: this repo *is* the backend.

- **`apps/`** — the curated list, one YAML file per app named after its
  applicationId. One file each rather than one list: every submission used to
  append to the end of a single `apps.yml`, so any two open at once modified the
  same line and whichever was merged second always conflicted. Separate files
  cannot collide, so merge order stops mattering.
- **`index-v1.json`** — rebuilt by CI from `apps/` + the GitHub Releases
  API, and served from GitHub Pages. This is the single file the app fetches.
- **`submit.html`** — the submission portal (GitHub OAuth, read-only).

## How an app gets in

Either open a [submission issue](../../issues/new?template=submit-app.yml), or use
the [portal](https://brightmarket.gzl.dev/submit.html), which signs
you in with GitHub and only lets you pick repos you actually own.

Either way `scripts/validate_submission.py` runs and checks the repo is public and
unarchived, has a published release with **exactly one** `.apk`, has a tag ending in
a number, and isn't already indexed under that applicationId. It reads the
applicationId out of the APK itself rather than trusting the submission. Pass opens
a PR; fail comments the reason and closes the issue so it can be reopened to retry.

## Why the sorts need no analytics

`downloads` is summed from GitHub's own `download_count` across every release, so
"most downloaded" costs nothing and tracks nobody. `firstSeen` (New) and
`latest.published` (Updated) come from the index and the API. BrightMarket never
sees a user.

## Two things that must not be "tidied"

**`pkg` still reads `light*` for older apps.** That's the Android applicationId and
it is the app's permanent identity. Changing one isn't an update — it's a second app
installed alongside the first, and every user loses their data. `name` is what people
see and is free to differ.

**`firstSeen` and the signer pin are carried forward from the previous index,
never recomputed.** Recomputing would reset the New sort every hour, and would make
the signer pin meaningless — anyone able to push one release could clear it.
