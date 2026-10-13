---
name: e2e-run
description: Run Casper's end-to-end test (an invited newcomer sets up with Casper's guide) unattended against the test stack, driving the VM's screen over VNC, and write a report. Launched by tools/e2e.sh; can also be used by hand.
---

# Casper end-to-end run

You are running Casper's end-to-end test **unattended, in the background**,
while the Principal works on something else. Nobody is watching. Work
quietly, finish on your own, and leave a report.

## Inputs (in the prompt that started you)

- `RUN`: the run directory. Everything you write goes there.
- `SCENARIO`: default `invite-headless`.
- `COMMIT`: the commit under test (the test stack already runs it).

## Ground rules

- **Only the test deployment.** Every `tools/scenario.py` command gets
  `--env test`. Never touch dev or prod, never deploy, never edit, commit or
  push code. Your job is to observe and report; propose fixes in the report.
- **Nothing on the Principal's screen.** VMs run with `--vnc` only. Never run
  `tools/vmgui.py open`, `tart run` without VNC, or anything that opens a
  window, plays a sound or posts a notification on this Mac.
- **One VM at a time** (`noah-mac`). Riley is headless on the mini.
- **Reach nobody.** The test stack keeps emails instead of sending them
  (`tools/scenario.py mail <address> --env test`) and has no Telegram. Use
  `noah@casper-test.invalid` as Noah's email.
- **Time limit: 75 minutes.** If you're stuck on one step for 10 minutes,
  write down what you saw and stop the run there.
- At the end, always shut down what you started, even after a failure:
  `tools/scenario.py stop $SCENARIO --env test` (only your own personas).

## Tools

- `tools/scenario.py reset <scenario> --vnc --env test`: resets Noah's VM
  and builds headless Riley (about a minute). `reset`, never `start`: start
  shuts down other VMs, which may be the Principal's.
- `tools/scenario.py ask riley-headless [-c] "..." --env test`: Riley talks
  to their own agent (Claude with Casper's tools). Use `-c` to carry on the
  same conversation. Speak as Riley would, briefly.
- `tools/vmgui.py shot noah-mac "$RUN/screens/NN-what.jpg"`, then Read the
  image. Coordinates for `click`/`dclick` are pixels in that screenshot.
  Also `type`, `key` (enter, tab, esc, ...).
- `tart exec noah-mac /bin/zsh -lc '...'` runs a command inside the VM. Use
  it for setup (opening a URL in Chrome), never to do what a person would
  click. The person's experience is the thing under test.
- Over VNC, Command-key chords and the scroll wheel don't work. Use clicks,
  the scrollbar, or keys like tab, enter and space.

## The run

1. `tools/scenario.py reset $SCENARIO --vnc --env test`.
2. **Riley invites Noah.** Ask Riley's agent "Invite my friend Noah to
   Mutual Aid." Answer its questions as Riley: email, `noah@casper-test.invalid`,
   then yes. Log each exchange in `$RUN/transcript-riley.md`.
3. **The invitation.** `tools/scenario.py mail noah@casper-test.invalid --env test`.
   Check the email: one clear call to action, a greeting by name, no jargon.
   Open its link in Noah's Chrome:
   `tart exec noah-mac /bin/zsh -lc 'open -a "Google Chrome" "<link>"'`.
4. **Noah sets up with Casper's guide**, by clicking only, as a newcomer
   would: invite page → "Start with the guide" → create an account (any
   username starting `noah`) → Accept → Download Casper → get past the
   browser's warning → open the zip and the app → macOS's own dialogs →
   Move to Applications → the guide carries on → choose a folder (Documents)
   → Start → Casper's Mirror dialog → copying → Protected → the recovery kit
   (from the ghost menu: Make Recovery Kit; any passphrase, typed twice) →
   the give-back question (answer either way) → done.
5. **Both sides agree.** Once Noah's guide says Protected, ask Riley's
   agent "What am I holding for Noah?" (`-c`). It must match what Noah was
   told: one folder, its size, up to date. Log it in the transcript.
6. Take a screenshot at every step and whenever something looks off.
   Number them in order.
7. `tools/scenario.py stop $SCENARIO --env test` (only your own personas).

## What to look for

- **Anything unexpected on screen**: a system prompt (local network, files,
  automation, login items), a dialog behind another window, a stray window
  or tab. Note when it appeared and what had just happened.
- **The guide's voice**: calm, terse, one ask at a time, buttons instead of
  typing, no internals, no apologies. Quote any line that breaks this.
- **Waiting**: thinking dots while the guide works (never two at once), the
  progress line while copying, and how long each wait took.
- **Truth**: does every line match what's actually true (online/offline,
  copied/protected)?
- **Friction for a human tester** repeating this by hand.

## The report: `$RUN/notes.md`

Start with a short summary: did it reach "done", how long it took, and the
three most important findings. Then the findings in the order they happened,
each one line, tagged **BUG**, **Friction**, **Observation** or **Good**, with
the screenshot number. Then the full guide transcript (what Casper said,
what Noah clicked). Then a "Suggested fixes" list. Plain, short sentences.
Write the report as you go, so a run that stops early still leaves one.
