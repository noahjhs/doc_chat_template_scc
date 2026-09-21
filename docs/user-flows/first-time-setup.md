# First-time setup

A brand-new user, on a machine that has never run Casper before, getting
from the landing page to a paired, working daemon.

## Preconditions

- A real Mac (not the dev machine that built the binary, if avoidable —
  code-signing/notarization/Gatekeeper problems are exactly the kind of
  thing that only shows up on a machine that didn't just build the thing).
- No `~/Library/Application Support/Casper` directory on it yet (delete it
  first if this machine has run Casper before — otherwise this is testing
  a *returning* user, not a new one; see `harness pair-daemon`'s own docs
  for why a real re-pair test is a separate, narrower thing).
- A fresh account with no existing paired hosts (`harness signup <username>
  --dev`, don't reuse an existing test account).
- `{WWW_ENV}`/`{APP_ENV}` below are `dev-www.casperagent.dev`/
  `dev-app.casperagent.dev` for a dev pass, or `www.casperagent.dev`/
  `app.casperagent.dev` for a prod pass (see the directory README — dev
  first, always). **Two different subdomains, not one** — the landing
  page and `/download` are www-subdomain-gated
  (`utils/auth.py`'s `require_www_subdomain()`), sign-in/sign-up are
  app-subdomain-gated (`require_app_subdomain()`); hitting either from the
  wrong one renders a plain "Page not found." (a real `st.stop()`, not an
  HTTP 404 — `curl`'s own 200 status code doesn't catch this, confirmed
  directly 2026-09-20 when this doc's own single-`{ENV}` version sent a
  live run-through to the wrong subdomain for step 1). The actual
  sign-in/sign-up **links** from the landing/download pages already use
  the correct subdomain (`app_subdomain_url()`) — this was only ever a bug
  in this doc's own instructions, not in the product.

## Steps

- [x] 1. Visit `https://{WWW_ENV}/`.
      **Expected:** the landing page loads, states what Casper does, and
      has a "Download Casper" link.
      **2026-09-20, dev, real confirmed** (after correcting the subdomain
      mistake above, hit live mid-run).
- [x] 2. Click through to `/download`.
      **Expected:** the download page loads with a numbered
      "Getting started" list and a platform download button.
      **2026-09-20, dev, real confirmed.**
- [x] 3. Download the Mac build.
      **Expected:** a `Casper-macos.zip` downloads.
      **2026-09-20, dev, real confirmed.**
- [x] 4. Unzip it, open `Casper.app` for the first time.
      **Expected:** some first-run confirmation, no Terminal/`xattr`
      needed.
      **2026-09-20, dev, real confirmed — simpler than the doc/copy even
      guessed.** The actual dialog: "This application was downloaded from
      the internet — Open / Cancel." A single click on **Open**, no
      right-click-to-bypass trick needed at all (unlike the *previous*
      guess, corrected here too — `pages/download.py`'s copy simplified to
      match: just "click Open," no numbered workaround). Confirms the
      2026-09-19 finding (Casper is properly notarized+stapled) was
      right, and narrows exactly how simple that makes this in practice.
- [x] 5. After it opens: confirm the 👻 status-bar icon appears, and that
      **no** workspace/folder picker appears (Phase 3 retired that — the
      daemon confines itself to the home directory automatically now).
      **Expected:** icon appears; no picker; a native "Add Casper to your
      login items?" dialog appears once (first cold launch only).
      **2026-09-20, dev, real confirmed** (login items dialog explicitly
      observed; no picker reported).
- [x] 6. Back on the landing/download page, click **Sign up** (a genuinely
      new account, per this flow's own precondition).
      **Expected:** opens `pages/signup.py` on the app subdomain in a new
      tab, with a username/password form.
      **2026-09-20, dev, real confirmed** — first real run-through since
      the 2026-09-19 dead-link fix.
- [x] 7. Submit the form.
      **Expected:** on success, the page shows "Signed up as
      &lt;username&gt;." and the browser hands `Casper.app` a
      `casper://pair?token=...&username=...` URL (an OS-level hand-off,
      not a visible browser action) — the already-running app receives it
      with no further clicks. The page then shows a "Waiting for Casper to
      connect..." spinner rather than immediately settling into its final
      text.
      **2026-09-20, dev, real confirmed** — daemon's own `command_log.txt`
      independently confirms the pairing (`Paired as <username>`) at the
      same moment.
- [x] 8. Confirm pairing actually succeeded.
      **Expected:** within ~12 seconds, the page polls `GET /hosts` for a
      host that became connected *after* the pairing hand-off fired and
      swaps the spinner for "Casper connected. You can close this tab."
      **2026-09-20, dev, real confirmed** — spinner, then "Casper
      connected," reported directly by a live run. First real
      confirmation of the 2026-09-20 pairing-confirmation fix end to end,
      closing out the "no end-user-facing UI" gap for real (not just via
      `AppTest`/direct-API checks as before).

## Pass/fail

**2026-09-20 dev run: full pass, 8/8 steps confirmed live** — every step's
actual behavior captured, all three previously-open gaps (dead sign-in
link, no pairing confirmation, stale Gatekeeper copy) either already fixed
or fixed live during this run (the `{ENV}` subdomain-conflation bug,
found mid-run — see Preconditions). Prod hasn't had a run-through with
these fixes yet; do that before/as part of promoting this work to prod.

## Known issues

- ~~**Dead "Sign in" link from `/download`**~~ **Fixed 2026-09-19,
  deployed to dev.** `pages/signin.py`/`signup.py` were deleted in
  `3491faf` ("Retire the authenticated Streamlit surface") without
  updating `pages/download.py`'s link, which depended on them. Restored
  both pages (trimmed: no more cross-tab localStorage session recovery or
  `/environments` redirect, since that page is gone for good — success now
  just fires the `casper://pair` hand-off and stops) plus the
  `utils/auth.py`/`utils/browser_nav.py` helpers they need. Verified: the
  pages load and execute without exception (`streamlit.testing.v1.
  AppTest`); `https://dev-app.casperagent.dev/signin` and `/signup` both
  serve real 200s post-deploy; `signup_with_auth_service` called directly
  against the live dev `casper_service` returns a real token and
  `build_pair_url` forms a correct `casper://pair?...` URL from it — i.e.
  every piece of the page's own logic has been exercised against the real
  live deployment. **Confirmed for real 2026-09-20** — a live run-through
  (form submit → `casper://pair` Apple Event → a real daemon receiving
  it) worked end to end; see step 6/7.
- ~~**No end-user-facing pairing confirmation**~~ **Fixed 2026-09-20,
  confirmed live the same day** (found 2026-09-19 — see step 8).
  `pages/signin.py`/`signup.py` now poll `GET /hosts` for up to ~12s after
  firing the `casper://pair` hand-off and show a real "Casper connected"
  confirmation once a NEW host shows connected (new `utils/auth.py`
  helper, `connected_host_ids`) — falling back quietly to the original
  generic message on timeout, never claiming failure. A live run reported
  the spinner, then "Casper connected," with the daemon's own log
  independently confirming the pairing at the same moment.
- ~~**Gatekeeper warning copy may be stale**~~ **Fixed 2026-09-20, confirmed
  live the same day** (flagged 2026-09-19 — see step 4). It was worse
  than stale copy: `pages/download.py` actively told users Casper wasn't
  notarized and to run `xattr -cr` in Terminal, both wrong since
  2026-09-19's deploy. A live run showed the real dialog is simpler than
  even the first fix guessed — a plain "downloaded from the internet,
  Open/Cancel" confirmation, no right-click bypass needed — so the copy
  was simplified further to match exactly what's actually seen.
- ~~**This doc's own `{ENV}` conflated two different subdomains**~~
  **Found and fixed 2026-09-20, live, mid-run.** The landing page and
  `/download` are www-subdomain-gated; sign-in/sign-up are
  app-subdomain-gated (`utils/auth.py`'s `require_www_subdomain()`/
  `require_app_subdomain()`) — this doc's single `{ENV}` = the app
  subdomain sent step 1 to the wrong one, rendering a plain "Page not
  found." (a `st.stop()`, not an HTTP 404, so `smoke_test.sh`'s own
  status-code-only checks never would have caught this either — worth
  keeping in mind, not fixing here). The product's own sign-in/sign-up
  **links** were never wrong, only this doc's instructions were — see
  Preconditions for the corrected `{WWW_ENV}`/`{APP_ENV}` split.
