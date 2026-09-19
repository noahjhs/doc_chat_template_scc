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
- `{ENV}` below is `dev-app.casperagent.dev` for a dev pass, or
  `app.casperagent.dev` for a prod pass (see the directory README — dev
  first, always).

## Steps

- [ ] 1. Visit `https://{ENV}/`.
      **Expected:** the landing page loads, states what Casper does, and
      has a "Download Casper" link.
- [ ] 2. Click through to `/download`.
      **Expected:** the download page loads with a numbered
      "Getting started" list and a platform download button.
- [ ] 3. Download the Mac build.
      **Expected:** a `Casper-macos.zip` downloads.
- [ ] 4. Unzip it, open `Casper.app` for the first time (double-click).
      **Expected:** per the page's own warning, macOS Gatekeeper blocks it
      the first time ("Apple could not verify..."). Follow the documented
      `xattr -cr` workaround, then reopen.
      **⚠ Verify this expectation is still accurate** — the current build
      pipeline (`build/build_go_macos.sh`) signs *and notarizes* the
      binary (confirmed directly during the 2026-09-19 dev deploy: a real
      `xcrun notarytool submit ... --wait` succeeded and the ticket was
      stapled). A properly notarized, stapled app is not usually the
      "could not verify" block at all — it's typically the milder "are you
      sure you want to open an app downloaded from the internet?" prompt
      with a plain **Open** button, no Terminal/`xattr` needed. If that's
      what actually happens, the download page's warning is stale and
      should be simplified or removed — note the actual behavior observed
      here either way.
- [ ] 5. After it opens: confirm the 👻 status-bar icon appears, and that
      **no** workspace/folder picker appears (Phase 3 retired that — the
      daemon confines itself to the home directory automatically now).
      **Expected:** icon appears; no picker; a native "Add Casper to your
      login items?" dialog appears once (first cold launch only).
- [ ] 6. Back on the download page, click **Sign in** (or **Sign up**, for
      a genuinely new account).
      **Expected:** opens `pages/signin.py`/`pages/signup.py` on the app
      subdomain in a new tab, with a username/password form.
      **Fixed 2026-09-19** — was 404ing (see Known Issues). Re-verify this
      step specifically the next time this flow runs; it hasn't had a real
      run-through since the fix, only `AppTest`-level (no live network)
      checks that the page renders without an exception.
- [ ] 7. Submit the form.
      **Expected:** on success, the page shows "Signed in as
      &lt;username&gt;." and the browser hands `Casper.app` a
      `casper://pair?token=...&username=...` URL (an OS-level hand-off,
      not a visible browser action) — the already-running app receives it
      with no further clicks. No further page navigation happens (there's
      no post-pairing page to send you to anymore) — the page's own text
      says to check your computer and close the tab.
- [ ] 8. Confirm pairing actually succeeded.
      **Expected:** ⚠ there is currently no end-user-facing UI that shows
      this (the authenticated Streamlit surface — Environments/hosts list
      — was retired in favor of `harness`, and no replacement front end
      exists yet). Verify via an operator/dev path instead: `harness login
      <the account> --{env}` then `harness hosts list` should show the new
      host `connected: true`. Note this gap explicitly when reporting
      results — it means an actual end user currently has no way to know
      pairing worked.

## Pass/fail

Pass = every step's *actual* behavior is captured (not just "worked" or
"didn't"), and steps 4/6/8's known gaps are either confirmed still-broken
(re-file/bump priority) or confirmed fixed (close them out, update this
doc's Known Issues below and its step text).

## Known issues

- ~~**Dead "Sign in" link from `/download`**~~ **Fixed 2026-09-19.**
  `pages/signin.py`/`signup.py` were deleted in `3491faf` ("Retire the
  authenticated Streamlit surface") without updating `pages/download.py`'s
  link, which depended on them. Restored both pages (trimmed: no more
  cross-tab localStorage session recovery or `/environments` redirect,
  since that page is gone for good — success now just fires the
  `casper://pair` hand-off and stops) plus the `utils/auth.py`/
  `utils/browser_nav.py` helpers they need. Verified the pages load and
  execute without exception (`streamlit.testing.v1.AppTest`) and via a
  real local `streamlit run` hitting `/signin`/`/signup` directly — **not
  yet verified with a real form submission against a live deployment**;
  step 6/7 above still need a real run-through.
- **No end-user-facing pairing confirmation** (found 2026-09-19, still
  open). Step 8 requires `harness` (an internal dev tool) to verify
  success; there's no product-facing way for a real user to see "yes, my
  machine is connected." Not necessarily a bug (the front end doesn't
  exist yet), but worth tracking as a known gap in this flow specifically.
- **Gatekeeper warning copy may be stale** (flagged 2026-09-19, still
  unverified — see step 4). Confirm actual behavior with a real notarized
  build before editing the copy either way.
