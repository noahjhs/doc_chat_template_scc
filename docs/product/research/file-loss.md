# How people lose files, and how other products give them back

Research for the pilot, 2026-10-17. Docs only; nothing here changes code.
Part 1 is the loss scenarios and how common they are. Part 2 compares how
twelve products handle each, with the question that matters most for us:
**where do restored files go?** Part 3 says what that implies for Casper's
restore ([restore-design.md](../restore-design.md), [recovery.md](../recovery.md)).

**Reading the numbers.** Nearly every statistic is from a vendor that sells
backup or recovery, and most ask "have you *ever* lost data", not a per-year
rate. Each figure is marked **V** (vendor or vendor-commissioned), **I**
(independent or academic) or **S** (secondary: I saw it only through a
summary or a news report, not at the source). Gaps are said plainly. None
of the vendor documentation was tested by hand; vendor pages change.

## Part 1. How people lose files

### 1.1 How many people lose data at all

- 54% of US computer owners report data loss; 67% have accidentally deleted
  files; 48% have had an external drive fail (21% in the last year); 44%
  lost access to data through shared or synced drives being deleted; 10%
  back up daily and 20% never have. Backblaze / Harris Poll, May 2022, 1,861
  computer owners, ±2.8 pts. **V** (professional pollster, checked at
  source). <https://backblaze.com/blog/the-2022-backup-survey-54-report-data-loss-with-only-10-backing-up-daily/>
- 63% of US adults have lost personal digital data. Causes among them:
  hardware failure 20%, "system malfunction" 20%, accidental deletion 19%.
  **89% of those who lost data lost photos**; 56% of recovery attempts
  failed; smartphone loss (45%) was about as common as hard-drive loss
  (44%). CleverFiles (Disk Drill), April 2020, SurveyMonkey panel, sample
  size not stated. **V** (checked at source).
  <https://www.cleverfiles.com/howto/cleverfiles-data-recovery-survey-2020.html>
- Acronis World Backup Day surveys give anything from 34% to 65% for
  "experienced loss in a year" depending on year and wording. **V, S.** The
  spread is the lesson: the headline "half of people" is soft.
  <https://www.acronis.com/en-us/blog/posts/exploring-recent-trends-acronis-world-backup-day-survey>
- Qualitative: Marshall's field studies of home users find irreplaceable
  photos and records already lost, and practice made of "benign neglect,
  sporadic backups, and unsystematic file replication". Interviews, small
  samples, 2000s. **I.**
  <https://dlib.org/dlib/march08/marshall/03marshall-pt1.html>

**What is solid:** roughly half to two-thirds of people say they have lost
data; accidental deletion is the event people recall most; photos are what
they lose; most people back up rarely. **What is not:** any per-year
household rate, and anything split by population (below).

### 1.2 Taxonomy

| # | Scenario | How common, and the evidence | Who (as far as the data goes) |
|---|---|---|---|
| 1 | **Accidental deletion** (incl. emptied trash, deleted folder) | The most-recalled event: 67% have done it (Backblaze/Harris 2022, **V**); 19% of those who lost data (CleverFiles, **V**). In SaaS, the top cause at 41–43% (Spanning, 2016, IT staff, **V, S**). People over-blame themselves: in Kroll Ontrack customers 40% said human error but only 27% could be sure (2010, **V, S**). | Everyone; small businesses name it first (Carbonite 2015, 125 firms, 54%, **V, S**). |
| 2 | **Overwrite / bad edit** (saved over the good version, wrong "Save As", a find-and-replace) | Not measured separately; grouped with deletion. SaaS overwrite 26–27% (Spanning, **V, S**). No consumer rate found. | Anyone who edits a long-lived document: students (thesis), small businesses (spreadsheets), creatives (project files). Evidence is mechanism, not counts. |
| 3 | **Sync propagating a deletion or corruption** | 44% lost data through deleted shared/synced drives (Backblaze/Harris 2022, **V**). Nobody measures how often a sync client spreads a mistake; "sync is not backup" rests on mechanism and vendor blogs. Aberdeen's "one in three businesses permanently lost SaaS data" could not be traced to its source (**S**). | Anyone with Dropbox/iCloud/OneDrive; small businesses with shared folders. |
| 4 | **App / file corruption** (crash mid-save, bad update, failed database) | Kroll Ontrack cites software failure at about 6% of lab cases in 2014 (**V, S**); photographers: software corruption 13% of computer losses (RPS/Verbatim, about 1,800 European photographers, **V, S**). | Creatives with large project files (Lightroom/Photos libraries, video); developers (local databases). |
| 5 | **Ransomware** | Enterprise and SMB figures only: ransomware in 44% of breaches and 88% of SMB breaches (Verizon DBIR 2025, 12,195 breaches; counts investigated breaches, not the share of firms hit; **I**, S); in 2025 only 49% of attacks ended with data encrypted, 53% recovered from backups (Sophos, victims of 100+ employee firms, **V**); 23% paid in Q3 2025 (Coveware, **V, S**). **No consumer rate exists.** | Small businesses by far. For families the more usual cousin is a malicious download or scareware, unmeasured. |
| 6 | **Hardware failure** (drive dies; SSD controller; no warning) | The largest single cause in recovery labs: 66% in 2014 Kroll Ontrack cases (**V, S**; a paying-customer sample, so it over-weights unrecoverable physical failure); 52% of photographers' computer losses (**V, S**). Rates: Backblaze's data-center fleet is 1.36% a year in 2025 (344,196 drives; **V**, transparent data); Schroeder & Gibson (USENIX FAST 2007) found 2–4% a year common, up to 13% (**I**, old, data-center). 21% of external-drive owners reported a failure in the last year (**V**), far above the fleet rate, which suggests consumer drives (heat, drops, age) fare worse or the question is loose. | Everyone; creatives hold the most data on external drives; families keep the family photos on one old drive. |
| 7 | **Theft or loss of the computer / phone** | Old corporate figure: 5–10% of laptops lost or stolen in three years, 5% recovered (Ponemon, ~2010, **V, S**). Phones: 8% lost and 5% stolen in a year (a 2024 survey of unknown origin, **S**). Phone loss is as common as hard-drive loss in CleverFiles. | Students (laptops in bags, cafés, dorms), travellers, anyone with a phone. |
| 8 | **Migration to a new computer** (files left behind, partial transfer, old one wiped or sold too early) | No consumer rate. Migration errors are about a third of SaaS loss causes (Spanning 2016, **V, S**). The realistic exposure is the **hours after a loss or upgrade, when there is one copy**. | Older people (a relative sets up the new machine), small businesses, everyone at the 3–6 year upgrade. |
| 9 | **Cloud account lockout, suspension or service shutdown** | **No prevalence figure exists** (no survey, no vendor disclosure). Evidence is anecdotal: a 2011 Gmail incident hit under 0.08% of users, still hundreds of thousands (**S**); the 2023 pattern of thieves resetting an Apple ID with a stolen passcode, locking owners out of years of photos (**S**); consumer shutdowns are certain events (CrashPlan Home, 2018). 5% of photographers had completely lost cloud-stored data (**V, S**). | Photo-heavy families; older people who can't recover a forgotten password; anyone whose only copy is a single provider's account. |
| 10 | **Bad merge / sync conflict** (two devices edit; conflict copy overwritten; a git force-push) | **Nothing quantified.** I found no study on developers or on conflict-copy loss. | Developers; couples and families sharing a folder; small teams. |
| 11 | **Disk full** | **Nothing quantified.** Its effect is indirect: failed saves and half-written files, and it blocks *recovery*, since a restore needs room (§3.5). | Creatives (video, RAW photos), anyone on a small laptop SSD. |
| 12 | **Physical damage** (liquid, drops, fire, flood) | Almost 1 in 10 photographers dropped a phone or tablet in a bath or toilet (**V, S**); 31% of smartphone owners damaged a phone in a year (**S**). No reliable consumer rate. | Phones above all; households (fire, flood take every local copy at once). |

### 1.3 By population: what the data supports

- **Families:** photos are the loss (89% of losses in CleverFiles), kept on
  one phone or one old drive. The risk is *single copies and no one in
  charge*, not any one cause.
- **Older people:** the evidence conflicts. Backblaze says older men are more
  than twice as likely as 18–44-year-olds to back up daily (**V**); its own
  2022 survey has 6% daily at 55–64 versus 9% at 18–34; a survey from a
  seniors' tech-help company found 52% make no digital copy of important
  documents (**V, S**). The honest reading is *no reliable difference in
  loss, but a different way of recovering*: help comes from a relative, and
  a lockout or lost password is the hardest case.
- **Students:** only a 2002-era study of poor data-management habits
  (abstract only). Reasonable inference, not evidence: laptop theft and loss,
  a single deadline document, a whole degree on one machine.
- **Creatives / photographers:** the best-studied group (RPS/Verbatim,
  about 1,800 Europeans): hardware failure 52%, software corruption 13%,
  accidental damage 7%, virus 3%, theft 1%; over a quarter had lost data;
  a third never back up phones or tablets. **V, S.** They have the biggest
  folders, which makes disk space the binding constraint on restores.
- **Small businesses:** deletion and hardware tie for first (54% each in the
  Carbonite survey), then viruses (33%). Ransomware is where they differ
  from households (Verizon, Sophos; both enterprise-skewed).
- **Developers:** no data. Plausibly lost to force-pushes, bad merges,
  `rm -rf` in the wrong directory, uncommitted work, and local databases,
  but this is an inference from mechanism.

### 1.4 What the evidence says, in short

1. Two causes dominate what people recall and what labs see: **a mistake
   (delete/overwrite)** and **hardware dying**. Both are about one machine.
2. **Losing the whole computer** (failure, theft, damage) is the moment of
   greatest stakes: every file at once, often with the backup the person
   believed in having never been set up or tested.
3. **Sync is a different risk, not a safe one**: it moves a mistake to every
   copy within seconds. 44% report loss that way.
4. Ransomware is real for businesses and unmeasured for families.
5. Account lockout is invisible in the statistics and severe in the stories.

## Part 2. How other products handle it

Sources are the vendors' own documentation unless marked. Product behavior
was read, not run. `?` marks a claim I could not confirm at the source.

### 2.1 Summary: versions, deleted files, rollback, ransomware

| Product | Version history | Deleted files | Folder / account rollback to a time | Ransomware |
|---|---|---|---|---|
| **Time Machine** | Hourly 24 h, daily a month, weekly until the disk is full; local snapshots hourly for 24 h | Visible in older snapshots until pruned | Yes: browse a folder back in time | None; if malware can write to the volume it can damage backups |
| **iCloud Drive** | No official retention page; macOS per-file version history is local to each Mac (third party, `?`) | Recently Deleted, 30 days; deletes propagate to every device | No rollback. icloud.com **Data Recovery → Restore Files**, bulk, about 30 days (a summary said longer, `?`) | None found; Apple's page points to Time Machine |
| **Dropbox** | 30 d (Basic/Plus/Family), 180 d (Professional, Standard, Business), 365 d (Advanced, Business Plus, Enterprise) | Same window by plan | **Rewind**: folder or whole account to a date, undoable by rewinding again; web only; paid plans | Positioned for virus recovery; no auto-detect found |
| **Google Drive** | 30 days or 100 versions; "Keep forever" pins one | Trash 30 d; deletions sync to desktop; admins can recover 25 d after emptying | No general rollback | Drive for desktop detects mass encryption, **pauses sync**, restores files changed in the last 25 days (GA March 2026; personal-account eligibility from a secondary source, `?`) |
| **OneDrive** | 30 days | Recycle Bin 30 d (personal), 93 d (work) | **Restore OneDrive**: whole drive to a point in the last 30 days (Microsoft 365) | Detects, notifies, three-step guided recovery; first recovery free for non-subscribers |
| **Backblaze** | 30 days (default), 1 year, or forever (forever billed beyond a year, `?` price) | Same as version window; an unplugged external drive's files count as deleted after 30 days | None found; pick files and versions | Versions only |
| **Carbonite Safe** | About 3 months (reviews, `?`) | About 30 days (reviews, `?`) | Not found | Not found |
| **IDrive** | Up to 30 versions per file (10 on accounts older than Nov 2018) | Trash 30 days | **Snapshots**: pick a date and time, select, restore | Snapshots pitched for pre-ransomware state; no immutability claim |
| **Arq 7** | Whatever you configure; backup records are dated | Whatever you configure | Browse any backup record (a point in time) | **Object Lock** on S3/B2/Wasabi etc.; undeletable until expiry |
| **CrashPlan** (small business only) | Unlimited version lifetime (Professional) | 90 d (Professional), 30 d (Essential) | Not confirmed | Immutable backup (Professional) |
| **Syncthing** | Off by default; Trash Can, Simple, Staggered (30 s / hourly / daily / weekly, default max 365 d), External. **Only changes received from a peer are archived, not local edits or deletes** | `.stversions` on the receiving device | **None** | None; recovery depends on a peer having versioning on |
| **Resilio Sync** | `.sync/Archive`, 30 d on desktops, 1 d on mobile; the originating device does not archive its own change | In the Archive on the peers | **None** | None |

Notable: the two sync-first products with a vendor (Dropbox, OneDrive)
rebuilt a *time machine on top of sync* because propagation of mistakes
is the problem users hit. The peer-to-peer ones (Syncthing, Resilio) did not,
and archive only what a *peer* changed.

### 2.2 Where restored files go (the question that matters)

| Product | Single file / version | Whole folder or account | Output form |
|---|---|---|---|
| **Time Machine** | Original location (Apple: "Restored items return to their original location"); the Replace / Keep Both prompt was not on the pages read (`?`) | Same, by browsing the folder | In place |
| **iCloud Drive** | Recently Deleted → back to the original folder (`?`, from a snippet) | Data Recovery → original folders | In place, **inside the synced folder, so it re-syncs everywhere** |
| **Dropbox** | **Replaces the current file**, same name, then syncs everywhere | Rewind: in place, whole folder/account | In place |
| **Google Drive** | Replaces the current file (manage versions) | Ransomware restore: in place | In place |
| **OneDrive** | The selected version **becomes the current version** | Restore OneDrive: in place; files created after the point go to the Recycle Bin | In place |
| **Backblaze** | No restore-in-place | Restore picker → **ZIP download** (free, ≤500 GB), **copy to a B2 bucket**, or **a shipped USB drive** (≤8 TB) | Not in place, ever |
| **Carbonite** | Original or a different location (reviews, `?`) | same | Chosen |
| **IDrive** | Restore-location field: original (structure intact) or **a different folder**; Express ships a drive | Snapshot restore, same choice | Chosen |
| **Arq 7** | Drag out to the Desktop/Finder, or Restore → pick a destination; if the original exists, asks to overwrite, otherwise "Restored by Arq" (the latter from a summary, `?`) | Same | Chosen, with a parallel-folder fallback |
| **CrashPlan** | Original path or a new path; recreates the folder tree if the original is gone | same | Chosen |
| **Syncthing** | Manual copy out of `.stversions` | Manual | Wherever you copy it; a copy into the folder **syncs to peers** |
| **Resilio** | Manual copy from Open Archive; duplicates get an `(i)` suffix | Manual | Same |

**Pattern.**
- **Sync products restore in place and the restore itself syncs.** Their
  own answer to "what if I'm wrong?" is a second rollback (Dropbox Rewind
  can itself be rewound).
- **Backup products split.** The big consumer one (Backblaze) never
  restores in place: you get an archive or a drive, which is safe and slow.
  The configurable ones (IDrive, Arq, CrashPlan) let you pick, and Arq
  falls back to a side folder on conflict.
- **Casper's choice, a dated copy next to the original, is rare.** The only
  close match is Arq's "Restored by Arq" fallback and Syncthing/Resilio's
  suffixed duplicate. It buys no-confirmation safety the in-place products
  have to buy with a rollback of the rollback.

### 2.3 What each does on a brand-new computer

| Product | New-computer behavior |
|---|---|
| **Time Machine** | **Migration Assistant** on setup: pick the backup (and an older one by expanding the disclosure), then choose accounts, apps, files. The strongest "everything back, once" flow among all of these. Needs macOS 11+ for backups made on 12+. |
| **iCloud Drive** | Sign in, turn on Desktop & Documents ("Sync this Mac"). Files appear as cloud icons; "Optimize Mac Storage" keeps old ones on demand. Must first disable another provider's Desktop/Documents sync. What happens to the new Mac's existing local Desktop contents was not verified. |
| **Dropbox** | Install and sign in; choose online-only or selective sync (forum summary, `?`). Backed-up Desktop/Documents are **not** swapped into the new computer's system folders (community answer, `?`). |
| **Google Drive** | Sign in to Drive for desktop; **Stream** (default, on demand) or **Mirror** (full download). Desktop/Documents backup is a separate choice. |
| **OneDrive** | Folder Backup "roams" Desktop/Documents/Pictures to other PCs; Files On-Demand. On a Mac needs the standalone app and Full Disk Access. Redirected folders from another organization arrive empty. |
| **Backblaze** | **Restore first (ZIP or drive), then "Inherit Backup State"** so the new computer carries on without re-uploading. Inherit "does not transfer any data". Backblaze says the product isn't meant for quick device-to-device moves. |
| **Carbonite** | Nothing found. |
| **IDrive** | **Adopt / link** the old computer's backup to the new one (skips the initial upload); restore its files to the new machine separately. |
| **Arq 7** | Add the storage location on the new Mac (you need the encryption password), then restore from it to a folder you choose. |
| **CrashPlan** | **Replace device** links the old archive and carries over file selection and settings; known issue: files missing from the restore browser afterwards. Universities advise restoring to the new machine first, then moving files. |
| **Syncthing** | No account: pair the device, accept the folder, and everything downloads in full. No placeholders. |
| **Resilio** | Re-link by key or link; full download (Connect has selective sync). |

**Pattern.** "New computer" is a *separate flow* from "restore a file"
everywhere that does it well (Migration Assistant, Inherit, Adopt, Replace
device): get the data back first, reconnect the history second. The sync
products blur it, since signing in *is* the restore, which is lovely when
it works and silent when the account is the problem.

### 2.4 Account and service risk

Cloud-only products fail with the account: Apple can lock one on suspicion
and iCloud-only files may be unreachable meanwhile; Google files in a
suspended account become inaccessible and are eventually deleted (secondary
source); Microsoft closes OneDrive after a grace period of 30–60 days
(secondary). Carbonite purges 30–60 days after a lapsed renewal. Backblaze
treats a detached drive as deleted after 30 days. CrashPlan Home was shut
down in 2018. Time Machine, Arq (with your own storage), Syncthing and
Resilio have no account to lose, only a disk or a key. I found no official
page giving a rate for any of these.

## Part 3. What this implies for Casper

### 3.1 Restore locations

1. **Keep the dated copy next to the original as the default.** It is not
   a product convention (the sync products overwrite in place and then
   rely on a second rollback; Backblaze hands over a ZIP) and that is a
   strength: nothing is overwritten, nothing needs confirmation, and a
   restore cannot itself become a propagated mistake. It also matches the
   one behavior the sync-first products added later (Arq's side folder,
   Syncthing's duplicate).
2. **A mirrored folder restore must land outside the mirror's reach.** The
   cloud products' trap is that a restore re-enters the synced folder and
   propagates (iCloud, Dropbox, Syncthing, Resilio all do). Casper already
   puts folder restores in a sibling folder outside the mirrored one; keep
   that, and say it. The single-file dated copy *is* inside the mirrored
   folder and will be mirrored, which is fine (it's a new file) but should
   be a known cost against the 256 MB margin.
3. **Replace stays the exception**, offered when the copy won't fit. This
   is how everyone else defaults, so people will *ask* for it ("put it
   back"). Answer with the copy and say where it is; offer replace only
   on the disk-full condition, as designed.
4. **Add a chosen-location restore for the new-computer and lost-Mac case.**
   Every serious backup product lets you pick a destination (IDrive, Arq,
   CrashPlan) and the good new-computer flows separate "bring the data
   back" from "reconnect the history". When the original path doesn't
   exist, or the machine is new, the copy should go to where the person
   says, defaulting to the original path recreated (as CrashPlan does) or
   a visible `Casper Restores` folder. Never silently the old path on a
   fresh account with a different username.
5. **Disk space is a first-class restore condition.** No product I read
   treats it as a design input; Casper already does. For creatives and a
   new Mac with a small disk it is the common failure (a photographer's
   library can't be copied beside itself). Keep the free-space check, and
   prefer restoring to an external or chosen location over "delete
   something first".
6. **Offer the "as of" folder restore as the headline recovery for
   mistakes and for ransomware.** Only Dropbox, OneDrive, IDrive and Time
   Machine have it among consumer products; Backblaze, Google and iCloud
   don't, and Syncthing/Resilio can't. Casper's `restore_as_of`
   (non-destructive, by time, multiple folders) is better than Rewind
   (in place) in the case where the person is *unsure* of the time.
7. **30 days is the market floor and the common default, not a weakness.**
   It equals Backblaze's default, IDrive's trash, OneDrive, Google and
   Dropbox Basic. Be explicit that it is 30 days, and plain at the edge
   ("older than the history"), as designed. Dropbox's lesson: upgrading
   does not retroactively extend, so any later extension applies from that
   day on.

### 3.2 Which scenarios matter most for the pilot

Ranked by (how common) × (how bad) × (whether Casper changes the outcome),
for the pilot's peer-backup users (friends, family, small groups).

1. **Losing the Mac (failure, theft, damage, liquid).** The most common
   *total* loss, the worst for families and students (single copy, photos),
   and the thing a friend's mirror is for. This is the lost-Mac recovery
   path ([recovery-v2-design.md](../recovery-v2-design.md)); the research
   says to treat the **first hours on the new machine** as the product:
   restore-then-reconnect, chosen destination, enough disk, and a kit that
   works with the old Mac gone. Highest priority.
2. **Accidental deletion and bad overwrite.** The loss people recall most.
   Casper changes the outcome fully inside the 30 days; the design (one
   question at most, deleted-only list, dated copy) is aimed here. High.
3. **Sync-propagated mistakes.** Casper *is* a mirror, so a deletion
   reaches the peer within seconds by design; the 30-day history on the
   peer is the whole defense. Worth testing explicitly: delete a folder,
   confirm the peer keeps versions of *local* deletes (unlike Syncthing's
   plain versioning, which only archives changes received from a peer);
   restore as of before. Note too that Casper's own Syncthing restores
   must not re-propagate a restore (§3.1.2).
4. **Ransomware / corruption.** Small-business-heavy, rare at home, but
   when it happens it looks like scenario 3 at scale: every file changed,
   mirrored in minutes. The defense is the same history plus `restore_as_of`
   before the infection. Two unknowns to settle before claiming this:
   does a mass-encryption event get pruned out of the "one per day after 7
   days" thinning, and could ransomware on the Mac damage the peer's copy
   (the peers see only ciphertext; check whether a compromised Mac can
   delete history). Medium for the pilot; it should not be claimed until
   tested.
5. **Migration to a new computer** (planned). The same code path as 1 with
   the old Mac still alive: a good test of "chosen location" and
   "reconnect history". Medium.
6. **Account lockout / shutdown.** Casper isn't a cloud holding the
   files (ciphertext lives on friends' Macs), so a provider lockout is less
   likely to cost the files, but Casper's *own* account and the recovery
   kit are the equivalent single point; also the sustained-shutdown risk
   of Casper itself. The recovery kit and master key
   ([recovery.md](../recovery.md)) are the answer; verify the kit works
   without Casper's service if that is claimed. Medium.
7. **Disk full.** Not a loss cause here but a restore blocker; covered by
   the fit check. Low-medium.
8. **App corruption, bad merge/conflict, physical damage.** Corruption is
   mostly scenario 2. Conflicts and developers are unmeasured; defer.
   Fire/flood is a reason to value a mirror in *another house*, which is
   the premise. Low.

### 3.3 Open questions this raises

- Does the mirror history keep versions of **locally made** deletes and
  edits (the Syncthing default does not)? I did not check what
  Casper does (the mirror code was not read for this); worth an explicit test
  in the e2e.
- What does a person with a new, differently-named Mac expect "put it
  back" to mean? Default to a chosen location with the original path as
  the suggestion.
- Whether to offer a **time-scrubbed account-wide rollback** later (Rewind
  without being in place) for the ransomware case. Not for the pilot.
- Known research gaps: no data on developers, students or conflicts; no
  consumer ransomware rate; no lockout rate. Pilot logs will beat any of
  these, so count what the pilot's people actually ask to get back.

## Sources

Loss statistics
- Backblaze/Harris 2022: <https://backblaze.com/blog/the-2022-backup-survey-54-report-data-loss-with-only-10-backing-up-daily/>
- Backblaze 2016: <https://www.backblaze.com/blog/data-backup-survey/>
- CleverFiles 2020: <https://www.cleverfiles.com/howto/cleverfiles-data-recovery-survey-2020.html>
- Acronis: <https://www.acronis.com/en-us/blog/posts/exploring-recent-trends-acronis-world-backup-day-survey>
- Kroll Ontrack (press): <https://betanews.com/2014/06/11/hard-drive-failure-responsible-for-two-thirds-of-data-loss>, <https://www.itjungle.com/tfh/tfh072610-story10.html>
- Spanning SaaS survey: <https://betanews.com/2016/03/24/user-error-saas-data-loss/>
- Carbonite small business: <https://www.channelinsider.com/news-and-trends/half-of-small-businesses-impacted-by-data-loss-survey/>
- Photographers (RPS/Verbatim): <https://amateurphotographer.com/latest/photo-news/photographers-fail-heed-warnings-digital-armageddon/>
- Backblaze Drive Stats 2025: <https://www.backblaze.com/blog/backblaze-drive-stats-for-2025/>; SSD vs HDD: <https://blocksandfiles.com/2023/03/10/backblaze-ssd-vs-hdd/>
- Schroeder & Gibson, FAST 2007: <https://www.usenix.org/conference/fast-07/disk-failures-real-world-what-does-mttf-1000000-hours-mean-you>
- Verizon DBIR 2025 (coverage): <https://infosecurity-magazine.com/news/verizon-dbir-smb-ransomware-attacks>
- Sophos State of Ransomware in Enterprise 2025: <https://www.sophos.com/blog/the-state-of-ransomware-in-enterprise-2025>
- Coveware Q3 2025 (coverage): <https://securityaffairs.com/183941/cyber-crime/ransomware-payments-hit-record-low-only-23-pay-in-Q3-2025.html>
- Ponemon laptops: <https://www.darkreading.com/risk/lost-laptops-cost-companies-billions-study-says>
- Phone loss/damage: <https://www.androidheadlines.com/2024/03/78-million-americans-damaged-their-smartphones-in-the-last-year.html>
- Gmail lockout 2011: <https://www.silicon.co.uk/workspace/gmail-users-accounts-wiped-out-22258>; Apple ID theft pattern: <https://9to5mac.com/2023/01/17/apple-account-recovery/>
- Marshall: <https://www.microsoft.com/en-us/research/wp-content/uploads/2007/05/Archiving2007-submitted.pdf>
- Seniors: <https://www.backblaze.com/blog/seniors-are-the-kings-of-data-backup/>, <https://www.homecaremag.com/news/bask-technology-inc-shares-survey-seniors-tech-habits-honor-world-backup-day>

Product documentation
- Time Machine: <https://support.apple.com/guide/mac-help/back-up-files-with-time-machine-mh35860/mac>, <https://support.apple.com/102154>, <https://support.apple.com/guide/mac-help/restore-files-backed-up-with-time-machine-mh11422/mac>, <https://support.apple.com/en-in/102613>
- iCloud: <https://support.apple.com/en-us/111763>, <https://support.apple.com/en-us/109344>, <https://eclecticlight.co/2025/05/05/how-document-versions-are-handled-in-icloud-drive/>
- Dropbox: <https://help.dropbox.com/delete-restore/version-history-overview>, <https://help.dropbox.com/delete-restore/recover-deleted-files-folders>, <https://help.dropbox.com/delete-restore/rewind>, <https://help.dropbox.com/delete-restore/recover-older-versions>
- Google Drive: <https://support.google.com/drive/answer/2409045>, <https://support.google.com/drive/answer/2375102>, <https://support.google.com/drive/answer/7329379>, <https://knowledge.workspace.google.com/admin/drive/detect-ransomware-and-recover-files-in-drive-for-desktop>, <https://workspaceupdates.googleblog.com/2026/03/ransomware-detection-and-file-restoration-for-Google-Drive-now-generally-available.html>
- OneDrive: <https://support.microsoft.com/en-us/office/restore-a-previous-version-of-a-file-stored-in-onedrive-159cad6d-d76e-4981-88ef-de6e96c93893>, <https://support.microsoft.com/en-us/office/restore-deleted-files-or-folders-in-onedrive-949ada80-0026-4db3-a953-c99083e6a84f>, <https://support.microsoft.com/en-us/office/restore-your-onedrive-fa231298-759d-41cf-bcd0-25ac53eb8a15>, <https://support.microsoft.com/onedrive/how-to-detect-ransomware-and-recover-files-using-onedrive>
- Backblaze: <https://www.backblaze.com/computer-backup/docs/version-history>, <https://www.backblaze.com/computer-backup/docs/external-hard-drives>, <https://www.backblaze.com/computer-backup/docs/create-a-restore>, <https://www.backblaze.com/computer-backup/docs/use-the-inherit-backup-state-option>, <https://www.backblaze.com/computer-backup/docs/migrate-data>
- Carbonite: <https://support.carbonite.com/articles/Personal-Pro-Windows-Mac-Storage-of-Data-After-Expiration>; reviews <https://www.expertreviews.co.uk/technology/software-services/carbonite-home-review>
- IDrive: <https://www.idrive.com/new-design/snapshots>, <https://www.idrive.com/help/Windows/restore.htm>, <https://www.idrive.com/idrive-express-restore>
- Arq: <https://www.arqbackup.com/documentation/arq7/English.lproj/restore.html>, <https://www.arqbackup.com/documentation/arq7/English.lproj/objectLock.html>
- CrashPlan: <https://crashplan.com/en-us/pricing/>, <https://support.crashplan.com/hc/en-us/articles/9199442505101>; consumer exit <https://tidbits.com/2017/08/22/crashplan-discontinues-consumer-backups/>
- Syncthing: <https://docs.syncthing.net/users/versioning.html>, <https://docs.syncthing.net/users/foldertypes.html>, <https://docs.syncthing.net/users/untrusted.html>
- Resilio: <https://help.resilio.com/hc/en-us/articles/204754239>, <https://docs.resilio.com/content/reference-information/understanding_the_archive_folder/>
