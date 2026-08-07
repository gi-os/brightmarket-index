# Submission portal Worker

The only server-side component in BrightMarket. It exists for exactly one
reason: GitHub's OAuth code→token exchange needs a `client_secret`, and a static
GitHub Pages site cannot hold one. Everything else stays client-side.

## Two credentials, never mixed

**The submitter's token** — minted per login, scope `read:user` only. Enough to
read their username and list the public repos they own; it cannot write anything
anywhere. It is used once inside `/exchange` and then discarded: never returned
to the browser, never logged, never stored.

**`SUBMIT_PAT`** — a fine-grained PAT belonging to gi-os, scoped to
`brightmarket-index` alone with `Issues: write` and nothing else. This is the
only credential that can file the submission issue. The browser never sees it.
If it leaked, the blast radius is "someone can open issues on one repo."

## Why the session is signed rather than stored

`/exchange` returns an HMAC-SHA256 signed payload holding the login and the
verified list of owned repos — no KV, no database, 10-minute TTL. `/submit` then
re-checks the requested repo against that **signed** list, so a request forged in
devtools cannot submit a repo the user doesn't own. Verified: a tampered repo
list, a forged signature, a malformed token and an expired token are all
rejected.

## Deploying

```bash
wrangler secret put OAUTH_CLIENT_ID       # Ov23liUz650T0P6VGU5B
wrangler secret put OAUTH_CLIENT_SECRET   # from the OAuth App, shown once
wrangler secret put SESSION_SECRET        # openssl rand -hex 32
wrangler secret put SUBMIT_PAT            # fine-grained PAT, see above
wrangler deploy
```

Then put the deployed URL into `WORKER_URL` at the top of `../submit.html`.
