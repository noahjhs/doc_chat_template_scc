# Claims: what Casper tells people, and what it knows

Status: inventory and proposal, 2026-10-16. Docs only; nothing here is built.
Written from the code at commit a5fa94a (+ the 2026-10-16 recovery and restore
work). Where a finding says "plausible", I read it off the code but didn't
reproduce it live.

Casper's product is a promise: your folders are safe, nobody can read them, you
can get them back. Everything people see (a guide line, an agent's sentence, a
dialog, an email) is a **claim** about that promise. This document lists the
claims, says what has to be true for each, says how Casper knows today, and
marks the claims that rest on weak, stale or missing evidence. Then it surveys
prior art and proposes one systematic way to make claims.

Vocabulary: a *claim* is a statement shown to a person. *Evidence* is an
observation Casper holds that bears on it. A claim is *grounded* if evidence
that is fresh enough and strong enough for its wording exists, and *ungrounded*
otherwise.

---

## 1. What Casper can actually observe (the signals)

Everything below is all Casper has. Every claim in section 2 is built from these
or from nothing.

| ID | Signal | Where from | How fresh | Strength |
|---|---|---|---|---|
| S1 | **Presence**: "this Mac is connected" | App's presence beat to `casper_service`, held in memory (`_live`, `main.py`) | Beat about once a minute; expires after `LIVE_TTL_SECONDS = 180`; **lost on every casper_service restart** until the next beat | Weak: says "the app can reach us", not "the Mac is on and healthy" |
| S2 | **Folder status report**: files, bytes, per-peer `connected`/`completion`/`last_seen`, `at_risk_files`, `unprotected_files` | Owner's daemon computes from its own Syncthing (`mirror/status.go` `OwnedStatus`), posts every 15 s while the app runs (`mirrord.Service`, `Interval`) → `mirror_status` row with `updated_at` | Fresh only while the owner's app runs. **Never compared with the clock when rendered** | Self-report of one party about its own view; point in time |
| S3 | **Syncthing's own numbers**: `remoteneed`, `completion`, `connections`, `stats/device lastSeen` | Owner's Syncthing, inside S2 | As S2 | Says the peer's *index* agrees with ours. Says nothing about the peer's disk, readability or decryptability |
| S4 | **A person's click**: Mirror/Cancel dialog, Telegram Approve/Deny, picker pick | Native dialog → `/hosts/consent`; Telegram webhook | Event | Strong for "they agreed"; says nothing about what happened afterwards |
| S5 | **Profile facts**: `recovery_kit_at`, `lost_mac_at`, `recovery_unlocked_at` | `POST /profile/recovery-kit` etc. | Event, never revisited | A report that something happened once |
| S6 | **Database rows**: grants, offerings, folders (`status`, `activated_at`), invites, `restore_host_id` | Casper's own DB | Current as of the last write | Strong for "we recorded X"; says nothing about the world |
| S7 | **Daemon call result**: `daemon_call(...)` succeeded and returned stdout | Synchronous call to a connected Mac | Seconds | Strong for "that Mac did that step just now" |
| S8 | **No signal**: static copy, a design property, the model's paraphrase | Code, site, email templates, VOICE | Never changes | Nothing is checked |

Two structural facts matter for everything that follows:

1. **Claims are rendered strings, and logic reads the strings back.** The guide's
   `protected` fact is `": protected." in _status_text(db, f)`
   (`main.py` `_guide_facts`). A copy edit to `_status_text` silently changes
   the setup flow.
2. **No evidence has an expiry.** S2 and S5 keep their last value forever. The
   only expiry in the system is S1's 180 s presence TTL.

---

## 2. Claim inventory

Each entry: **wording** and where it appears; **must hold** (the conditions that
make it true); **how known today**; **gaps**. Severity of a gap: **H** = could
tell someone their files are safe, or recoverable, when they aren't; **M** = a
wrong or stale status, or an action called done that wasn't; **L** = wording
looser than the facts.

### Protection and status

#### C1. "Protected" / "protected. Every change is on at least one mirror."

- **Wording, where.** `protection_status` and the ledger (`my_casper`) via
  `_status_text`: `"<label>: protected. Every change is on at least one mirror."`;
  with a catcher holding changes: `"protected. N recent change(s) are held by the
  catcher until a mirror wakes up."` The guide says, scripted, `"Protected. Last
  step: your recovery kit..."` (`guide.py` stage `recovery_kit`). Site hero card
  shows `Protected · last change mirrored 40 seconds ago` (a mockup).
  The agent repeats it in its own words (INSTRUCTIONS: "Never say it's protected
  until protection_status says so").
- **Must hold.** (a) Every file in the folder that exists now is, byte-for-byte,
  on at least one mirror. (b) That mirror's copy can be read back and decrypted
  by this person (key present, recoverable if the Mac is lost). (c) The mirror is
  in the arrangement the person agreed to. (d) All of it is true *now*, not as of
  the last report.
- **How known.** S2/S3: no `unprotected_files`, and some mirror connected at
  least once (`ever`, tested by `last_seen` not starting "1969"). Nothing else.
- **Gaps.**
  - **H: the report's age is ignored.** `_status_text` reads the stored report and
    never checks `updated_at` against now. If the owner's Mac is shut, asleep,
    lost or the app quit, the last report ("protected") is shown indefinitely. The
    trailing `(… as of 2026-10-16T09:12:03)` line is the only hint, in UTC and not
    as an age, and the guide's `protected` fact ignores it entirely.
  - **H: (a) is measured on the owner's index, not the mirror's disk.** Syncthing's
    `remoteneed` empty means the mirror's *index* has caught up. Disk full,
    corrupt, deleted-by-the-friend, or mid-write states are invisible (see the
    scrubbing and PDP sections below).
  - **H: nothing checks (b).** "Protected" and "can be restored" are different
    claims; today only the first is made, but people hear the second.
    `recovery-v2` covers the key half; nothing tests a read-back.
  - **H (plausible): empty or not-yet-scanned folder.** `local_files == 0` skips the
    "not protected yet" branch (`if st.get("local_files") and not ever`), and
    `_first_copy_done` accepts `completion >= 100` alone. Syncthing reports 100%
    for a folder it hasn't scanned. A just-created folder could read "protected"
    before it has seen a file. Needs a live test.
  - **M: changes not yet noticed.** A file changed since Syncthing's last scan is
    not "at risk" in S3 yet, so "every change is on a mirror" has a window of
    unknown size (watcher latency, scan interval, a paused app).
  - **M: the same string drives logic.** See structural fact 1.
  - **L: the site card is a mockup** with a live-sounding "40 seconds ago".

#### C2. "NOT protected yet…" and "N recent change(s) not on any mirror"

- **Wording.** `"NOT protected yet, waiting for the person to click Mirror…"`;
  `"NOT protected yet: no mirror has connected yet, so nothing has been copied."`;
  `"N recent change(s) not on any mirror or catcher yet (oldest M min)"`, plus the
  names, fetched live from the Mac (`_unprotected_names`); the mirror_folder
  reply `"NOT protected yet. Nothing is mirrored until the person confirms."`
- **Must hold.** Negative: it is safe to be wrong in the cautious direction. The
  claims "nothing has been copied" and "oldest M min" must still be accurate.
- **How known.** S2, S4, S6.
- **Gaps.** **L**: "no mirror has connected yet, so nothing has been copied" is
  inferred from `last_seen`; a mirror that connected before the status loop
  started can read as never. **L**: "oldest M min" is computed from the file's
  mtime, so it measures how old the file is, not how long it has been unprotected.
  Negative claims need evidence too: if the report is stale they should say
  "unknown", not repeat the last count.

#### C3. "up to date" / "connected, catching up" / "offline (last seen …)" per mirror

- **Wording.** `"  - mirror: Riley/Riley's Mac: up to date"` (`_status_text`, so
  `protection_status` and `my_casper`).
- **Must hold.** That peer holds all current content, and is reachable right now.
- **How known.** S3 (`completion == 100 and connected`), as of the last S2 report.
- **Gaps.** **H**: stale-report problem as C1 (an old "up to date" outlives the
  connection that justified it). **M**: `completion` is a percentage of bytes
  from Syncthing; 100 can hold with nothing to compare (see C1's empty-folder
  note). **L**: `last_seen` is shown as a raw timestamp prefix with no timezone.

#### C4. "holding 2 folders of theirs (3.1 GB), up to date" / "still copying" (giver's view)

- **Wording.** `my_casper`, "Space you give friends" (`_holding`).
- **Must hold.** The friend's copy on *this* Mac is complete and current.
- **How known.** The *friend's* last S2 report, `unprotected_files == 0` and any
  peer "ever connected". `_holding` uses `any(p.connected or last_seen not 1969)`,
  which is "ever", not "now".
- **Gaps.** **M**: "up to date" asserts a state of data the giver's own Mac holds
  but is judged from the friend's possibly stale report; it can say "up to date"
  while this Mac has been off for a week. **M**: no report counts as "still
  copying" (right direction), but a friend whose Mac is lost stays "up to date"
  forever. The giver's own Mac could (and should) report what it holds (S2-like,
  from the holder's side).

#### C5. "online" / "offline" / "connected" / "Your Mac isn't connected right now"

- **Wording.** `my_casper`: `Your machines: Noah's Mac (online), …`; `list_hosts`
  `connected`; guide/choose-folder: `"Your Mac isn't connected right now."`;
  guide stage `choose_folder` gate: "Their Mac is connected now."
  `setup status`: "This Mac: paired and connected as …".
- **Must hold.** The Mac is on, Casper is running, and we can reach it.
- **How known.** S1 only (presence, TTL 180 s).
- **Gaps.** **M**: "offline" means "casper_service hasn't heard a beat for 3
  minutes", not "off"; it flips to offline for **every** machine on each
  casper_service restart or deploy until beats resume. **M**: it feeds logic.
  In `_guide_facts`, `lost = restore_host_id is None and owner_host_id not in
  connected` is recomputed live, so a recovered Mac that sleeps for 3 minutes can
  flip a recovered person back into the "recovering" stage (the code comments
  show the same hazard was fixed for `mac_connected` by using "has a Mac" rather
  than "connected this second", but not for `lost`). **L**: friends' machines
  (`_host_config(...) is not None`) are judged the same way.

#### C6. "Copying Documents: 42%" / "Connecting to your friend's Mac to copy Documents…"

- **Wording.** Guide progress line (`_copy_progress`), `guide/state`.
- **Must hold.** A copy is progressing at about that percentage.
- **How known.** The best `completion` among mirrors from the last S2 report.
- **Gaps.** **M**: no age check, so 42% can sit on screen while the Mac is off;
  `max` across mirrors reports the best mirror's progress, not "every mirror".
  "You can leave this page open" implies copying continues; true only while the
  Mac is on, awake and online (the Mac sleeping is the common case on a laptop).

### Privacy

#### C7. "Encrypted, so friends can never read them" / "you can't read it" / "Not your friends, not us"

- **Wording, where.** INSTRUCTIONS/VOICE (`mcp_server.py`, guide prompt:
  "encrypted so friends can never read them"; roles: "that they can never read
  it"); site index: "Encrypted before it leaves. Names, folders and contents are
  sealed on your Mac. Not your friends, not us — no one else can open them";
  `about-app`: "Friends hold copies they can never read, and so does Casper";
  invite email: "Your files are encrypted on your computer before they leave it, so
  {inviter} can never read them"; "Casper — held by friends, read by no one";
  invite preview ("Encrypted, so {owner} can never read it"); `publish_offering`
  plan ("It arrives encrypted, so you can never read it"); the notification to a
  friend: "{me}'s Documents is now mirrored on your Mac, encrypted -- you can't
  read it."; `my_casper`: "everything arrives encrypted; you can't read it".
- **Must hold.** (a) Each peer's copy is held as a `receiveencrypted` folder,
  encrypted with a key the peer never gets. (b) That key stays in the owner's
  Keychain/kit and isn't in a place the peer or Casper can read; Casper's legacy
  bundle is ciphertext (recovery P2). (c) What the peer *can* still see is
  known and stated honestly. (d) The statement is about what is true now, not
  only the design.
- **How known.** S8, a design property, plus S6 (desired state says
  `receiveencrypted`; the peer's own daemon applies it, `manager.go`).
  Nothing is observed.
- **Gaps.**
  - **H: "never" is unqualified.** Syncthing's untrusted-device mode hides
    contents and names but not everything (it still sees, roughly, file count,
    approximate sizes, change times, that the folder exists, and how often it
    changes). Not verified against Syncthing's own list here; do that before
    keeping "never" and "Names, folders and contents" in copy.
  - **M: nothing checks (a) per folder.** Casper doesn't observe that a given
    peer actually holds the folder encrypted, and wouldn't notice a daemon
    configured otherwise. (The peer's daemon decides this from desired state
    Casper hands it.)
  - **M: the claim is made at invite time and on a friend's notification before any
    data has arrived** ("is now mirrored on your Mac" is sent at consent, not at
    first copy).
  - **L: "so does Casper"/"not us"** is true only while (b) is; it should name the
    one thing Casper holds (the sealed legacy bundle, and the catcher's
    encrypted staging).

### History and restore

#### C8. "30 days of history" / "Every version for a week, then daily for a month" / "Only 30 days exist"

- **Wording, where.** INSTRUCTIONS, guide prompt, tool descriptions
  (`list_versions`, `restore_*`, `publish_offering`), site ("History 30 days";
  "Every version for a week, then daily for a month"), invite page/email ("with 30
  days of history"), `mirror_folder` preview ("Encrypted, 30 days of history"),
  and in every restore result ("stays in your history for 30 days").
- **Must hold.** For this folder, a version of any file that existed at each moment
  in the last 30 days is on a mirror that is currently reachable (or soon will be),
  and the policy (all versions 7 days, daily to 30) is in force on that mirror.
- **How known.** S8: a constant in the peer's Syncthing config
  (`staggered`, `maxAge`) and `PruneVersions`. Nothing reports what a peer
  actually holds.
- **Gaps.**
  - **H: a promise about a future on a friend's computer.** The mirror may be off,
    full, wiped or revoked; the history only starts when the folder did (a new
    folder has no history); while a mirror is away its versions coalesce into
    nothing per-change. "30 days" is a policy, not a measured retention.
  - **M: results claim completeness they don't have.**
    `_version_listing` asks the *first reachable* mirror only; `"No deleted files
    in the last 30 days."` and `"No earlier versions match."` are therefore
    claims about one mirror's holdings. `restore_as_of`'s `"The oldest available
    is about {earliest}"` is `now − 30 days`, not the oldest version that
    exists (`restore.py earliest`), so for a 3-day-old folder it names a date that
    has no history.
  - **L: `short_time`/"as of" dates** are in whatever zone `restore.py` chose;
    a date with no zone for a "bare date means end of that day" rule is a claim
    about the person's day.

#### C9. "Restored X as it was at T … a new copy next to the original; nothing was overwritten."

- **Wording.** `_mcp_restore_version`, `_mcp_restore_as_of`: `"…N files from
  history and M unchanged ones, in <dest> -- a new folder; nothing was
  overwritten."`, `"Put X back as it was at T, in place of the current file…"`
- **Must hold.** The file(s) at that path now hold exactly the version at T;
  nothing existing was changed; the copy is complete.
- **How known.** S7: each chunk write returned success on the owner's Mac;
  `mirror_restore_version_finish` returned a path.
- **Gaps.** **M**: no read-back or hash comparison of the finished file against
  the version's expected hash; "N unchanged ones" is a count returned by the
  daemon, not verified. **L**: "nothing was overwritten" is by construction
  (a new dated name); a name collision case isn't claimed to be handled in copy.

#### C10. "Your folders are back on this Mac, and Casper keeps them mirrored." / "Restoring now" / "Restoring your folders now…" / "{label} is back on {Mac}, and this Mac keeps it mirrored now."

- **Wording.** Guide stage `recovered` (scripted); `recover_copy`
  ("Restoring now. You can leave this page open."); the Mac's dialog ("They come
  back from your friends' computers, and Casper keeps them mirrored afterwards.");
  Telegram on promotion; `_status_text` "restored on X -- N files so far".
- **Must hold.** Every file has come back and matches what was mirrored; the new
  Mac is now the folder's owner with a working mirror to the same peers; the old
  Mac can't resurrect and overwrite it.
- **How known.** S2 reported by the new Mac: `need_files == 0`, state `idle`,
  `local_files > 0` held for `PROMOTE_AFTER_SECONDS = 10` (`settled_for_s`),
  then a DB update; the guide `recovered` is `unlocked and folders and not
  recovering`, derived from S1 and S5/S6.
- **Gaps.** **H**: "back" means "the sync index settled for 10 s", not "the files
  match"; there is no count or hash comparison against the pre-loss manifest
  (not available today, since the old report is deleted at promotion:
  `DELETE FROM mirror_status`). **M**: the dialog promises "Casper keeps them
  mirrored afterwards" at the start, before anything is verified. **M**: the
  `lost` flapping hazard in C5 can contradict a "recovered" line already said.
  **M**: the board lists the guide line "Casper's window on your Mac lists your
  folders. Choose Restore there." as no longer matching the alert dialog: a
  claim about the UI that has drifted from it.

### The recovery kit

#### C11. "Saved the recovery kit" / "Recovery kit saved in <folder>. Keep a copy, and your passphrase, somewhere other than this Mac." / "Protected. Last step: your recovery kit."

- **Wording.** `setup recovery-kit` output; the app's dialog; guide
  `recovery_kit`: "without it, a lost Mac can't be rebuilt"; INSTRUCTIONS: "Keep
  the file and passphrase off this Mac."; `give_back` follows when
  `recovery_kit_at` is set.
- **Must hold.** A kit file exists, outside every mirrored folder, openable with
  the passphrase the person chose; it holds every key this account needs (master
  key + legacy bundle in v3); and the person did keep a copy off this Mac.
- **How known.** S5: the CLI wrote a file and called `POST /profile/recovery-kit`
  ("best effort"). The guide marks the step done on that timestamp.
- **Gaps.** **H: "kit exists" is the only fact, and it's never rechecked.**
  Deleted, moved into a mirrored folder, or stale (older than the keys it should
  hold) still reads "done". v3 removes staleness for the master key, but legacy
  folders and a changed passphrase are not tracked. **H**: nothing tests that the
  kit opens (the passphrase isn't known to Casper by design; the best check is a
  "dry-run open" on the Mac after writing, and a fingerprint of what it covers).
  **M**: "keep a copy off this Mac" is an instruction and then treated as done;
  Casper can't observe it, so it shouldn't claim it. **M**: the report is
  best-effort, so the guide could keep asking after a kit was made (the safe
  direction), but the reverse (marked done without a kit) can't happen except by
  a direct API call.

### Actions called done

#### C12. "Sent." / "emailed to …" / "Done (offering N)." / "Mirroring Documents to Riley's Mac. Changes reach them within seconds whenever both computers are on." / "Sent X a friend request." / the guide's "Done."

- **Wording.** `create_invite` (`sent.append(f"emailed to {email}")`),
  `_mcp_add_friend`, `_publish_mirror_offering`, `_decide_mirror_consent`; VOICE
  ("Never write an action as done until Casper confirms it").
- **Must hold.** The action happened and its effect is in place.
- **How known.** S7/S6: the function returned without raising; for email,
  `smtplib.send_message` returned (the provider accepted it, not that anyone received it).
- **Gaps.** **M: the guide's "Done."** `guide.run` sets `acted = True` for any tool
  call with `preview=False`, **without looking at the result**; a result starting
  "Can't:" still produces "Done." (`if result.startswith("PREVIEW") … elif
  args.get("preview") is False: acted = True`). **M**: "emailed" means "handed to
  SMTP". **M**: "Changes reach them within seconds" (also the site: "every change,
  within seconds") has no measurement behind it (no latency signal is stored);
  it is also conditional on both Macs being on, which the sentence says, but not
  on the relay/NAT reachability that determines whether it works at all.
  **L**: "Sent {x} a friend request. They'll be asked to accept." promises an
  approval prompt; `create_approval` is a DB row, so it depends on a Telegram
  link or the person looking.

#### C13. "Stopped mirroring X. The copies on your friends' computers are deleted after 7 days (until then they can still be restored)." / "Stored backups stay restorable for 7 days."

- **Must hold.** Each friend's daemon removes its copy after the grace; until then
  restore works.
- **How known.** S8/S6: a constant and a DB flag; each peer's daemon decides
  (`CleanupOrphans`). Not observed.
- **Gaps.** **M**: a statement about other people's computers, with no
  confirmation of deletion (and it matters: "deleted" is also a privacy claim).
  A peer that is offline for the 7 days deletes late; one that has the app
  uninstalled never does. State "asked to delete", not "are deleted".

### Roles and resources

#### C14. Giving space: "it asks nothing but their Mac being on now and then" / "how much disk it takes" / "20 GB on this Mac?" / "{inviter} has set aside 20 GB."

- **Must hold.** The giver's disk can actually hold the quota; usage is bounded by
  it; Casper never takes more.
- **How known.** S6: `quota_bytes` on a grant; per-friend `used_bytes` against the
  quota is checked *before* sharing (`mirror_folder`) and then "monitored";
  Syncthing itself has no quota (mirroring.md).
- **Gaps.** **M**: the quota is a ceiling, never a reservation; "has set aside 20
  GB" (invite email, page) reads as reserved. Free space on the giver's disk is
  never compared with the sum of grants. **M**: "asks nothing but their Mac being
  on" omits bandwidth and a bundled Syncthing running.

#### C15. "Casper keeps … safe for you from another Mac. Is that Mac lost?" (recover offer) / menu "Recover from a Lost Mac…" appears

- **Must hold.** There are folders this account owns that another Mac owns and
  this Mac can restore; the person's folders really are held by peers.
- **How known.** `restorableFolders()` from desired state (S6) via the server.
- **Gaps.** **M**: it names folders that exist in Casper's DB; whether the peers
  *still hold* them isn't checked before offering recovery.

### Website, email, scripted lines

#### C16. Marketing and onboarding copy (site, `about-app`, invite page, emails, scripted guide lines)

- **Wording.** "Your files, kept safe by your friends"; "Lose a laptop, or an
  afternoon's work, and nothing is gone"; "Ask 'are my files safe?' and get a
  straight answer — down to which recent files aren't protected yet"; "Many hands…
  With two mirrors, one is usually awake"; "(Ignore browser warnings. The app is
  safe.)"; "You can leave this page open."; the app's "Include in startup items"
  rationale ("a mirror that stops at the next restart protects nothing").
- **Must hold.** Each is a design claim or promise: it must be one the system
  can back at the time the person reads it.
- **How known.** S8.
- **Gaps.** **H: "nothing is gone"** is the strongest unqualified sentence in the
  product; it is true only if C1, C7 (key), C8 and C10 are all true. **M: "The app
  is safe."** is a statement about code signing and notarization state at build
  time, said to someone clicking past a browser warning. **L**: "usually awake"
  needs real uptime data from the first users.

#### C17. App and menu: "Service is running" / "Service is paused" / "Start mirroring {label}?" (Mirror / Cancel)

- **Must hold.** "Service is running" means whatever the person takes it to mean
  (probably: my files are being kept safe). It is the Casper process state: the
  daemon enabled and the tunnel up (`daemon.go applyState`).
- **How known.** In-process state. Not mirroring state, not S2.
- **Gaps.** **M**: a green dot "running" while no mirror has connected for a week
  is reassurance without evidence; it is a different (cheap) fact than C1 and
  should be worded as one, or fed from the same source. The consent dialog says
  only the decision; the plan was shown earlier by the agent, in a sentence the
  dialog doesn't repeat (the person can click Mirror on a plan the dialog doesn't
  show them).

#### C18. Nudges: "N recent change(s) in your Documents aren't on any mirror yet -- the computers mirroring it have been off for a while."

- **Must hold.** Unprotected changes older than `NUDGE_AFTER_SECONDS`; the mirrors
  have been off.
- **How known.** S2 `unprotected_oldest_unix`, a Telegram message.
- **Gaps.** **M**: only fires from a *fresh* report; if the owner's Mac itself is
  the one that's off, nothing fires: the absence of S2 is not an evidence the
  system acts on. This is the biggest missing "dead man's switch" (a missed
  heartbeat should itself change what Casper says, and cause a nudge).

#### C19. VOICE/INSTRUCTIONS rules about claims: "Say only what's true right now." "Never say it's protected until protection_status says so." "Never imply something is done or underway unless Casper said so."

- **What it is.** A control on the model's wording, not a source of facts.
- **How enforced.** Prompt text only, plus the guide's scripted lines (when a
  step begins "Casper writes its line, not the model") and Casper-written
  choice buttons. No check on the model's replies against what tools returned.
- **Gaps.** **H**: the model can say "Protected" or "your files are safe" in a
  turn in which no tool said so; or paraphrase "up to date" from an old result in
  history (`HISTORY_LIMIT = 60` keeps results from earlier turns in context). The
  rule limits what the model *should* say; nothing checks what it did say.

---

## 3. Summary of gaps, ranked

1. **Evidence has no expiry** (C1, C3, C4, C6, C18). One rule fixes five claims:
   every positive status claim carries `as_of` and a maximum age; older means
   "unknown, last seen …".
2. **Silence isn't treated as evidence** (C5, C18). A Mac that stops reporting
   should *change* what Casper says and trigger a nudge.
3. **"Protected" means "the index agrees"** (C1). Nothing reads data back from a
   mirror (C9/C10 same).
4. **Claims are derived from rendered strings** (C1). One typed source, not
   `": protected." in text`.
5. **Absolute wording over partial evidence** (C7 "never", C8 "30 days"/"No
   deleted files in the last 30 days", C16 "nothing is gone").
6. **Actions called done on weak results** (C12: the guide's "Done.", "emailed").
7. **Claims about other people's computers asserted, not observed** (C4, C13, C8).
8. **Setup facts treated as permanent** (C11 kit, C5 `lost`).
9. **Model paraphrase unchecked** (C19).

---

## 4. Prior art

Notes are mine; each source is linked. "Applies to Casper" is my reading.

### 4.1 Assurance and safety cases (GSN, Assurance 2.0)

An assurance case is a structured argument that a claim is true, linking the
claim through sub-claims to evidence. GSN (Goal Structuring Notation) is the
widely used graphical form: goals are claims, strategies are the reasoning that
splits a claim into sub-claims, solutions are evidence, with context,
assumption and justification nodes
([overview of GSN and cases](https://arxiv.org/pdf/1703.06350), which also
covers *dynamic* assurance cases that are re-evaluated at runtime;
[GSN for LLM review](https://arxiv.org/pdf/2511.02203)). Bloomfield and Rushby's
**Assurance 2.0** adds that arguments must record *defeaters* (reasons the claim
might be false), and that human reviewers are subject to confirmation bias, so
the argument should list counter-evidence on purpose
([manifesto](https://www.csl.sri.com/~rushby/abstracts/assurance2),
[defeaters and eliminative argumentation](https://www.csl.sri.com/~rushby/abstracts/defeaters24)).

*Applies.* Each Casper claim is a goal; section 2's "must hold" is its
decomposition; the "how known" column is the solutions layer, and the gaps are
defeaters nobody listed. A runtime-evaluated ("dynamic") case is exactly the
shape needed: a claim's truth value is recomputed from evidence as it ages. We
don't need GSN's diagrams; we want its discipline: *no claim without named
evidence, and every claim lists its defeaters.*

### 4.2 Runtime verification

Runtime verification checks a running system's trace against a specification
and emits a verdict. The key point for us is that verdicts are not
two-valued when the observation is incomplete: monitors for finite traces use
three or more values (true / false / *inconclusive*)
([Leucker & Schallhart, A Brief Account of Runtime Verification, 2009](https://isp.uni-luebeck.de/node/453)).
Failure detectors in distributed systems are the same lesson: from outside, you
only get *suspicions*, characterised by completeness and accuracy, never
certainty ([Chandra & Toueg, Unreliable Failure Detectors](https://cs.nyu.edu/~apanda/classes/sp25/papers/chandra-toueg.pdf)).
Leases give the matching tool for time: a grant that is valid only for a
bounded term, and expires by itself if not renewed
([Gray & Cheriton, Leases, 1989](https://courses.cs.duke.edu/spring15/cps210/readings/leases-sosp89.pdf)).

*Applies.* "Protected" is a property over a trace (every change eventually reaches
a mirror within a bound) observed through a lossy channel; its honest values are
*holds*, *violated*, *unknown*. A status report should be a **lease**: it supports a
positive claim for a bounded term and lapses to *unknown* unless renewed. Presence
(S1) already behaves like a lease with a 180 s term; S2 should too.

### 4.3 Observability and health-check truthfulness

Google's SRE book separates *symptoms* (what's broken, seen by the user) from
*causes* (why), and warns about monitoring that reports the cause you can see
instead of the symptom people feel
([Monitoring Distributed Systems](https://sre.google/sre-book/monitoring-distributed-systems/)).
Practitioners' accounts of Kubernetes probes show both failure modes: a shallow
probe says "healthy" while the service can't do its job; a deep probe that
checks shared dependencies turns a blip into a total outage
([the pod that lied](https://dev.to/devopsaitoolkit/the-pod-that-lied-a-kubernetes-readiness-probe-war-story-4kfg),
[deep health checks](https://leapcell.io/blog/implementing-robust-deep-health-checks-in-backend-frameworks-for-container-orchestration)).
Consumer sync tools show the user-facing version: Dropbox's tray says "Up to date"
while individual files show pending
([Dropbox community thread](https://www.dropboxforum.com/discussions/101001016/are-ya-synced-or-are-ya-not-synced/839152));
Time Machine's menu-bar "last backup" disagrees with what is browsable after a
verification
([Apple discussions](https://discussions.apple.com/thread/2465249?page=5)).

*Applies.* "Service is running" (C17) and "online" (C5) are shallow probes read as
deep claims. "Up to date" (C3) is the Dropbox failure. The fix is to say which
depth was measured ("Casper is running", "Riley's Mac was reachable 2 min ago",
"the copy matched at 14:02"), not to have a single green light.

### 4.4 Proofs of retrievability and provable data possession

PDP and PoR let a client check, cheaply, that an untrusting storage holder still
has the data. In PDP (Ateniese et al., *Provable Data Possession at Untrusted Stores*;
found via search, not read), the client keeps a tag per block, asks the server
about randomly sampled blocks and checks the proof. PoR is defined in
[Juels & Kaliski, Proofs of Retrievability for Large Files](https://www.arijuels.com/wp-content/uploads/2013/09/JK07.pdf);
a later compact scheme is in the [Asiacrypt 2008 paper](https://iacr.org/archive/asiacrypt2008/53500093/53500093.pdf)
(not read in full). PoR is stronger: it shows the file is *retrievable* in full, by adding error
correcting codes so that a sampled spot-check implies recoverability. Both cost
communication independent of the file size. In production systems the same idea
is a standing audit: Storj's satellites audit nodes for data still held and
unmodified and repair pieces that fail
([Storj whitepaper](https://static.storj.io/storjv3.pdf),
[auditing and repair](https://storj.io/blog/decentralized-auditing-and-repair-the-low-key-life-of-data-resurrection));
Filecoin's WindowPoSt requires a proof from each storage provider about each
sector every 24 hours on a 30-minute deadline, enforced by slashing
([Filecoin docs](https://docs.filecoin.io/storage-providers/filecoin-economics/storage-proving)).

*Applies.* Casper's owner holds the secret and the plaintext, which is the PDP
setting without a third-party verifier. The owner's daemon can *challenge* a
mirror: ask for randomly chosen encrypted blocks of a random file, and check them
against what the owner can compute locally. That turns "the index agrees" (E1)
into "the peer produced bytes that match, today" (E3) at a cost of a few
megabytes a day. It isn't full PoR (no coding, so spot checks give statistical
assurance, not a recoverability guarantee), and it need not be: it is enough to
make "Protected" mean something checkable and to make a lying or broken mirror
visible. Adversarial peers (a friend deleting the data) are a smaller threat
than disks, updates and unplugged drives, so unannounced sampling is the value,
not the cryptography.

### 4.5 Restore testing

The common cause of backup disaster is an untested restore. GitLab's 2017
database incident found that pg_dump had been failing on a version mismatch,
failure notices went to an address that bounced, and the other paths were
stale or incomplete; the fix included "a system to automatically test recovery"
([GitLab postmortem](https://about.gitlab.com/blog/gitlab-dot-com-database-incident),
[InfoQ summary](https://www.infoq.com/news/2017/02/gitlab-outage-postmortem)).
Backup tools build verification in at several depths: restic's default `check`
verifies structure; `--read-data` or `--read-data-subset` reads and verifies pack
contents, and the subset form lets a schedule cover everything over time
([restic docs](https://restic.readthedocs.io/en/v0.13.0/045_working_with_repos.html));
Borg's `check --verify-data` decrypts and verifies everything
([borg check](https://borgbackup.readthedocs.io/en/stable/usage/check.html)).

*Applies.* Two lessons: the *monitor of the backup* must itself be monitored
(Casper's silent-owner problem, C18), and "ran" is not "restorable". The
subset idea is the right shape for us: a rolling sampled restore drill (restore
one random old version to a temp path, compare its hash), so every file is
exercised eventually and nobody pays for a full read. It also gives the honest
sentence "last tested restore: Tuesday".

### 4.6 Data scrubbing and latent errors

Disks fail more than their specs say and silently: in field data, annual failure
rates were several times the datasheet, and failures were not independent
([Schroeder & Gibson, FAST 2007](https://www.usenix.org/biblio-2529)); in 1.5
million drives, 9.5% of nearline disks developed a latent sector error in a year,
many discovered only when scrubbing re-read the data
([Bairavasundaram et al., SIGMETRICS 2007](https://research.cs.wisc.edu/adsl/Publications/latent-sigmetrics07.html)).
ZFS's answer is end-to-end checksums in a Merkle tree plus a scheduled *scrub*
that reads every block and verifies it
([scrubs explained](https://klarasystems.com/articles/understanding-zfs-scrubs-and-data-integrity/);
[ZFS end-to-end integrity, FAST 2010](https://www.usenix.org/conference/fast-10/end-end-data-integrity-file-systems-zfs-case-study)).

*Applies.* Friends' Macs are laptops and desktops with consumer disks and
uncontrolled uptime. "Protected" has to cover latent corruption, and the only
evidence is reading it back. A rolling scrub on the peer (verify each held
encrypted block against the checksum Syncthing already stores) gives E2-level
evidence cheaply; the owner's sampled challenge (4.4) gives E3.

### 4.7 Trust calibration and overclaiming (human factors)

Lee & See's review makes the point that matters here: trust should be
*calibrated* to the automation's actual capability, and over-trust (relying on it
where it is not capable) and under-trust are both failures of design, with
automation displays one of the main levers
([Lee & See, 2004](https://journals.sagepub.com/doi/10.1518/hfes.46.1.50_30392);
[PDF](https://csel.eng.ohio-state.edu/productions/intel/research/trust/Lee%20%26%20See%20Trust%20Review.pdf)).
Experiments with ML systems show people's trust follows both stated and observed
accuracy, and the effect of a stated accuracy depends on what they observe
([Yin, Vaughan, Wallach, 2019](https://arxiv.org/html/1807.01308v1)); setting
expectations *before use*, including that the system is imperfect and where, raised
acceptance of a 50%-accurate assistant
([Kocielnik, Amershi, Bennett, CHI 2019](https://www.microsoft.com/en-us/research/?p=559869)).
A review of the literature on appropriate trust in human-AI interaction is at
[arXiv 2311.06305](https://arxiv.org/pdf/2311.06305).

*Applies.* People can't verify a backup; they will believe the tone. A calm,
confident voice (our VOICE) makes every unqualified sentence heavier. The
research supports (a) saying what was observed with when ("copied to Riley's Mac
at 14:02"), (b) stating limits before they matter ("only 30 days exist"; "your
friend's Mac has to be on"), and (c) letting the stated confidence move when the
evidence does (a "Protected" that can turn into "unknown since Tuesday") so that
observed behavior never contradicts the stated claim.

(Not cited, from memory, to verify before relying on it: the end-to-end
argument in system design, Saltzer, Reed & Clark 1984. It supports checking at
the endpoints, here the owner's Mac, rather than trusting each middle hop's
report.)

---

## 5. A systematic approach for Casper

The aim: **a person is never told something Casper can't currently back, and when
the evidence weakens the words weaken with it, without anyone remembering to
change them.**

### 5.1 Evidence levels

Evidence is what a claim stands on. Each piece is typed by *how it was
obtained*, because strength comes from method:

| Level | Name | Meaning | Today's examples |
|---|---|---|---|
| E0 | **Asserted** | Design property or copy; nothing observed | "never read", "30 days", site copy |
| E1 | **Reported** | One component's own statement about itself | S1 presence, S2/S3 index agreement, S5 kit-saved |
| E2 | **Observed** | A *different* party checked it, or Casper called the Mac itself | S7 daemon call; a peer's scrub result; the owner's own read of the peer's reported state |
| E3 | **Challenged** | Random, unannounced challenge answered with bytes that match (PDP-style) | none today |
| E4 | **Exercised** | A real end-to-end use of it: a sampled restore verified by hash | none today |

### 5.2 A claim is a typed statement with named evidence and a freshness limit

A closed set of claim types, each a small record in one module, e.g.:

```
Claim FolderProtected(folder)
  holds when : EVIDENCE index_synced(folder, mirror)   level >= E1  max_age 2 min   for >= 1 mirror
               AND  EVIDENCE sampled_read(folder, mirror) level >= E3  max_age 48 h
               AND  EVIDENCE key_recoverable(account)      level >= E1  max_age 30 d
  strong form: + EVIDENCE restore_drill(folder)            level  = E4  max_age 30 d
  defeaters  : no scan since last change; empty folder; mirror revoked; disk full on mirror
  words      : holds -> "Protected."; lapsed -> "Not confirmed since {as_of}."; false -> "Not protected: {why}."
```

Other types in the same shape: `MacReachable(host)`, `PeerCurrent(folder, peer)`,
`HistoryAvailable(folder, days)` (carries the *measured* oldest version, not 30),
`KitSaved(account)` (exists, outside mirrored folders, covers current keys, opened
by a dry-run), `RestoreComplete(restore_id)` (counts and hashes match a manifest),
`ActionDone(action_id)` (the effect exists, not "the function returned"),
`EncryptedAtRest(folder, peer)` (the peer's daemon reports `receiveencrypted`; a
design note lists what the peer can still see), `DeletedAfterGrace(folder, peer)`.

Rules for the type:

- **Evidence is an object, not a flag**: `{kind, subject, observer, method, level,
  observed_at, value}`. The store is append-only per key; "latest" is a query.
- **Every evidence kind has a freshness limit (a lease).** `S2` is 2 minutes;
  presence 3 minutes; a kit 30 days; a challenge 48 hours. Past the limit the
  claim's state is *lapsed*, not its last value.
- **Three truth values plus age**: *holds* / *violated* / *unknown*, and for
  *holds* the `as_of`. Never a boolean. *Unknown* is the default; a silent Mac
  yields *unknown* (and a nudge), not the last report.
- **Asymmetry on purpose**: a positive claim needs fresh, strong evidence; a
  cautious claim ("not confirmed yet") never does. Wording can fail only in the
  safe direction.
- **Wording is a function of state**: a template table keyed by `(claim type,
  state)` produces the sentence, with `as_of` and, where it's relevant, *what was
  checked* ("every change reached Riley's Mac at 14:02", not "safe").

### 5.3 One place in code that issues claims

A single module, say `casper_service/claims.py`, the only code allowed to produce
or interpret claim text:

```
issue(claim_type, subject, now) -> Issued{state, as_of, evidence_ids, text, say_verbatim}
```

- `_status_text`, `_holding`, `my_casper`, `protection_status`, guide gates
  (`Facts.protected`, `recovery_kit`, `recovered`), notifications, emails and the
  site's status card all call `issue(...)`; none formats "protected" or "up to
  date" itself, and none parses a rendered string (replace
  `": protected." in _status_text(…)` with `issue(FolderProtected, f).state == HOLDS`).
- Logic uses the `state`; humans see the `text`. They can't drift apart.
- **Static copy goes through the registry too.** Site, emails and the app's
  dialogs reference a claim id; each registered design claim (E0) names its
  owner, the test that backs it or the words "promise, not measured", and is
  shown only at moments it can be backed. A CI check greps the repo for claim
  vocabulary ("protected", "up to date", "can't read", "never read", "30 days",
  "online"/"offline", "restored", "saved") outside the registry and fails.
- **Tool results carry the claim, not just prose.** `protection_status` returns
  the issued sentence marked `SAY:` plus its `as_of`, and the agent is told to
  quote it (the guide already works like this for scripted steps: "Casper writes
  its line, not the model").
- **Every issued claim is logged** (`claim_log`: id, type, subject, state,
  evidence ids, rendered text, surface, time). "We said Protected when it wasn't"
  becomes an inspectable event, and a later contradiction ("we said recovered")
  can be seen before being repeated.

### 5.4 Collecting stronger evidence (what to add, in order of value)

1. **Leases on what exists.** Store `updated_at` use: every S2/S5/S1-backed
   claim carries `as_of` and a limit. No new infrastructure. Fixes gaps 1, 2, 4.
2. **A dead man's switch.** Owner silent past a limit → `FolderProtected` becomes
   *unknown*, and a nudge goes out (Telegram). Covers a lost or dead owner Mac.
3. **Holder-side reports.** Each Mac that holds a friend's folder reports what it
   holds (count, bytes, last write, scrub result) in S2's mirror image, so the
   giver's view and the owner's claim have independent evidence (E2).
4. **Sampled read challenge** (4.4): daily, a few random encrypted blocks of a
   random file per mirror, compared with what the owner computes. Gives E3; also
   detects "friend deleted it", "disk full", "restored a backup of the drive".
5. **Rolling restore drill** (4.5): weekly, restore one random old version to a
   temp path, verify hash and delete it. Gives E4 and the sentence "last
   tested restore". Also checks the decrypt path and the key.
6. **Kit verification**: after writing, a dry-run open on the Mac (the passphrase
   is in hand then), recording the kit's fingerprint and what it covers; a daily
   local check that the file still exists, outside mirrored folders, and covers
   the current keys. "Kept a copy off this Mac" stays an instruction, never a claim.
7. **Restore manifests**: before promotion on a recovery, compare the new Mac's
   file count and a rolling hash with the last known manifest (kept as a hash,
   not names) so "your folders are back" is E4.
8. **Real latency**: stamp the time a change was noticed and the time the mirror
   reported it, so "within seconds" becomes a measured p95 or goes away.

### 5.5 Limiting the scope of possible errors

Errors can't be eliminated; the design limits how far a wrong claim travels and
how long it lives.

1. **Closed vocabulary.** Only claim types in the registry can be said; a new
   kind of reassurance means a new type with evidence and defeaters (the Assurance
   2.0 discipline, 4.1).
2. **Default to unknown.** No evidence, expired evidence and conflicting evidence
   all render as *unknown*, never as a positive.
3. **Asymmetric rendering.** Positive claims need strong fresh evidence; cautious
   claims need none. Monotonic by construction: weaker evidence never yields a
   stronger sentence (property-test this over all evidence combinations).
4. **Narrow wording.** Say what was observed, to whom, when; qualify "never" and
   "nothing is gone" with what they depend on, and state the limit ahead of need
   (4.7).
5. **Separate the three layers**: evidence collection (daemon and service),
   claim policy (the registry), rendering (templates). Each is testable alone, and
   a bug in one can't bypass the others.
6. **No model-written assurance.** For Tier A claims the model quotes the
   issued sentence verbatim; a post-check rejects a reply that contains claim
   vocabulary without a matching issued claim in this turn's tool results (the
   existing guard against "acting in the reply that showed the plan" is the
   precedent). Old tool results are dropped from context after their age limit.
7. **Tiers by blast radius.** Tier A (could lead to data loss if wrong):
   protected, restorable, history, kit, recovered, deleted-after-grace. Tier B
   (misleads about state): online, up to date, progress, done. Tier C (marketing).
   Migrate A first; B next; C is registry-only with owners.
8. **Defeater tests per claim.** Each claim type has fault-injection tests: freeze
   the clock, drop reports, fill a mirror's disk, delete a mirror's copy, revoke a
   peer, quit the owner's app; the expected rendered state is asserted.
9. **Ground-truth e2e.** The unattended e2e runs (`tools/e2e.sh`) assert issued
   claims against reality: when "Protected" is issued, kill the owner Mac and
   restore on a fresh one (the lost-mac scenario already does the second half);
   when "restored" is said, compare hashes.
10. **Audit the log.** Review `claim_log` for claims issued with no evidence
    refs, and for state flips (`holds` → `unknown`) as a health signal about the
    evidence pipeline itself.
11. **Change control.** A diff touching claim templates or evidence limits shows
    the affected claim types and their before/after wording for the Principal's
    review. Wording changes are rare and deliberate; they are no longer a side
    effect of editing a status function.

### 5.6 Suggested order

1. Add freshness (5.4 #1, #2) inside the current `_status_text`, and make the
   guide read states instead of strings: smallest change that closes the biggest
   gaps. Fix the guide's "Done." (C12) and the `lost` flapping (C5).
2. Introduce `claims.py` with `FolderProtected`, `MacReachable`, `ActionDone`,
   `KitSaved`; log issued claims; add the vocabulary CI check.
3. Holder-side reports and the sampled challenge (5.4 #3, #4); move "Protected"
   to require E3.
4. Restore drill, kit verification, restore manifests (#5 to #7).
5. Re-word the site, emails and dialogs from the registry; settle the "never"
   privacy wording against what Syncthing's untrusted mode exposes.

### 5.7 Open questions for the Principal

- Is "Protected" to mean *the index agrees and a sample read back* (E3), or are we
  content with E1 plus freshness for the pilot? (Cost: a few MB a day per mirror.)
- How should a Mac that is simply closed overnight read? Proposal: "Protected as of
  23:10; Riley's Mac asleep" is a holds-with-note, only unknown after a day.
- Do we state the privacy limits (file count, sizes, timing) in plain words, in
  the invite and the site, or keep "never" and accept the risk?
- Should the model's verbatim-quote rule (5.5 #6) apply to the person's own agents
  (we can instruct, not enforce), or only to Casper's guide?
