# Protecting a Mac's Photos library: the problem and prior art

Research for a design conversation, written 2026-10-16. Photos were left out
of v1 ([scenarios/mirroring.md](../scenarios/mirroring.md), decision 3: the
defaults are Documents and Desktop). This is what it would take to bring them
in, what other products do, and what we could honestly promise.

How to read it: a claim with a link is from that source. Anything marked
**(inference)** is ours, drawn from the sources but not stated by them.
**(unverified)** means we could not confirm it from a primary source and it
needs a test on a real Mac before we rely on it.

Related: [values-and-goals.md](../values-and-goals.md),
[recovery-v2-design.md](../recovery-v2-design.md),
[restore-design.md](../restore-design.md),
[scenarios/mirroring.md](../scenarios/mirroring.md).

## 1. The problem

### 1.1 What the library is

- By default the library is one package, `Photos Library.photoslibrary`, in
  `~/Pictures`. Apple's own backup instruction is to "drag the photo library
  (stored by default in the Pictures folder on your Mac) to your storage
  device" ([Apple: back up your photo library](https://support.apple.com/guide/photos/back-up-your-library-pht6d60d10f/mac)).
  To Finder it is one file; underneath it is a folder tree.
- Inside (community reverse engineering, since Apple does not document it):
  `database/` (SQLite; `Photos.sqlite` holds the photo metadata, `photos.db`
  the library's own), `originals/` (unmodified files, named by UUID, not by the
  camera's filename), `resources/` (edits and edit data, shared-album files,
  derivatives, audit logs) and `private/` (config, state, caches)
  ([osxphotos wiki](https://github.com/RhetTbull/osxphotos/wiki/Understanding-the-Photos-database),
  [forensics notes](https://github.com/muxcmux/apple-photos-forensics)).
  Derivatives hold a medium-size version of each photo and a thumbnail
  ([search summary of the same sources](https://github.com/RhetTbull/osxphotos/wiki/Understanding-the-Photos-database)).
- **Everything a person cares about beyond the pixels** (albums, faces,
  keywords, captions, favorites, edits, the "which original goes with which
  edit") lives in that database and in `resources/`. Backblaze says it
  plainly: "the associated metadata is lost without the entire Photos Library
  package" ([Backblaze restore doc](https://www.backblaze.com/computer-backup/docs/create-a-restore-of-the-photos-library-mac)).
  A pile of restored JPEGs is not the library. Ente's migration doc lists what
  does not survive an export: favorites, album membership and People tags "remain
  Apple-only" ([Ente](https://ente.com/help/photos/migration/from-apple-photos/)).
- Photos can also hold **referenced** originals stored outside the package;
  Apple notes these "aren't included in library backups" and need their own
  backup ([Apple](https://support.apple.com/guide/photos/back-up-your-library-pht6d60d10f/mac)).
- Size: libraries are the largest single item most people own. We have no
  authoritative figure. One user writing about exporting for Immich calls
  800 GB "large" and splits the export by year
  ([Immich discussion](https://github.com/immich-app/immich/discussions/21251)).
  The size on a given Mac is the number to measure, not assume (see 1.6).

### 1.2 A live database

- The database is SQLite, open and written by Photos and by background
  daemons (`photolibraryd`; **(inference)**: they run with Photos closed too,
  which is why "quit Photos" is not a guarantee of quiet).
- SQLite's own warning applies exactly: background backup software "might try
  to make a backup copy of an SQLite database file while it is in the middle of
  a transaction. The backup copy then might contain some old and some new
  content, and thus be corrupt"; copying the database without its `-wal`/
  `-journal` file is "likely to lead to corruption"
  ([SQLite: how to corrupt](https://www.sqlite.org/howtocorrupt.html)).
  The safe routes are SQLite's backup API, `VACUUM INTO` or `sqlite3_rsync`,
  or a snapshot of the volume.
- Our existing mirror is file-level (Syncthing). Syncthing sees each file
  change separately and has no notion of "these three files must match." A
  library mirrored live can hold a database from one moment and originals from
  another, and a restored copy may fail Photos' own consistency check.
  **(inference; unverified on Photos specifically.)** No Syncthing source we
  found discusses this. The one Photos thread on the Syncthing forum is only a
  permission error ([forum](https://forum.syncthing.net/t/sync-apple-photos-library-on-macos-to-syncthing-server/18528)).
- Apple's own guidance steers away from live-synced copies: don't keep the
  library on "a device shared over your network or the internet, including
  over a cloud-based storage service"
  ([Apple: move your Photos library](https://support.apple.com/en-us/108345)).
  That is about *running* a library from such a place, not about keeping a
  copy there. But it tells us Apple does not support two writers, and a
  mirrored folder that a second Mac writes into is exactly two writers.
- Time Machine is Apple's sanctioned route and has its own sharp edge: Apple
  says not to put the library on a drive that is also a Time Machine
  destination ([Apple](https://support.apple.com/en-us/108345)), and to restore,
  quit Photos and restore the package from Time Machine
  ([Apple: restore from Time Machine](https://support.apple.com/guide/photos/restore-from-time-machine-pht6d60348f/mac)).
  Apple also warns that restoring a System Photo Library that uses iCloud
  Photos brings back items deleted since the backup and uploads them to iCloud
  again (same page).

### 1.3 Permissions: TCC, Full Disk Access, PhotoKit

- macOS gates the library with TCC. **First-hand check, 2026-10-16**: from an
  ordinary terminal-launched process on this Mac (macOS 15.6),
  `ls "~/Pictures/Photos Library.photoslibrary"` fails with "Operation not
  permitted", while the rest of `~/Pictures` lists. Casper's daemon runs
  confined to the home directory, so it will hit the same wall until it holds a
  grant.
- Evidence on which grant is enough is mixed:
  - Kopia: granting Full Disk Access fixed it
    ([Kopia forum](https://kopia.discourse.group/t/kopiaui-does-not-have-access-to-photos-on-mac/3632)).
  - Duplicati: Full Disk Access did **not** work for the GUI build until the
    app declared `NSPhotoLibraryUsageDescription`, and its developers
    concluded the Photos API is the reliable route
    ([Duplicati forum](https://forum.duplicati.com/t/macos-access-denied-to-backup-fotos-library-photoslibrary/20745)).
  - Arq ships dialogs that walk people through granting Full Disk Access
    (Arq docs, via [search summary](https://www.arqbackup.com/)).
  - **(unverified)**: whether Full Disk Access reliably covers the package on
    macOS 15/26 for a notarized helper like ours, or whether a separate
    "Photos" grant is needed, and whether the grants are per bundle (the
    daemon vs. the app). Test on a clean VM before promising anything.
- **Either way the person has to say yes in System Settings**, and Apple's
  consent prompts are not something an agent can click for them. That is the
  same shape as the folder-consent dialog we already own, and it fits the
  "consent answered outside the agent" rule.
- **PhotoKit** is the sanctioned API: it exposes assets and, per asset, the
  underlying resources, and can download them from iCloud
  (`PHAssetResourceRequestOptions.isNetworkAccessAllowed`:
  [Apple docs](https://developer.apple.com/documentation/photos/phassetresourcerequestoptions.md)).
  It needs a Photos usage description and the person's authorization. It gives
  originals and, where Photos holds them, edits; it does **not** give the
  database, albums-with-edits-structure or faces as Photos stores them
  **(inference from the API's shape)**. osxphotos' own README notes that its
  `--use-photokit` path is "experimental" and that the AppleScript route "is
  somewhat buggy" ([osxphotos](https://github.com/RhetTbull/osxphotos)).

### 1.4 iCloud Photos and Optimize Mac Storage

- iCloud Photos is **sync, not backup**. Apple: photos in iCloud Photos "are
  not duplicated in your iCloud backup"; "when you delete photos and videos on
  one device, they're deleted everywhere"; deleted items stay in Recently
  Deleted for 30 days, then are gone
  ([Apple: iCloud Photos](https://support.apple.com/en-us/108782)). Apple tells
  people to keep a local backup even with iCloud Photos
  ([Apple](https://support.apple.com/guide/photos/back-up-your-library-pht6d60d10f/mac)).
- **Optimize Mac Storage** keeps "smaller versions of your photos on your Mac
  when storage space is limited, and keeps the original, full-size photos in
  iCloud" ([Apple](https://support.apple.com/guide/photos/optimize-storage-in-photos-on-mac-phta9b4673b4/mac)).
  So on an optimized Mac, the package is **not the library**: copying it
  protects thumbnails and metadata, not originals.
- The trap, from Bombich: iCloud Photos "doesn't use the cloud-only
  placeholder trick that the other services do. Instead it keeps optimized,
  low-resolution versions of your photos in the library" as ordinary files, so
  backup software "cannot tell" a stand-in from an original
  ([Bombich](https://bombich.com/blog/2026/07/28/local-backups-of-cloud-storage)).
  Backup tools therefore **report success while holding only the small
  versions**. This is the single biggest source of false reassurance in the
  space.
- The remedy everyone gives is the same: choose "Download Originals to this
  Mac" ([Apple](https://support.apple.com/en-us/111762), Bombich, Backblaze,
  CrashPlan), at the cost of local disk. Photos' public API for this is
  per-asset download through PhotoKit (above); there is no documented
  switch an agent can flip. The person flips it in Photos > Settings > iCloud.
- Whether Casper can *detect* "Optimize is on and originals are missing":
  osxphotos reports missing originals by reading the database
  ([osxphotos](https://github.com/RhetTbull/osxphotos)), so it's detectable
  from Photos.sqlite (needs the read access of 1.3) or from PhotoKit
  (`isNetworkAccessAllowed = false` fails for non-local resources)
  **(inference)**. The Optimize setting itself is a Photos preference
  **(unverified how to read it)**.

### 1.5 Shared libraries and shared albums

- **iCloud Shared Photo Library**: up to six people; "the person who set up the
  Shared Library... provides iCloud storage for all of the content"; everyone
  can add, edit and delete; if the creator runs out of iCloud storage, nobody
  can add ([Apple](https://support.apple.com/en-us/123926)). Leaving gives you
  everything if you were in more than seven days, otherwise only what you
  contributed. A deletion by anyone goes to Recently Deleted, and only the
  contributor can purge it from there (same page).
- Shared items appear **inside the same System Photo Library** on each Mac.
  Whether they are in the package as ordinary originals is **(unverified)**.
  osxphotos' README says it "cannot read shared albums on macOS 26.x"
  ([osxphotos](https://github.com/RhetTbull/osxphotos)).
- Consequences: a person's library on Casper may contain **other people's
  photos**, which they can lose when the creator leaves or deletes the library.
  Mirroring them to a friend means a *third* person now holds copies of
  strangers' pictures, encrypted but real. That touches the trust framework
  and the promise we make to the person. See option 4.
- **iCloud Shared Albums** are lower quality by design: Ente notes they cap at
  2,048 px and 720p video and strip location data
  ([Ente](https://ente.com/help/photos/migration/from-apple-photos/)). They are
  a sync convenience, not an archive.

### 1.6 Where the library can live, and what Apple says about it

- Only the **System Photo Library** syncs with iCloud Photos, Shared Albums and
  the shared library; other libraries are just local ([Apple: overview](https://support.apple.com/guide/photos/photo-library-overview-pht211de786/mac)).
  A person may have several libraries; most have one.
- Apple's supported homes: the Mac's own disk, or a directly attached external
  drive in APFS or Mac OS Extended (Journaled). Not SD cards or USB sticks, not
  network or cloud-synced locations, not a Time Machine disk. If the external
  drive isn't there when Photos opens, Photos "will cease using that library
  and instead create a new empty one" ([Apple](https://support.apple.com/en-us/108345)).
  Implication: the library's path varies per Mac and may be on an external
  volume outside the home directory that our daemon is confined to **(inference)**.
- Apple recommends a **local** backup in addition to iCloud, by manual copy or
  Time Machine. It publishes no API or sanctioned "online backup" for the
  package. Nothing we found says third-party backup of the package is
  forbidden, and Apple's Time Machine backs it up as plain files.

## 2. How others handle it

| Product | What it does with Photos | What it tells the person | Where it falls short |
|---|---|---|---|
| **Time Machine** (Apple) | Backs up the package as files; restore is "quit Photos, restore the package" ([Apple](https://support.apple.com/guide/photos/restore-from-time-machine-pht6d60348f/mac)). | Apple names it as the recommended local backup. Warns about restoring with iCloud Photos on (deleted items return and re-upload). | Same Optimize trap; Apple says not to put the library on the backup disk; restores the whole library, not one photo (Photos has no per-photo import from a library backup, other than opening the old library as a second library **(unverified)**). Michael Tsai's complaint: no practical official route for huge libraries on external drives ([mjtsai](https://mjtsai.com/blog/2017/09/28/time-machine-vs-photos-app)). |
| **Backblaze** | Backs up the package "at any size" as a bundle ([supported data](https://www.backblaze.com/computer-backup/docs/supported-backup-data)); an entire package can be excluded in settings. Restore: get the whole package as a ZIP, move it to `~/Pictures` ([restore doc](https://www.backblaze.com/computer-backup/docs/create-a-restore-of-the-photos-library-mac)). | "If you're using Backblaze, we back up the Photos library for you"; tells people to choose "Download Originals to this Mac" and keep a local copy too ([Backblaze blog](https://www.backblaze.com/blog/back-photos-library/)). | The Optimize caveat is in a blog post, not in the product. The restore doc is silent on Photos being open, iCloud, and Optimize. No per-photo restore. Backblaze also now skips cloud-placeholder files ([discussion](https://talk.macpowerusers.com/t/backblaze-no-longer-backing-up-cloud-based-files/45082)); that is iCloud Drive, not Photos, but people conflate them. |
| **Arq** | Backs up whatever is on disk, so the package like any folder. Offers three modes for cloud placeholders (error, skip, materialize and re-evict) ([Arq docs](https://www.arqbackup.com/documentation/arq7/English.lproj/datalessFiles.html)); Photos is not mentioned there. | Warns that "skip items excluded by Time Machine rules" "may include things that you'd rather not exclude, such as your Photos Library's database" ([Arq](https://www.arqbackup.com/documentation/arq7/English.lproj/excludeFiles.html)). | Placeholder handling does not apply to Optimize (not placeholders). It reads the live package from a snapshot where it can. **(unverified for Photos specifically.)** |
| **CrashPlan** | Includes photo libraries in the default selection; documents back up and restore of the library ([CrashPlan](https://support.crashplan.com/hc/en-us/articles/8797997548301-Back-up-and-restore-photo-libraries); we could not fetch the page, only the search summary). Its consumer product was discontinued in 2018 ([TidBITS](https://tidbits.com/2017/08/22/crashplan-discontinues-consumer-backups/)). | "If you haven't changed the default location, your photos are already backing up." | Default-on with no mention of Optimize in what we could read. |
| **Carbon Copy Cloner** | Clones the package as files; incremental. States the Optimize problem outright: it "cannot tell that a given file is not full resolution," and says to pick "Download Originals to this Mac" ([Bombich](https://support.bombich.com/hc/en-us/articles/20686419951767-Backing-up-the-content-of-cloud-storage-volumes), via search summary; [blog](https://bombich.com/blog/2026/07/28/local-backups-of-cloud-storage)). | The most honest wording we found. CCC also skips 0-byte cloud stubs because "they provide false security". | Honest about the limit, but cannot fix it. |
| **SuperDuper** | Clones the volume, packages included. We found only a forum thread about iCloud and SuperDuper marking files modified ([forum](https://blog.shirt-pocket.com/forums/showthread.php?p=34268)). No Photos-specific guidance found. | Unknown. | Not researched further. |
| **Dropbox Camera Uploads** | Uploads a *device's* camera roll when plugged into a Mac with the desktop app (needs "files on a removable volume" permission); lands in a `Camera Uploads` folder ([Dropbox](https://help.dropbox.com/create-upload/camera-uploads-overview)). Does not read the Mac's Photos library. | "Uploading your photos... ensures your files are securely backed up." | Phone-to-cloud, not Mac library. Putting the package inside a Dropbox/OneDrive folder is the pattern Apple warns against; guides tell people to export to the folder instead ([example](https://www.wps.com/office-quick-solution/document-file-issues/fix-apple-photos-library-cannot-open-in-onedrive/)). |
| **Google Photos** | The Google Drive desktop app could back up the Mac's System Photo Library to Google Photos; Google Photos' own folder backup says "Apple Photos Library is not supported when you back up folders from photos.google.com," and Drive for desktop "ends support for Google Photos backup on August 10, 2026" ([Google](https://support.google.com/photos/answer/6193313?hl=en&co=GENIE.Platform%3DDesktop)). | Google is steering people to the iPhone app. | Not available for Mac libraries going forward. The one mainstream "Mac library to a photo cloud" path closed this summer. |
| **Amazon Photos** | Desktop app uploads selected folders; Prime has unlimited full-resolution photos ([TechRadar](https://www.techradar.com/reviews/amazon-photos)). We found no Apple Photos library integration. | n/a | Folder upload of the package is not a library backup. |
| **Ente** | Preferred route is the iPhone app reading iCloud directly; on a Mac, export "Unmodified Originals" from Photos and import ([Ente](https://ente.com/help/photos/migration/from-apple-photos/)). End-to-end encrypted. | Says plainly which things don't carry over (favorites, albums, People). | A migration, not a protection of the Mac's library. Keeps a copy of the pictures, not of Photos. |
| **Immich** (self-hosted) | No library backup. Common recipe: set "Download Originals", export with osxphotos (XMP sidecars), upload with the Immich CLI; or the iOS app backs up from the phone ([discussion](https://github.com/immich-app/immich/discussions/21251)). | A user warns that without osxphotos "a HIGH number of your assets will miss metadata"; mismatches can create duplicates. | Manual, one-way; the result is not a Photos library. |
| **PhotoSync** | Moves photos between phone, Mac and NAS/cloud; its Mac companion can use Photos as a transfer *target* ([PhotoSync](https://www.photosync-app.com/support/ios/answers/how-to-transfer-into-the-photos-app-on-my-mac)). | n/a | Transfer tool, not a backup of the Mac library. Technical details of its Photos access were not found. |
| **osxphotos** | Open-source (Python): reads the database directly, exports originals and edits with metadata; `--update` gives incremental exports "for periodic backups"; `--download-missing` fetches iCloud-only originals through AppleScript (buggy, crashes) or experimental PhotoKit ([README](https://github.com/RhetTbull/osxphotos)). | Honest list of limits: missing photos on Optimize, shared albums not readable on macOS 26. | It reverse-engineers an undocumented schema; each macOS release can break it (supported "through macOS Sequoia"). The most thorough export, not a restorable library. |
| **Syncthing users** | Same permission wall as ours, and no one in the thread discusses consistency ([forum](https://forum.syncthing.net/t/sync-apple-photos-library-on-macos-to-syncthing-server/18528)). Separately, Syncthing's Android app once uploaded half-written photos because it read files before they were finished, fixed with a delay ([forum](https://forum.syncthing.net/t/images-synchronized-using-syncthing-appear-corrupted/11042)). | n/a | The unexamined case, and ours. |

Patterns:

1. **Everyone who backs up the package treats it as a folder**, and most of
   them are honest only in a blog post or knowledge-base article about
   Optimize Mac Storage. In-product warnings are rare.
2. **The Optimize trap is universal**, and is the thing a plain mirror would
   most plausibly get wrong while reporting "Protected."
3. **Tools that care about portability export files plus sidecars** (Ente,
   Immich, osxphotos). They protect the pictures and *lose or flatten the
   Photos-specific structure*, and they say so.
4. **Restore is whole-library everywhere** except exports, where it is
   per-file but not into Photos' structure. Nobody offers "get one photo
   back into Photos from backup".
5. **Google just exited the Mac library case.** The remaining sanctioned story
   is Apple's: iCloud Photos plus a local backup you manage.

## 3. What this means for Casper

What our mirror already does well: continuous peer copies, ciphertext only on
friends' Macs, 30 days of history, restore driven by an agent or the guide
(`list_versions`, `restore_as_of`). Where Photos strains each:

- **Consistency.** A live Syncthing mirror of the package can capture a database
  inconsistent with `originals/`. History helps (30 days of versions), but
  restoring "as of a time" picks files by time and cannot know which instant
  was consistent. Mitigation we'd need: only mirror when Photos and its daemons
  are idle, or mirror a snapshot (below).
- **Permission.** Requires a one-time Apple grant the person must make.
- **Optimize.** A mirror of an optimized Mac is a mirror of the thumbnails.
- **Size.** Peer space is donated by friends; a Photos library may be several
  times Documents and Desktop combined, so the "20 GB on this Mac?" kind of
  ask changes character.
- **Restore.** A restored package must land where Photos will accept it (quit
  Photos; as the System Photo Library; with iCloud Photos on, deleted items
  come back and re-upload, Apple's own warning). Our "dated copy next to the
  original" default is natural for a folder and wrong for a package: a second
  library next to the first is fine, but it is *a second library*, not the
  restored one.
- **No conflict with Apple's services.** Apple's services are iCloud Photos
  (sync) and iCloud Shared Library. We must not read from, write to or
  "repair" the person's iCloud data, must not suggest turning iCloud Photos
  off, and must not host a library someone then *runs* from a friend's Mac
  (Apple says not to run a library from network/cloud storage). A copy kept
  for recovery doesn't do any of that.

## 4. Options

Each lists what we could truthfully say to the person. The wording is a draft
in Casper's voice.

### Option A: Say no, and point to Apple's own route

Don't touch Photos. When asked, say what Apple recommends and offer to help
with the parts we can (the Documents/Desktop copy continues).

- **Claim:** "Photos aren't something I back up. Apple's iCloud Photos keeps
  them, and Time Machine on a drive keeps a second copy."
- **Cost:** low. **Helpfulness:** low, and this is the thing people most
  fear losing. **False-impression risk:** none, as long as we don't imply
  iCloud is a backup (it isn't: see 1.4).
- Good as the floor: the agent should never answer "back up my Photos" with
  silence or with a mirror of the package it cannot stand behind.

### Option B: Mirror the package as a folder, honestly gated

Treat `Photos Library.photoslibrary` like any folder, once the person has
granted access, and **check before claiming**:

- If iCloud Photos is on and Optimize is on: refuse to call it protected
  (or offer only "thumbnails and albums are protected, originals aren't").
- If Download Originals is on or iCloud Photos is off: mirror, but only when
  Photos is quiet (closed and database idle), and call it "a copy of the
  library as of the last quiet moment".
- Restore only as the whole library, into a dated sibling library, with
  Photos closed.
- **Claim (when the checks pass):** "Your library is copied to Sam's Mac as it
  stood when Photos was last closed, with 30 days of history. To get it back,
  we bring the whole library back and open it in Photos."
- **Claim (Optimize on):** "Your Mac only keeps small versions of most
  photos; the originals are in iCloud. I can copy what's here, but that isn't
  your photos. Switching to 'Download Originals' needs [N] GB free."
- **Cost:** moderate (permission, quiet-state detection, a photos-aware
  restore). **Risk:** the consistency problem in 1.2 is not solved, only
  narrowed; unverified until tested. **Helpfulness:** high, and is the same
  thing Backblaze/CrashPlan/CCC do, with better disclosure.

### Option C: Mirror a consistent snapshot, not the live package

Add a daemon step that makes a consistent copy first (APFS snapshot of the
volume, or SQLite backup API for the database plus a copy of `originals/` and
`resources/`) into a staging folder, and mirror *that*. Same permission need.

- **Claim:** same as B, but "consistent copy" is then true rather than best
  effort: "A full copy as of last night, taken so Photos can open it."
- **Cost:** high (new daemon capability, staging disk equal to the library
  unless APFS clones are used; clones may help **(unverified)**). **Risk:** we
  start owning knowledge of an undocumented structure that changes with macOS
  releases; the same fragility that limits osxphotos.
- Best long-term for the package; only worth it if B tests badly.

### Option D: Protect the pictures, not Photos (export route)

Use PhotoKit (a proper Photos permission, the sanctioned API) to export every
original, edit and sidecar metadata into a plain folder the mirror already
handles (like osxphotos, Ente and Immich recipes). The mirrored folder is
ordinary files, restorable per photo, readable on any computer, and it can
include iCloud-only originals because PhotoKit can fetch them (needs
network and disk during export, and the person's permission).

- **Claim:** "Every photo and video, with dates and places, is copied to
  Sam's Mac. If your Mac is lost, you get them back as files, but not your
  albums, faces or edits as they are in Photos."
- **Cost:** moderate to high (a PhotoKit exporter, incremental, big
  first-run). **Helpfulness:** high for the real fear (losing the pictures),
  and the only option that solves Optimize, because PhotoKit can request the
  original. It also supports per-photo restore, which our restore model
  already does well.
- **Risk:** a person may believe "Photos is backed up." We must not let them.
  It also doubles disk use if the staging folder isn't cleaned.
- **Shared Library:** exported items would include other people's photos.
  Policy needed: probably export only what the person contributed, or ask.

### Option E: D plus B or C, later

Ship D first (pictures, per-photo restore), add C later as "your whole library
for a full recovery" when the structure is understood.

## 5. Recommendation

**Do A now (as the stated behavior of the agent today), then build D, then
consider C. Do not ship plain B.**

Reasons:

1. The failure we most want to avoid is a green "Protected" over thumbnails
   or an inconsistent database. B's plain form risks both and the evidence
   that the market treats this as acceptable is the same evidence that it
   quietly fails (Bombich says as much). D is the only option that can truthfully
   promise the *originals*, including iCloud-only ones, because it asks Photos
   for them.
2. D uses Apple's sanctioned API, needs one Photos permission, doesn't touch
   iCloud data, and doesn't depend on an undocumented schema. It fits "no
   conflict with Apple's services" best.
3. D reuses what we have: a mirrored folder, 30 days of history, per-file
   restore through `restore_version`/`restore_as_of`. The guide's picker can
   list photos by date.
4. What D can't do (albums, faces, edits as Photos shows them) is stated up
   front, exactly as Ente and CCC state their limits. C answers that later.

Before building anything, run these cheap tests on a VM (they decide B vs C
and D's design):

1. Does Full Disk Access alone let our daemon read the package on macOS 15 and
   26, or does it need a Photos grant? Per bundle?
2. Mirror a library while Photos edits/imports; restore it; does Photos
   open it clean? How often does the database and originals diverge?
3. On an Optimize Mac, how does a PhotoKit resource request behave for
   non-local originals, and what are the real throughput and disk needs?
4. Can we read the Optimize setting, and "originals missing" counts, reliably?
5. Are Shared Library items in the package as normal originals, and does
   PhotoKit expose who contributed them?
6. Size distribution: how big are the libraries of the people we're inviting?

**What we would say**, in one line, to the person today: "I can't protect your
Photos yet. iCloud Photos is a sync, not a backup; I'll tell you when I can."
And once D exists: the claim in Option D above, with the "not your albums,
faces or edits" part kept in the sentence every time, not in a help page.

## 6. Gaps in this research

- We could not fetch CrashPlan's article (403); its entry rests on a search
  summary. Arq's Photos handling beyond the two pages above, SuperDuper's, and
  PhotoSync's PhotoKit use were not found.
- Apple does not document the package. Everything about its contents rests on
  community reverse engineering, and on macOS 26 (Tahoe) is least certain.
- The TCC behavior (1.3) has one first-hand observation and conflicting
  third-party reports; treat as to-be-tested.
- Library size has no source beyond anecdote; measure real libraries.
- Nothing here tested Syncthing against a live Photos library. That test
  would turn the consistency concern from inference into fact either way.
