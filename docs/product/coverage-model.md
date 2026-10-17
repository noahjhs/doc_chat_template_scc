# Coverage: what Casper protects, as a set

Status: design proposal, 2026-10-17. Docs only; nothing here is built.
Related: [claims.md](claims.md) (what Casper may say), [agent-first-ux.md](agent-first-ux.md)
and [gates.md](gates.md) (how it is offered), [scenarios/mirroring.md](scenarios/mirroring.md)
(the mirroring machinery), [windows.md](windows.md).

Written against the code at `fbb411d`. What I read in the code is marked as such;
what I believe about Syncthing, macOS or Windows without having measured it here
is marked **(verify)**. Nothing about performance below is a measurement.

## 1. The problem

Today Casper protects **folders**: a person picks one (default Documents), and
Casper mirrors it. Everything else is silently not protected, and the person has
to know to add Desktop, Pictures, `~/Projects`, and every folder they create
next year. The one exclusion that exists is hard-coded: the Photos library
(`agent/internal/mirror/carveout.go`), plus per-file ignore lines for OneDrive
placeholders (`cloudonly.go`) and the noise list in `noise.go`.

What people mean when they say "back up my stuff" is closer to *"everything of
mine, except what I'd never miss"*. Backblaze and Time Machine are built that
way (section 9). So the unit should be a **coverage set**: a few roots, minus
exclusions. "My whole home folder except Applications, Library, Music and
Movies" is one sentence, and a folder created tomorrow is in it without anyone
doing anything.

The hard part is not the set arithmetic. It is that a bigger, vaguer scope makes
every other promise harder: it costs friends' disk, it touches macOS privacy
prompts, it meets files that can't be read, and "protected" has to keep meaning
something. So this document treats the set, its Syncthing mapping, its defaults
and its claims together.

## 2. The model

### 2.1 Definitions

- A **root** is an absolute directory on one of the person's computers that
  Casper covers recursively. Typical: home (`~`), or `~/Documents`.
- An **exclusion** removes paths from a root. It is either a **path**
  (anchored to a root: `/Library`, `/Music`) or a **pattern** (a name or glob
  that matches at any depth: `node_modules`, `*.photoslibrary`, `*.dmg`).
- The **coverage set** of a root is every path under it that no exclusion
  matches. A path outside every root, or inside an exclusion, is **not
  covered**. Nothing else is left out silently.
- A **coverage** is `{version, roots[], exclusions[], overrides[]}`. It is
  data, not a set of rules in code, so the same coverage can be shown to the
  person, sent to the daemon, compared, stored, and restored on a new
  computer.

```
Coverage v1
  roots:      [ ~ ]
  exclusions: builtin  photos-library   pattern  *.photoslibrary          always
              builtin  app-bundles      path     /Applications            default
              builtin  library          path     /Library                 default
              builtin  node-modules     pattern  node_modules             default
              personal music            path     /Music                   (person's)
              personal movies           path     /Movies                  (person's)
  overrides:  [ ]            // built-in "default" entries the person turned back on
```

### 2.2 Semantics, stated so they can be tested

1. **Exclusion beats root.** A path is covered iff it is under a root and no
   active exclusion matches it or one of its parents.
2. **No re-includes inside an exclusion, in v1.** You can't say "Library, except
   Mail". The only way to un-exclude something is to remove the exclusion
   (or override a built-in). Reason: Syncthing's negation (`!`) makes it walk
   ignored directories to look for exceptions, which costs what the exclusion
   saved (its docs say so, section 5.1), and a second root inside an excluded
   directory would be a nested Syncthing folder, which it says it does not
   support. If people need it, section 12 asks.
3. **Roots may not overlap.** A root inside another root is rejected (same
   nested-folder limit). Adding `~/Documents` while home is a root is a no-op
   that says "already covered".
4. **New things join automatically.** Anything created under a root that no
   exclusion matches is covered from the next scan. That is the point. It
   means coverage can **grow without the person acting**, so growth is observed,
   reported, and checked against friends' space (section 7), never assumed to fit.
5. **Exclusions are stated, never inferred from absence.** If a path isn't
   covered, Casper can name the rule that left it out (rule id, reason) or say it
   is outside every root. "I don't know" is only allowed for a path it can't
   read (section 10).
6. **Removing a root or adding an exclusion doesn't delete history.** Copies
   already on friends' computers stay until normal pruning (30 days; the stop
   behaviour in claims C13 is the model). Backblaze states the opposite policy
   ("after you add files to the exclusions list, they are deleted from Backblaze
   servers in 30 days", section 9). Which one we want is a question for the
   Principal; whichever it is, the words after "stop covering X" must say it.
7. **The same coverage applies on every computer that owns the root**, but paths
   are resolved per computer: `~/Documents` on Windows may be
   `~\OneDrive\Documents` (windows.md). A root is stored as a path relative to a
   known folder (`home`, `documents`, `desktop`) plus a subpath, not as a string.

### 2.3 Exclusion sources

Three, kept apart because each makes a different claim to the person:

| Source | Who makes it | Can the person change it? | Example |
|---|---|---|---|
| **Built-in, always** | Casper's catalog | No | Photos library (today), Casper's own recovery kit, Casper's data folder, sockets and lock files |
| **Built-in, default** | Casper's catalog | Yes: turn it back on | `/Library`, app bundles, caches, `node_modules`, VM images |
| **Personal** | The person (words, or unchecking in a list) | Yes | `/Music`, "my Downloads", `~/old-photos` |
| **Detected** | The daemon, from the machine's state | No; it ends when the cause does | An unreadable file, a OneDrive placeholder (today's `cloudonly.go`), an iCloud "dataless" file |

Detected exclusions are what the carve-out code already does for OneDrive: they
exist so Syncthing won't trip, and they are always *reported*, never hidden
(they feed `unreadable_items`, which already stops a folder from being called
protected).

"Always" is for what Casper can't or mustn't mirror, not for what it thinks is
junk. The Photos library is the first: it is a package of databases the system
guards and can't be mirrored safely file by file, and the Principal decided on
2026-10-16 to leave it out "for now". It stays an "always" entry with a stated
reason; when we support it properly, the entry is removed from the catalog and
the offer changes. Anything the person might miss belongs in "default", so they
can say yes to it.

## 3. How the catalog and the coverage live in the system

- **The catalog is shipped with the app** (per platform, versioned), because
  only the machine knows what exists. Each entry: `id`, `match` (path or
  pattern, per OS), `tier` (always / default), `reason` (one plain sentence, the
  one claims use), `named` (whether it must be named to the person, section 10),
  and `introduced` (catalog version).
- **The coverage is stored by `casper_service`** next to the folder it belongs to
  (new `coverage` JSON on `mirror_folders`: roots are the rows themselves, so
  this holds exclusions, overrides and the catalog version applied).
  `mirroring.py` already computes each Mac's desired state from grants; it adds
  the coverage to it, and the daemon renders it. It is not secret, and the
  server already knows each folder's path.
- **It is part of recovery.** A new computer that restores the folder gets the
  same coverage, so "what was protected" survives a lost Mac exactly like the
  folders do (recovery-v2-design.md). It doesn't go in the kit: it is not a key.
- **Catalog updates don't silently shrink protection.** A new "default" entry
  applies to folders created afterwards. For existing folders it becomes an
  offer ("node_modules folders are 6 GB and can be rebuilt. Leave them out?").
  Only "always" entries apply immediately, and the person is told. Otherwise
  Casper would be quietly mirroring less than it said yesterday.

## 4. Mapping onto Syncthing

### 4.1 One Syncthing folder per root

Casper's `mirror_folders` row is one root, and one root is one Syncthing folder,
exactly as now. "Home minus X" is a single folder rooted at `~` whose ignore
list holds the exclusions. Alternatives considered:

| Option | For | Against |
|---|---|---|
| **A. One folder per root, exclusions as ignores** (recommended) | Matches the code and DB today. New folders inside join with no machinery. Loose files at the top of home are covered. One status, one consent, one restore label. | One big index. One slow or broken item affects the whole folder's status. Consent/quota is all-or-nothing. |
| **B. Expand a root into one Syncthing folder per top-level child** (`~/Documents`, `~/Desktop`, `~/Projects`...) | Smaller indexes. Per-folder status, and per-folder restore pickers read naturally. A privacy-prompt denial hurts only that child. | A new top-level folder needs a watcher to create its Syncthing folder and a consent decision. Loose files at the root need their own folder, and a home folder can't ignore-and-nest children (nested folders are "not supported, recommended or coded for", Syncthing FAQ). Many folders = many handshakes and password derivations. |
| **C. Hybrid:** A for the root, but split out a child above a size or file-count threshold | Keeps the common case simple. | Splitting an existing folder moves files between Syncthing folders (re-index, history boundary). Complex. |

Recommendation: **A for v1**, with the measurement in 4.3 deciding whether B or C
is needed. A is also the least surprising for a claim: "your home folder" is one
thing that is or isn't protected.

### 4.2 Rendering exclusions as `.stignore`

Syncthing's ignore file is per folder, **never synced to other devices**, and
the first matching line wins; `*` doesn't cross a directory boundary, `**`
does; a leading `/` anchors to the folder root; `(?i)` is case-insensitive, and
"on Mac OS and Windows, patterns are always case-insensitive"
([Syncthing: Ignoring Files](https://docs.syncthing.net/users/ignoring.html)).
The mapping:

- Path exclusion `/Library` → the line `/Library`.
- Pattern exclusion `node_modules` → `node_modules` (any depth).
- Always put exclusion lines first, `!` lines never (rule 2 above).
- Casper owns one **marked block** in the ignore list and leaves every line
  outside it alone, as `carveOut` does today for lines the person added:

```
// >>> Casper coverage v1 (managed block; change it in Casper)
// Casper: the Photos library isn't mirrored yet
(?i)*.photoslibrary
/Library
/Applications
node_modules
// <<< Casper coverage
```

- Casper writes it through `/rest/db/ignores`, as `carveOut` does, and **reads
  it back** after writing. The read-back is the evidence for the coverage claim
  (section 10): it is what Syncthing is really applying (E2 "observed" in
  claims.md 5.1), not what Casper meant to apply.
- Detected exclusions (cloud-only placeholders) stay as exact-path lines
  (`ignoreLine`), in a second marked block, with the existing cap (500 files,
  20 s scan). **When the cap is hit, Casper says so and doesn't call the folder
  protected**, which today it does implicitly and should do explicitly.
- Syncthing can't ignore by **size** or by **file content**. "Very large files"
  can only become explicit path lines found by a scan (like the cloud scan) or,
  better, a *suggestion to the person* naming the big folders (section 6). I
  don't propose a size-based exclusion rule.
- Marker-based junk (a `target/` is junk only beside a `Cargo.toml`) can't be a
  pattern either. Pattern only the unambiguous names (`node_modules`,
  `__pycache__`, `.venv`, `DerivedData`, `Pods`); for ambiguous names
  (`build`, `dist`, `target`, `out`) don't exclude by default. A scan that
  turns markers into exact paths would hit the cap on a developer's machine.
- Unreadable-by-design items (sockets, FIFOs, `*.sock`) are not worth a
  claim; they go in the always tier, unnamed (aggregated).

### 4.3 Whole-home performance

What is known, from Syncthing's own docs and not from this project's tests:
the folder is rescanned hourly by default, which "means checking every file's
modification time and comparing it to the database" and "can cause spikes of CPU
usage for large folders" ([FAQ](https://docs.syncthing.net/users/faq.html));
the filesystem watcher is on by default with a 10 s delay and the hourly rescan
as fallback ([config](https://docs.syncthing.net/users/config.html)); a negation
inside an ignored directory forces traversal (ignoring page above). Linux's
inotify watch limit doesn't apply to macOS or Windows, which Casper targets
**(verify)**. I believe an ignored directory is neither walked nor watched
**(verify)**; if so, excluding `/Library` and `node_modules` is the main
performance lever, and is a second reason (besides safety) to exclude them rather
than ignore them late.

What I haven't measured, and the plan should before the default is decided: a
real home folder (Documents, Desktop, Projects, Downloads, dotfiles) is
plausibly 100k to a few million files once `node_modules` and `.git` are
counted **(verify)**. Experiment, on the mini or a Tart VM: generate homes of
50k / 250k / 1M files with realistic depth; for each record first-scan time,
steady-state RSS and CPU, database size, hourly-rescan time, and time from a
file change to a peer having it; repeat with and without the built-in defaults;
and test the Windows VM the same way (NTFS metadata is slower). Decision rule to
propose: if first scan or the hourly rescan exceeds a few minutes or memory
exceeds a few hundred MB at the 90th-percentile size, move to option C for that
root. These thresholds are guesses until measured.

One more cost, specific to Casper: receivers keep **30 days of versions**
(`staggered`), so a high-churn item (a mail database, a running VM disk) costs
its churn, not its size, on the friend's disk. Coverage defaults (section 6)
are partly an estimate of churn. The "does it fit" check (section 7) should
have a churn allowance; its size is unmeasured.

## 5. Interactions

### 5.1 macOS privacy prompts

macOS asks the person before an app reads Desktop, Documents or Downloads, and
they can revoke it later in Privacy & Security > Files & Folders
([Apple Support](https://support.apple.com/guide/mac-help/control-access-to-files-and-folders-on-mac-mchld5a35146/mac)).
Removable and network volumes, iCloud Drive and some app data are guarded too,
and Mail, Messages, Safari data and similar need Full Disk Access **(verify the
full list per macOS version)**. A home-folder root makes this the main
interaction, because the first scan touches all of it at once.

- **Never require Full Disk Access.** Asking for it is a heavy request and a
  trust cost, and most of what it unlocks (`~/Library/Mail`, Messages, Safari)
  is in the default-excluded Library anyway. This is a reason Library is
  excluded by default, besides churn and size.
- **Prompts come as one, on purpose, in context.** Prompts are attributed to
  the app doing the reading (Casper's Syncthing child, **verify** how that shows
  to the person), and appearing "from nowhere" minutes after the person said yes
  looks like malware. So: the Mirror consent dialog (the trusted path, gates.md)
  says "macOS will ask to let Casper see Desktop, Documents and Downloads. Choose
  Allow.", the daemon touches those three directly after "Mirror", and only then
  starts the scan. The guide already has a "choose Allow" note for the first
  pairing; this is the same pattern.
- **A denial is a result, not a silence.** If the person says Don't Allow, or
  later revokes, the folder reads as unreadable (the existing path), the plan
  says "Desktop isn't covered because Casper wasn't allowed to read it", and
  the status never says protected. Casper may point to the Settings pane, once.
  I haven't verified how Syncthing reports a TCC denial; the daemon should probe
  the three folders itself rather than rely on that **(verify)**.
- **Roots outside the three guarded folders** (`~/Projects`) never prompt, which
  is why a hybrid of "ask for exactly these" is cleaner than whole-home when
  the person only wants Documents.

### 5.2 Windows equivalents

Windows has no equivalent prompt; the surprises are different (windows.md):

- **Known folders move.** Documents may be `~\OneDrive\Documents` and is still
  in home but not at `home\Documents`. Roots are resolved through
  `KnownFolder(...)`, not string-joined (rule 7 in 2.2).
- **Equivalent default exclusions:** `AppData` (as a path; its `Roaming` part
  holds real settings but is app-internal, like Library), `NTUSER.DAT*`
  (locked by the OS), `$RECYCLE.BIN`, `Windows.old`, `System Volume Information`,
  junction points such as `Application Data` and `My Documents` (reparse
  points; walking them can loop **(verify** how Syncthing treats junctions)),
  and `Thumbs.db`/`desktop.ini`/`~$*` (already noise).
- **OneDrive on demand** (cloud-only placeholders) is the Windows form of the
  cloud-sync problem in 6.3; the existing skip-and-report is the model.
- **Other things that may bite and aren't verified:** Defender's Controlled
  Folder Access (can block an unknown app from reading or writing protected
  folders) and locked files (already reported unreadable).
- **Case and path length:** NTFS is case-insensitive and Syncthing patterns on
  Windows are as well; long paths are handled (windows.md phase 4).

### 5.3 Friends' quotas and size

A grant has `quota_bytes`; Syncthing has none, so Casper checks the size before
sharing and monitors afterwards (mirroring.md). Coverage makes the size a moving
target:

- **At plan time**, Casper computes the covered size (it can look it up; it
  never asks the person), and the excluded size by rule, and shows both: "84 GB,
  and 210 GB left out (Movies 120 GB, Music 70 GB, Library 20 GB)". That is
  also what makes the exclusions understandable.
- **Fit is against the mirrors the person chose** (their friends' grants and
  free space), not against a number. If it doesn't fit, Casper doesn't ask a
  question about it; it offers the next smaller coverage (section 8's ladder).
- **After consent**, covered size is re-read on each status report. When new
  folders or files push it past a mirror's quota, the state is **not
  protected: N GB don't fit**, with the biggest recent additions named. Casper
  doesn't silently stop covering something, and doesn't silently keep going on
  a friend's full disk. The person decides: leave something out, or ask for more
  room.
- **A large new thing** (a new top-level folder over a threshold, say 5 GB or
  10% of the quota, to be tuned) is noticed on a status report and raised once
  as a choice ("A new folder 'Footage' is 40 GB. Cover it?"). Under the
  threshold, new folders join silently and show up in `my_casper` under "added
  since you last looked".
- **The friend's view.** The friend sees the owner's total, not the coverage.
  Coverage never reveals paths to them (names are encrypted; mirroring.md).
  Their `my_casper` line "holding N GB of theirs" is unchanged.

## 6. Curated defaults, and why

Principle: a built-in "default" exclusion must be something where, for almost
everyone, **at least one holds: it can be regenerated, the system or an app
guards it so a file-by-file copy would be wrong, it is already held elsewhere
and re-downloading is the normal path, or it is so large or so churny that
covering it would starve everything else**. Each catalog entry carries which one
as its `reason` (Carbon Copy Cloner groups its automatic exclusions by reason the
same way; section 9). If none holds, it is not a default.

macOS first, Windows equivalents in 5.2.

### 6.1 The person's example

| Entry | Reason | Notes |
|---|---|---|
| `/Applications` (`~/Applications`) | Regenerable (reinstall), app bundles are code | The `/Applications` in `/`, not in home, is outside a home root anyway; the `~/Applications` case is the one that matters. Backblaze and CrashPlan both exclude it (section 9). |
| `/Library` | Guarded, churny, mixed | `~/Library` holds Mail, Messages, Safari, Keychains, caches, app databases and iCloud/cloud-provider files (6.3). Most of it is churny SQLite state that would be copied inconsistent mid-write, or needs Full Disk Access. It is also where the **login keychain** lives (`~/Library/Keychains`), so "protected" must not imply passwords are safe. See 12, Q3. |
| `/Music`, `/Movies` | Large; mostly re-obtainable | The Music and TV apps keep their libraries here as managed packages. A person's own recordings and footage live here too, so these are **named** in the plan and the person can say "include my Movies". They are a *size-conditioned* default: excluded when large, offered otherwise (Q4). |

### 6.2 Caches and regenerable stuff

Reason: regenerable. These should usually be aggregated in the plan as one phrase
("caches and app clutter") rather than listed, per section 10.

- `~/Library/Caches` (inside Library already), `~/.cache`, `~/.npm`,
  `~/.cargo/registry`, `~/.gradle`, `~/.m2`, `~/.cocoapods`, `~/.nvm`,
  `~/.pyenv`, `~/.rustup`, Conda installs (`~/miniconda3`, `~/anaconda3`)
- `.Trash`, `.DS_Store`, `.Spotlight-V100`, `.fseventsd`,
  `.DocumentRevisions-V100`, `.TemporaryItems` (Time Machine, CrashPlan and CCC
  all exclude versions of these)
- Developer junk by unambiguous name, at any depth: `node_modules`,
  `__pycache__`, `.venv`/`venv`, `DerivedData`, `Pods`, `.next`, `.gradle`.
  **Not** `.git`: it often holds the only copy of unpushed work, and its cost is
  many small files, which section 4.3 measures. Not `build`/`dist`/`target`
  (ambiguous; 4.2).
- Dotfiles of value stay in: `.ssh`, `.zshrc`, `.gitconfig`, `.aws`... They are
  small, and encrypted before they leave. (Whether to mirror credentials to
  friends at all, even encrypted, is Q8.)
- Standards to honour as detected sources if cheap: `CACHEDIR.TAG`
  ([spec](https://bford.info/cachedir/)) and macOS's per-item "exclude from
  backup" attribute, which Time Machine honours as its *sticky exclusion*
  (section 9). Both need a scan to find, so they'd be detected, capped, and
  reported, not pattern lines. Optional, later.

### 6.3 Cloud-synced folders

iCloud Drive (`~/Library/Mobile Documents`), Dropbox, OneDrive, Google Drive
(on current macOS, `~/Library/CloudStorage/<Provider>`, plus legacy `~/Dropbox`,
`~/OneDrive`) **(verify paths per version)**.

- **Why leave them out by default:** reading an evicted file ("files on
  demand", dataless on macOS, placeholder on Windows) *downloads it*, so a scan
  of a half-evicted folder can pull tens of GB onto the disk and fail partway;
  the provider's client is already writing there, so two sync engines collide;
  and the data is already on someone else's computer, so the marginal
  protection is smaller. The OneDrive code path today skips and reports for the
  same reason.
- **Why that isn't the whole story:** sync is not backup. A deletion or a
  ransomware overwrite syncs too; the providers keep only limited recovery
  windows. Casper's 30 days of history on a friend's computer is a different
  thing. So the plan must *say* "iCloud Drive isn't included. It's kept by
  Apple" rather than imply those files are safe with Casper, and it should be
  re-offerable ("include it when it's all downloaded").
- **The trap:** with iCloud's *Desktop & Documents Folders* or OneDrive
  known-folder redirect turned on, the person's Documents and Desktop *are* in
  the cloud and may be partly evicted. That's the most common case, and the one
  where "I protected Documents" and "some of Documents are placeholders" are both
  true. The detected-exclusion path must cover macOS dataless files like it does
  OneDrive on Windows, and the status must say "N files in Documents are stored
  only in iCloud and aren't included" instead of "protected". Not verified on a
  real Mac with iCloud Drive optimized storage on; the e2e VMs have none.

### 6.4 Very large media and disk images

- Disk images and VM disks by extension: `*.dmg`, `*.iso`, `*.vmdk`, `*.vdi`,
  `*.qcow2`, `*.pvm`, `*.utm`, `*.sparseimage`, `*.sparsebundle`, `~/.tart`.
  Reason: size and churn (a running VM rewrites the disk image constantly, and
  Casper keeps 30 days of versions of the rewrite). Backblaze excludes the same
  file types by default (section 9). Named in the plan, since a VM can be the
  person's work.
- **No size rule** (4.2). Instead the plan lists the three biggest things it
  would cover ("Downloads 40 GB, Projects 22 GB") so one "leave out Downloads"
  fixes it in one pass.
- Photos: the library is the always-tier carve-out. A person's *exported*
  photos in `~/Pictures` are ordinary files and are covered.

### 6.5 Casper's own things (always tier)

Two conflicts that come with a home-folder root and don't today:

- **The recovery kit.** setup refuses to put the kit inside a mirrored folder
  (`insideAny`; the default is the Desktop or an external drive, or Documents in
  older versions). Under a home root, the Desktop *is* covered. Either the kit's
  file name is a built-in always-exclusion (`Casper Recovery Kit*`), or kit
  placement has to avoid home entirely. The first is simpler but means the kit
  isn't backed up by Casper, which is the point of it; the claim "your kit is
  saved" must still say where.
- **Restored copies.** Restores land as dated siblings (`Documents (as of ...)`,
  `~/Casper Restores/...`), which a home root would cover and mirror again.
  Probably fine for a short time, wasteful at 100 GB. Q7.
- **Casper's own data** (`Library/Application Support/Casper`,
  `%LOCALAPPDATA%\Casper`) and Syncthing's database are already inside the
  default-excluded Library/AppData; if a root were elsewhere they'd need
  their own always entry.

## 7. How new folders join, in detail

- **Inside a root** (`~/Projects/2027`): covered from the next scan or watcher
  event; nothing to do. No dialog. It shows up in the next status line and in
  `my_casper`.
- **A new top-level child of home** is just another directory of that root.
  Under option A no new Syncthing folder is created, so there's no new consent
  question. That is why A suits "protect my whole home".
- **A new root** (a second drive, `D:\`) is a normal Mirror gate: preview, size
  and fit, the Mac's dialog.
- **The standing-consent point.** The person's yes was to "my home folder minus
  X", so new folders under it are within what they agreed to, as long as
  Casper kept saying what was in scope (section 10) and told them when
  something large joined (5.3). That is the dynamic-consent reading in gates.md
  section 1, but it is a judgment: Q2.
- **Excluded thing becomes a folder you care about** (the person moves their
  novel into `Movies`). Nothing in Casper can notice. The claim machinery
  (section 10) is how they find out: "Movies isn't covered" is said whenever the
  scope is stated.

## 8. Offering it without twenty questions

The principles are agent-first-ux.md #11 (no more steps than the best GUI) and
gates.md (items are filled by words or by a yes; silence fills nothing; ask only
about what's open; one question at a time; no recap).

**Coverage is one gate item, "Folder", with a better default.** Today its
suggestion is Documents. The new suggestion is a *ladder* Casper climbs down by
itself until the first rung fits the space held, and it offers one rung:

1. Home, minus the default exclusions: "Your whole home folder, 84 GB, leaving
   out Applications, Library, Music and Movies?"
2. Documents and Desktop plus any other top-level folder under a size: "Documents,
   Desktop and Projects, 31 GB?"
3. Documents: "Documents, 12 GB?"

Casper chooses the rung from what it can look up (sizes, grants, free space);
it never asks the person for any of that. One question, one yes.

- **The exclusions are part of the sentence, not a second question.** A yes to
  rung 1 fills the "folder" item and the exclusion item at once (the default is
  affirmed). The exclusions the person would actually care about are named
  (section 10's `named` rule); caches and clutter are one phrase.
- **Adjusting is one pass, in words:** "Not Downloads" adds a personal
  exclusion; "include my Movies" removes one; "just Documents" is rung 3. Each
  reply re-previews with the new size and ends in the same one question.
  Casper never lists every exclusion and asks about each one.
- **If the person gave a scope first** ("back up my Projects folder"), that
  fills the item and the ladder isn't offered.
- **The picker, where choosing is faster than describing** (principle 11). On
  the Mac, the existing folder panel (`mirror_choose_folder`) shows home's
  children with their sizes, ticked by the defaults, with excluded items
  shown struck out and labelled with why (CrashPlan marks excluded items in
  its file picker; Arq uses checkboxes; section 9). The guide draws the same
  list inline from a listing the daemon sends, as the restore picker does. The
  tree is one level deep with sizes; the person ticks or unticks in one pass.
- **The macOS prompt note** goes in the Mirror dialog (5.1), not in chat.
- **Parity.** An outside agent and the guide use the same tools: `mirror_folder`
  gets a `coverage` argument (roots, personal exclusions, overrides) and its
  preview returns the covered size, the excluded size by rule, and the next
  rung if it doesn't fit. A new read-only `coverage_of(path)` answers "is X
  protected?" (covered / excluded by rule R, because Y / outside every root /
  unreadable). Nothing here works only in the guide.

## 9. How others do it

Surveyed 2026-10-17 from vendor documentation; I did not install any product.
Search-result summaries were checked against the fetched pages where a fetch
succeeded; the CrashPlan page I did not open beyond the search summary.

| Product | Model | Defaults | The person's controls | What to take |
|---|---|---|---|---|
| **Backblaze Personal Backup** | Whole computer by default; the user lists **exclusions** ("by default, you cannot select specific folders to back up"; to back up only some, exclude all the others) | Non-removable folder list: on Mac `/Applications/`, `/Library/`, `/Private/`, `/System/`, `/usr/`, `/bin/`, `/dev/`, `/etc/`, `/net/`, `/sbin/`, `/home/`; on Windows `\windows\`, `\program files\`, `\$recycle.bin\`, `\users\all users\microsoft\` and others. Editable file-type list (`.dmg`, `.iso`, `.exe`, `.log`, `.vmdk`, 30+ more). Also Time Machine and other backup sets, caches and thumbnails | Add folders to exclude; edit file types; max file size (default "No Limit", options 5 MB to 25,000 MB). "Excluding a folder excludes any folder with that name regardless of location." Excluded items are deleted from Backblaze after 30 days | The exact model of "home minus" and the best match for the Principal's example (same four exclusions, in effect). Take: defaults the person can't edit are system folders only; media types are editable. Don't take: exclusion-by-name-anywhere for personal folders (a footgun), and the silent deletion on exclusion without saying so |
| **Arq (7)** | Choose folders to back up (or all drives); then exclude *within* them | Docs don't list built-in exclusions | Four ways: uncheck items within a folder; wildcard rules (`?`, `*`, e.g. `*.log`, `/Users/alice/junk`); ICU regex rules; and **"skip items excluded by Time Machine rules"**, with a warning that it "may include things that you'd rather not exclude, such as your Photos Library's database" | A tree with checkboxes for the common case and rules for the rest, which is our picker plus patterns. The Time Machine warning is our Photos-library issue exactly: borrowed exclusions can drop something people care about, so we list ours and say why |
| **CrashPlan** | Select folders; defaults exclude OS and noise | Per its support docs: macOS `/System`, `/Applications`, `/Library`, `/bin`, `/sbin`; Time Machine backups and `.Trash`; media caches (Photos/iPhoto thumbnails, iTunes artwork, iMovie renders); Spotlight indexes, `.DS_Store`, `.fseventsd`; Windows `$RECYCLE.BIN`, `System Volume Information`, temp web data | In the file picker, "Manage Files" with hidden files shown marks excluded files with a small red circle. File-type/pattern include and exclude rules | Excluded items **visible in the picker, marked**, not just absent. We do the same, and add the reason |
| **Time Machine** | Everything on the Mac except a **system** list and the person's exclusions | System caches and rebuildable data excluded by Apple (e.g. `.DocumentRevisions-V100` in system exclusion list since Big Sur, per Eclectic Light). Plus a per-item **sticky exclusion** (an extended attribute that follows the item when it is moved or copied) vs a **fixed-path** exclusion (by location; stops applying if the item moves) | Options list in System Settings (paths); `tmutil addexclusion` for sticky or `-p` fixed-path | The sticky/fixed-path distinction is worth knowing: our exclusions are fixed-path (and pattern); a moved folder leaves its exclusion behind, which is a way for excluded to become covered or the reverse. The sticky attribute is a source of "an app told us not to back this up", honourable as a detected source (6.2) |
| **Carbon Copy Cloner** | A clone, a task filter chooses what is *not* copied | Automatic exclusions, explained in a KB article grouped by **reason**: filesystem internals, Spotlight/volume prefs, Apple-proprietary regenerable stores, volume caches that would make a volume unbootable, dynamic devices, large datastores (VM swap), Trash, Time Machine backups, "corrupted iCloud storage" | Per-task filter (unticking items turns them red); the Trash exclusion is changeable per task; SafetyNet keeps versions of changed/deleted files on the destination, and CCC never copies the SafetyNet folder | **Reasons shown with the exclusions** are the model for our catalog's `reason` field, and its grouping is our tiering. SafetyNet-style history is the thing Casper's mirrors already have. Note: its KB doesn't say whether its automatic list is visible in the app UI |

Cross-cutting findings:

1. Mature products are **exclusion-based over a wide root**, with a short
   non-removable system list and an editable media/type list. Home-minus is the
   norm, not an outlier.
2. Defaults converge on the same things: `/Applications`, `/Library`, caches,
   Trash, Spotlight/Time Machine internals, disk images. The Principal's list
   is the common one.
3. Visibility is the weak point of most of them: Backblaze's default list is a
   help article, Arq's warning is in docs, CCC's explains reasons but may not
   show them in the app. CrashPlan's marked-in-picker is the best. None of
   them, in what I read, **ties their status wording to the exclusion list**.
   That is the differentiator Casper's claims can have (section 10).
4. Nobody's docs address macOS privacy prompts or sync-provider placeholders as
   a coverage question; this is the part with the least prior art to copy.

## 10. How claims stay true

claims.md's rule applies unchanged: **a positive claim names its evidence and
its age; wording can only fail in the safe direction.** Coverage adds a new
fact to that, the *scope* of "protected".

- **"Protected" always carries its scope.** `FolderProtected` (claims.md 5.2)
  is about the covered set, and the sentence says what that set is:
  "Protected: your home folder, as of 14:02. Not included: Applications,
  Library, Music, Movies, and 3 folders of dependencies (6 GB)." Never "your
  files are protected" for a root with exclusions.
- **Excluded is never counted, and never hidden.** Excluded items are not in
  the numerator or the denominator of "every change is on at least one mirror".
  They are also not in the claim "nothing is lost", the strongest sentence in
  the product (claims C16): where a root has exclusions, that sentence is not
  said about the root.
- **Unreadable inside the set is not protected.** Unchanged from today
  (`unreadable_items` > 0 blocks "protected", the guide's `unreadable` step).
  Coverage adds the sources: a privacy denial (5.1), an iCloud-dataless file
  (6.3), a cap hit (4.2). Each is named by cause.
- **What must be named** (the `named` flag in the catalog): any exclusion that
  could hold something the person made or would miss: Library, Music, Movies,
  Downloads if excluded, cloud folders, VM images, the Photos library, and
  every personal exclusion. Pure regenerable clutter (caches, `node_modules`,
  `.DS_Store`) is aggregated into one count-and-size phrase, because naming 40
  cache folders is noise and *hiding* them is only wrong if one of them
  matters. Rule of thumb for catalog authors: if you'd be annoyed to find it
  missing, mark it `named`.
- **The scope itself needs evidence.** The coverage claim is true when what
  Syncthing is applying (the read-back of the ignore list, 4.2) matches the
  coverage Casper holds. A mismatch, or no read-back newer than the limit, makes
  the scope claim *unknown* and the status "Not confirmed since {as_of}". That
  catches an edited `.stignore` outside the managed block, a failed write, or
  an old daemon. Add `CoverageApplied(folder)` to the claim types, with the
  read-back as E2 evidence and a lease like the other status evidence.
- **Growth and quota are claims too.** "Protected" can't hold while covered
  size exceeds a mirror's space (5.3); the state is "N GB don't fit".
- **The answer to "is X protected?"** is the `coverage_of(path)` tool in section
  8: covered, or excluded by rule R for reason Y, or outside every root, or
  unreadable because Z. The agent says the result; it doesn't infer from "no
  warning".
- **History interacts.** `HistoryAvailable` (claims C8) is per covered path; an
  item newly excluded keeps no new versions, and the words must not say "30
  days of history" about it after the exclusion starts (rule 6, 2.2).
- **The consent record names the scope.** The Mirror dialog and the plan list
  the named exclusions, so "the person knew" has a record (gates.md consent
  receipt idea), and the later sentence "you chose to leave out Movies" is true.
- **Catalog entries that are wrong are claims that are wrong.** A built-in
  default that excludes something people need (a plausible one: `Library`
  holding a password database) is a claim failure, not just a design choice.
  The reason strings are reviewed like copy.

Places to change when this is built, by claims.md's own register: C1 (protected
wording), C16 ("nothing is gone"), C15 (recover offer lists the folders: should
list the scope), C17 (the app's status), and the guide gates that read the
`protected` fact.

## 11. Rough build order

Not a commitment; just an order that keeps each step small.

1. **Measure** (4.3) and resolve the `(verify)` items about Syncthing (ignored
   directories not walked, junctions, TCC behaviour). Everything else depends on
   whether A holds up.
2. **Coverage as data**: the schema, the catalog, rendering to the managed
   `.stignore` block, read-back. The Photos entry becomes catalog entry
   one with identical behaviour. Existing folders get `exclusions = []` and only
   "always" entries.
3. **Claims**: scope in the status sentence, `CoverageApplied`, `coverage_of`.
4. **Plan and preview**: covered and excluded size by rule, the ladder.
5. **Picker** on the Mac and in the guide; the dialog's prompt note and the
   daemon's pre-touch of Desktop/Documents/Downloads.
6. **Growth monitoring**: size against quota, large-new-folder notice.
7. **Windows** catalog and the iCloud/dataless detected exclusion.

## 12. Open questions for the Principal

1. **Home as a default, or Documents?** Should the first suggestion on a new
   account be "your home folder, minus the usual" (what this document assumes,
   and what Backblaze and Time Machine do), or stay "Documents" and make home an
   opt-in? Home is a bigger promise, touches more privacy prompts, and costs
   friends more disk.
2. **Is a new folder under a covered root within the person's yes?** I assumed
   yes, with a notice only for large additions. The alternative, asking each
   time, defeats the point. Is there a size, or a kind of place (a new
   top-level folder next to Documents, say), where we should ask anyway?
3. **Library.** Excluding it also leaves out the login keychain, Mail,
   Messages and Safari data. Are you OK with Library excluded in full, with
   that named? Or should a few places be offered back (needs re-includes, which
   2.2 rule 2 defers)? Related: should Casper ever say anything about
   passwords/keychain being outside?
4. **Music and Movies:** excluded by default always (as in your example), only
   when large, or only offered? Same for Downloads, which I did not exclude.
5. **Cloud folders:** leave out by default and say so (my recommendation), or
   include them when fully downloaded? Do we want to tell people "sync isn't
   backup" at all, or is that outside Casper's voice?
6. **When coverage stops, what happens to the copies?** Keep for 30 days (my
   assumption, same as stopping a mirror), delete at once, or keep until space
   is needed? Backblaze deletes after 30 days.
7. **Casper's own recovery kit and restored copies.** OK to exclude kit files
   by name (so the kit stays out of the mirror even on the Desktop), and should
   restored dated copies be covered like anything else or excluded from the
   mirror?
8. **Credentials in dotfiles** (`.ssh`, `.aws`, tokens). They are encrypted
   before leaving the Mac, but they do leave it, to friends' disks. Mirror them
   by default, exclude by default, or name them in the plan?
9. **Where does the line sit between "friend's disk" and "I want everything"?**
   When covered size outgrows a friend's quota, is "pause and ask" the right
   behaviour, or should Casper keep the oldest/most-valued folders and drop the
   newest automatically? I recommended never dropping silently.
10. **Option A vs B.** If measurements say a whole-home Syncthing folder is
    heavy, are you willing to take the extra machinery of per-child folders
    (and a consent decision for each new top-level folder), or would you
    rather cap what "home" can mean (for example, refuse above N files and offer
    folders instead)?

## Sources

- [Syncthing: Ignoring Files](https://docs.syncthing.net/users/ignoring.html)
- [Syncthing: Configuration (folder watcher, rescan)](https://docs.syncthing.net/users/config.html)
- [Syncthing: FAQ (scanning, nested folders, inotify)](https://docs.syncthing.net/users/faq.html)
- [Backblaze: Configure Exclusions (Mac)](https://www.backblaze.com/computer-backup/docs/configure-exclusions-mac)
- [Backblaze: Exclusions Settings (Mac)](https://help.backblaze.com/hc/articles/217666298-Exclusions-Settings-Mac-)
- [Backblaze: Exclusions Settings (Win)](https://help.backblaze.com/hc/articles/217666538-Exclusions-Settings-Win-)
- [Arq 7: Excluding Files from Backup](https://arqbackup.com/documentation/arq7/English.lproj/excludeFiles.html)
- [CrashPlan: Understanding default file exclusions](https://support.crashplan.com/hc/en-us/articles/8693725623949)
- [Eclectic Light: Knowing what not to back up, and how](https://eclecticlight.co/2021/08/03/knowing-what-not-to-back-up-and-how/)
- [Eclectic Light: Excluding folders and files from Time Machine, Spotlight, and iCloud Drive](https://eclecticlight.co/2024/07/09/excluding-folders-and-files-from-time-machine-spotlight-and-icloud-drive/)
- [alexwlchan: exclude files from Time Machine with tmutil](https://alexwlchan.net/til/2024/exclude-files-from-time-machine-with-tmutil/)
- [Carbon Copy Cloner 5: Some files and folders are automatically excluded](https://bombich.com/kb/ccc5/some-files-and-folders-are-automatically-excluded-from-backup-task)
- [Apple Support: Control access to files and folders on Mac](https://support.apple.com/guide/mac-help/control-access-to-files-and-folders-on-mac-mchld5a35146/mac)
- [Cache Directory Tagging Specification](https://bford.info/cachedir/)
