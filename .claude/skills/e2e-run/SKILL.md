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
- `SCENARIO`: default `invite-headless`. `lost-mac` also has its own section at the end: do the run above first (steps 1 to 6), then that section. `invite-windows` is the same run with Noah on a Windows 11 PC: see the section `Scenario invite-windows`, which says what differs; read it first.
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

## Scenario `lost-mac`: Noah's Mac is lost and a new one recovers

Tests recovery v2 as a person would live it: P0 (the kit is saved outside
mirrored folders), P1 (the app's "Recover from a Lost Computer…") and P2 (the master
key covers old and new folders). Same ground rules; still one VM at a time
(the old VM is discarded before the new one starts). Write this part of the
report in a section of its own, `## Lost Mac`, and number its screenshots on
from the first part.

Do the main run's steps 1 to 6 first, with these differences:

- Note the username and password you give Noah's account: you need them on
  the new Mac. Use `noah@casper-test.invalid`.
- Before the kit: put a few extra files in Documents so the check is real
  (`tart exec noah-mac ... 'echo hello > ~/Documents/lost-mac-check.txt'`),
  and write down a checksum list of Documents:
  `cd ~/Documents && find . -type f ! -name .DS_Store -exec shasum {} + | sort -k2 > /tmp/documents.sha`;
  copy it to `$RUN/documents-before.sha`.
- Make the kit through the ghost menu's Make Recovery Kit. In the Save
  panel, **try saving it inside Documents first** and note that Casper
  refuses (P0); then save it to the Desktop (any passphrase, twice; write
  it down). Check the file is not in Documents.
- Wait until the guide says Protected and Riley's agent agrees.
- Also create a second folder before the loss, so old and new are both
  covered: after the first copy, ask nothing of Riley; as Noah, mirror
  `~/Pictures` too if the guide or the ghost menu offers it. If there is no
  way to do that by clicking, say so as a finding and instead (from Riley's
  side) skip it. The folder made before the master key existed can't be
  recreated here, so this is "Documents (first) and Pictures (later)".
- **Copy the kit out through the host**, as a person carrying a USB stick
  would, then stop Noah's Mac. Kit file to `$RUN/kit/`:
  `tart exec noah-mac /bin/zsh -lc 'cat ~/Desktop/"Casper Recovery Kit (<user>).txt"' > "$RUN/kit/kit.txt"`
  (use the real file name). Do not copy anything else, and do not keep a
  copy of the passphrase anywhere in the VM.

Then the loss and the recovery:

1. Add one more file in Documents on Noah's Mac, wait for the guide/status
   to say it is protected, and checksum Documents again (`documents-before.sha`).
2. **The Mac is lost.** `tools/scenario.py newmac noah --vnc --env test`
   discards the VM and starts a fresh `noah-base` one (the account stays on
   the server). Screenshot it: it must look like a Mac Casper has never seen.
3. Put the kit where a person would find it, in Downloads:
   `tart exec -i noah-mac /bin/zsh -lc 'cat > ~/Downloads/"Casper Recovery Kit.txt"' < "$RUN/kit/kit.txt"`.
4. In Noah's Chrome, open the sign-in page of the test stack
   (`https://test-app.casperagent.dev/signin`; check the real address in
   the invitation link's host if unsure). By clicking only, as a person
   would: use "I lost my computer" → sign in as Noah → the guide's recovery steps
   → download Casper → open it → the macOS dialogs → Move to Applications →
   the app's pairing hand-off. Note every place it asks something that isn't
   obvious to someone who just lost their Mac.
5. From the ghost menu choose **Recover from a Lost Computer…**. Use only the
   app's own dialogs: pick the kit in the Open panel (Downloads), type the
   passphrase, read the Restore confirmation (it must list each folder and
   where it goes: note the exact text), click Restore. Try a wrong
   passphrase first and note the message.
6. Wait for the copy to finish (progress line, then promotion; give it up
   to 15 minutes). Screenshot the guide/app at each stage.
7. **Check Documents came back intact**: checksum the restored folder
   (find where it went; the dialog says) and diff against
   `$RUN/documents-before.sha` (paths relative to the folder). Expect no
   differences. Same for Pictures if it was made. Look for anything left
   over (`.stfolder`, `.stversions`, `.syncthing.*` in the person's folder,
   a stray "Casper Restores" folder when the original place was free).
8. **The new Mac is now the folder's Mac**: create a new file in the
   restored Documents on the new Mac (`echo after > ...`), wait a few
   minutes, and ask Riley's agent "What am I holding for Noah?" (`-c`): the
   size/up-to-date state must have moved on, and the Noah account's
   `my_casper` (or the Mac name in Riley's answer) should name the new Mac,
   not the lost one. Check again with `tools/scenario.py ask riley-headless -c`
   that nothing says the folder is waiting for the old Mac. Log in
   `transcript-riley.md`.
9. **P2 check**: in the new Mac, `/Applications/Casper.app/Contents/MacOS/Casper setup status`
   (find the binary path from the other steps) and look for the master key
   being present: `security find-generic-password -s "Casper Master Key"`
   (do not print the key: no `-w`, no `-g`). If a Pictures folder was made
   after the master key, it must have restored too (it only can with the
   key). If Documents was an older-scheme folder, it restored through the
   sealed bundle; report which scheme each folder had if you can see it
   (the dev endpoints or `mirror_folders.key_scheme` on the test stack via
   `ssh casper-mini`). Don't paste any key, password or the kit's text into
   the report.
10. **Restore a deleted file** (docs/product/restore-design.md; Noah's new Mac
    is now the folder's Mac, so this is the everyday "I deleted something"
    case, through Casper's guide in Chrome, by typing as Noah would):
    - On the new Mac, in the restored Documents, create `budget-2026.txt`
      ("v1") and `budget-draft.txt` ("draft"). Wait until the guide/status
      says protected (the files reached Riley's mirror), then delete both
      (`rm`), and edit nothing else.
    - **One match, no questions.** Type to the guide: "I deleted
      budget-2026 by accident, can you get it back?". Expected: it acts at
      once (no "Restore it?"), the answer names the file and where it went,
      and `budget-2026 (as of <date time>).txt` now sits in Documents with
      "v1" inside. Count the guide's messages before the file is back: more
      than one is a finding.
    - **Several matches, the inline picker.** Type "I also lost a budget
      file". Expected: one short line and a picker drawn on the page (search
      box, "Deleted files only", versions with written times). Screenshot
      it. Pick the draft and click "Restore a copy"; the result appears as a
      guide message and the dated copy exists with "draft" inside. The
      guide must not list the files in prose or ask "which one?".
    - **A folder at a time.** Type "Put Documents back the way it was an hour
      ago" (a time before the files above existed): the new dated folder
      `Documents (as of <date>)` next to Documents must hold the files as
      they were then, without `budget-*`. The original Documents is untouched.
    - **Nothing asked about disk.** Never ask Noah to free space. (The "doesn't
      fit, replace instead?" answer is covered by tests; don't fill the VM's
      disk.)
    - Note every place the wording or the number of steps felt longer than a
      Finder or Time Machine restore would be.
11. `tools/scenario.py stop lost-mac --env test`.

What to look for in addition to the list above: the kit save panel's wording
and default folder; whether the refusal in Documents is clear; whether "I
lost my Mac" is findable and kind; the recovery guide's wording and its
agreement with what the app shows; the length of each wait; whether anything
asks for a second sign-in or the old Mac's name; and any leftover or
duplicate Mac in Riley's or Noah's views (the lost Mac still listed).
Passphrases and kit contents are secret: not in the report, not in
screenshots left on screen longer than needed.

## Scenario `invite-windows`: Noah is on a Windows 11 PC

The invite-headless run (steps 1 to 7 above) with Noah's computer being the
Windows 11 ARM VM (persona `noah-windows`, VM name `windows`). Same ground
rules, same report, same 75 minutes; the run's own findings matter most where
the experience **differs from the Mac's**: tag those **Windows** in the report
(in addition to BUG, Friction, ...) and say what the Mac does.

Differences in the tools:

- Preflight, after the reset: `tools/scenario.py exec noah-windows 'Test-Path "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"'` must say True; if not, the base image has no browser: report that as the only finding and stop.
- Reset: `tools/scenario.py reset invite-windows --vnc --env test` (`--vnc` is
  harmless: the Windows VM is always on VNC and never opens a window; the
  e2e lock is already held by tools/e2e.sh). Restores the clean `windows-base`
  snapshot, builds headless Riley.
- Screen: `tools/vmgui.py shot windows "$RUN/screens/NN-what.jpg"` (1280x960),
  then `click`/`dclick`/`type`/`key windows ...`. Keys are vncdo's names: `super`
  is the Windows key, `alt-tab`, `alt-f4`, `ctrl-l` (address bar), `enter`. Never
  `ctrl-alt-del`. Scroll with `scroll windows down`, or the scrollbar.
- Commands: `tools/scenario.py exec noah-windows "<powershell>"` runs in a
  session of its own (not the desktop): fine to read files, logs and the registry
  and to open a link in the person's browser through the interactive-session
  launcher: `.venv/bin/python tools/winvm.py gui 'start msedge "<link>"'`
  (or `start chrome` if Chrome is installed; check which browser the VM has, and use
  Edge, the default, if both). Never use exec to do what a person clicks.
- Email: `tools/scenario.py mail noah@casper-test.invalid --env test`, as before.

The run:

1. Reset, Riley invites Noah (step 2), read the email (step 3) and open its
   link in Edge through the launcher. Screenshot what the browser shows first.
2. Noah, by clicking only: the invite page → "Start with the guide" → create an
   account (username starting `noah`) → Accept → the guide's Download button (it
   must hand out the **Windows** installer, not the Mac zip) → get past the
   browser's own warning (Edge: the download panel's "Keep", maybe twice, and
   "Keep anyway") → open the installer → **SmartScreen** ("Windows protected your
   PC": More info → Run anyway; note the exact words and the number of clicks, and
   whether the guide told Noah about it beforehand) → the installer's own dialogs → the
   app starts → the guide carries on (the pairing hand-off through the browser's
   "Open Casper?" question for `casper-test://`; note it) → choose a folder
   (Documents; note Windows' folder picker wording) → Start → **firewall prompt**
   (the design avoids one, so any "Windows Defender Firewall has blocked / allow
   access" window is a BUG: screenshot it) → Casper's **Mirror dialog** → copying →
   Protected → the recovery kit (from the **tray** ghost icon, perhaps behind the "^"
   overflow arrow: note how findable it is; any passphrase, twice; the Save panel's
   default folder and drive list) → the give-back question → done.
3. Both sides agree (step 5): ask Riley's agent "What am I holding for Noah?" (`-c`).
4. Look specifically for, on Windows: wording that says "Mac", "macOS",
   "Finder", "Keychain", "menu bar", "Applications" or "ghost menu" (should be
   "computer", "File Explorer", the tray, ...); a console window flashing; a second
   Casper window or tray icon; the folder shown as `~/...` where Windows would say
   `C:\Users\noah\...`; OneDrive-redirected Documents; any dialog partly off screen
   at 1280x960; Defender ("virus") warnings; the time each wait took against the Mac's
   (about 8 minutes in all).
5. Logs for the report, over exec: `%LOCALAPPDATA%\Casper-test\command_log.txt`,
   `%LOCALAPPDATA%\Casper-test\syncthing\syncthing.log` (tail only; no secrets), and
   the Application event log for crashes (`Get-WinEvent -FilterHashtable @{LogName='Application';Level=2} -MaxEvents 5`).
6. `tools/scenario.py stop invite-windows --env test` (shuts Windows down, wipes Riley).

State plainly in the summary whether Noah reached "done" **with nothing a
person couldn't do by clicking and no BUG on Windows**: that decides whether
Casper stops saying "macOS only", so be exact and don't round up.
