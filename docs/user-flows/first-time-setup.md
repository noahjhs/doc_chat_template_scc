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
- [ ] 4. Unzip it, open `Casper.app` for the first time (double-click, then
      if that doesn't work, right-click → **Open** per the page's current
      copy).
      **Expected:** some first-run warning, resolved via the page's
      instructions, no Terminal/`xattr` needed.
      **Copy fixed 2026-09-20** (was stale since 2026-09-19's deploy) —
      `pages/download.py` previously claimed Casper "isn't yet notarized"
      and told users to run `xattr -cr` in Terminal; both were wrong by
      then. Confirmed directly this time via `spctl -a -vvv --type execute`
      (`accepted, source=Notarized Developer ID`) and `xcrun stapler
      validate` (passed) against the actual built app — a properly
      notarized, stapled app doesn't need the quarantine-stripping
      workaround at all, just the standard right-click-to-open bypass.
      Copy updated to that; `streamlit.testing.v1.AppTest` confirms the
      page still renders with no exception. **Still not verified: an
      actual human double/right-clicking through it** — `spctl`/`stapler`
      confirm what Gatekeeper's verdict *would* be, not the exact dialog
      wording/flow on a real run (which varies by macOS version) — do
      that the next time this flow gets a real run-through.
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
      with no further clicks. The page then shows a "Waiting for Casper to
      connect..." spinner (new 2026-09-20 — see step 8) rather than
      immediately settling into its final text.
- [ ] 8. Confirm pairing actually succeeded.
      **Expected (fixed 2026-09-20 — was the "no end-user-facing UI"
      gap tracked below):** within ~12 seconds, the page polls `GET
      /hosts` (see `utils/auth.py`'s `connected_host_ids`) for a host that
      became connected *after* the pairing hand-off fired (not one that
      was already connected — matters for a returning user with other
      machines already paired) and swaps the spinner for "Casper
      connected. You can close this tab." If it doesn't detect a
      connection in that window, it falls back to the original generic
      "Check your computer..." message rather than claiming failure —
      pairing may still have worked, this poll just isn't authoritative
      (`harness hosts list` still is, if this needs double-checking).
      **Still needs a real run-through** — verified so far only via
      `streamlit.testing.v1.AppTest` (renders with no exception) and a
      direct call to `connected_host_ids` against a real, already-paired
      account (correctly returned its connected host's id); the actual
      12-second polling loop, against a *fresh* pairing, timed live in a
      browser, hasn't been observed yet.

## Pass/fail

Pass = every step's *actual* behavior is captured (not just "worked" or
"didn't"), and steps 4/6/8's known gaps are either confirmed still-broken
(re-file/bump priority) or confirmed fixed (close them out, update this
doc's Known Issues below and its step text).

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
  live deployment. **Still not verified: an actual browser click through
  the full page (form submit → `casper://pair` Apple Event → a real
  daemon receiving it)** — that needs a human at a real browser; step 6/7
  above should get a real run-through next time this flow runs.
- ~~**No end-user-facing pairing confirmation**~~ **Fixed 2026-09-20**
  (found 2026-09-19 — see step 8). `pages/signin.py`/`signup.py` now poll
  `GET /hosts` for up to ~12s after firing the `casper://pair` hand-off
  and show a real "Casper connected" confirmation once a NEW host shows
  connected (new `utils/auth.py` helper, `connected_host_ids`) — falling
  back quietly to the original generic message on timeout, never claiming
  failure. Not yet confirmed against a real, live pairing in a browser —
  see step 8's own note.
- ~~**Gatekeeper warning copy may be stale**~~ **Fixed 2026-09-20** (flagged
  2026-09-19 — see step 4). It was worse than stale copy: `pages/
  download.py` actively told users Casper wasn't notarized and to run
  `xattr -cr` in Terminal, both wrong since 2026-09-19's deploy. Replaced
  with the standard right-click-to-open instructions, confirmed accurate
  via `spctl`/`stapler` against the real built app (not yet via an actual
  human click-through — see step 4's own note).
