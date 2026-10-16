# Restore: getting files back

Designed 2026-10-16 for the pilot. Builds on the version machinery
(`list_versions`, `restore_version`, Syncthing's 30-day history on the
mirrors) and the principles in [agent-first-ux.md](agent-first-ux.md) (10
and 11, added today) and [gates.md](gates.md). This is about getting files
back on the Mac that owns the folder. A whole lost Mac is
[recovery-v2-design.md](recovery-v2-design.md).

## 1. Decisions (the Principal, 2026-10-16)

1. **Minimal app GUI.** Restore is driven by the person's own agent or by
   Casper's guide, with feature parity between them. (The guide may later
   be a paid service for people who keep it instead of moving to their own
   agent, so nothing may exist only in one of the two.)
2. **At least as efficient as the best classic GUI.** No twenty questions.
   Where a widget is the most efficient way to choose (a file/version
   picker), the agent presents one as good as an app would: the guide
   renders it inline on its page; a person's own agent asks the Casper app
   to show it on demand (as `mirror_choose_folder` does), never as standing
   app UI.
3. **Copy by default**, next to the original, with a dated name. If the
   restore doesn't fit on the disk, offer to replace instead. Never ask the
   person to delete their local copy first.
4. **In scope:** single files (earlier versions and deleted files, within
   the 30-day history), whole folders, and sets of folders, as of a point in
   time.
5. **Gate model.** File, version, folders and time are info items. The one
   intent is the person's choice.

## 2. The best GUI equivalent, and what we keep from it

The best classic flows are Time Machine ("enter Time Machine", pick a
folder, scrub back to a time, select, Restore), Dropbox/iCloud version
history (right-click a file, see its versions with dates, Restore) and
Photos' Recently Deleted (one list, select, Recover). What makes them
efficient:

| GUI efficiency | How we keep it |
|---|---|
| Starts from the thing, not from a form: you're already looking at the file or folder | The person's first sentence usually names it ("my budget spreadsheet from before Tuesday's edit"). The agent searches (`name_contains`) before it asks anything. |
| One list, newest first, deleted files easy to find | `list_versions` is grouped per file, newest first, deleted files marked, and `deleted_only` shows just those. |
| The latest sensible version is preselected | One match, one version: restore it, no question. Several versions of one file: the newest one before the person's stated time; "the one before my last edit" is the second newest. |
| Dates are scrubbed, not typed | The agent turns "last Tuesday" into a time itself. A bare date means the end of that day. |
| Restore is one click, never destructive | Copy by default, so no confirmation is needed (§4). |
| Multi-select | A folder or several as of a time is one call; the picker takes several rows. |
| Free-text search over a long list | `name_contains` on both tools and a search box in the picker. |

So the common case is **one question at most**: usually none ("restore
budget.xlsx from yesterday morning" has file, version and time in it), and
when a name matches several files or versions, the agent shows the picker
instead of asking "which one?" turn after turn.

## 3. The flow

```mermaid
flowchart TD
    A[Person says what they want back] --> B{Items known?}
    B -- all -- C[Restore a copy]
    B -- name matches several, or none given --> P[Picker: inline in the guide, or the Casper app on demand]
    P -->|Person picks| C
    C --> F{Fits on the disk?}
    F -- yes --> D[Copy next to the original, dated name]
    F -- no --> R[Offer: replace instead]
    R -->|yes| X[Replace; the current one stays in history]
```

### Gate (restore)

| Item | Kind | Filled by | Default |
|---|---|---|---|
| Folder(s) | info | the person's words, or Casper's records (one mirrored folder, so that one) | their only folder |
| File(s) | info | words, search results, or the picker | none (or "everything" for a folder restore) |
| Version | info | words ("before Tuesday"), or the picker | the newest earlier version |
| As-of time (folder restores) | info | words | none (asked as a bare question: "Back to when?") |
| Mode | info | Casper: copy, unless it doesn't fit | copy |
| Restore | **intent** | the person: their request, or their pick in the picker | none |

A copy overwrites nothing and costs only disk space, so the person's own
request (or their pick in the picker) is the intent: there is no "Restore
it?" after they have said so. This is the gate model's "when the last item
is filled, act". The picker is a trusted surface the agent can't operate
(the guide's page buttons, the Mac's own panel), so a pick there is the
intent, filled by the person directly.

**Replace** is the one destructive form. Casper only offers it when the copy
doesn't fit, and it needs the person's yes after seeing the plain terms
("This will put the old version in place of the current file. The current
one stays in your history for 30 days."). Tools preview by default for
replace, as sharing tools do. Whether replace should instead require
Casper's own dialog is an open question for the Principal (board).

## 4. What gets restored, exactly

- **A file** (`restore_version`): that version of the file, as a new file
  next to the original: `Budget.xlsx` becomes `Budget (as of 2026-10-14
  09.12).xlsx` in the same folder (the time is when that version was
  written; a number is added if the name is taken). A *deleted* file works
  the same: its folder still exists, so the copy goes where it was. If its
  folder is gone too, Casper recreates the path.
- **A folder as of a time** (`restore_as_of`): a new folder next to the
  original, `Documents (as of 2026-10-10)`, holding every file as it was at
  that moment: files changed or deleted since come from the history; files
  untouched since are copied from the current folder; files created later
  are left out, and so are earlier restored copies (their dated names mark them). It sits outside the mirrored folder, so it isn't mirrored
  (it is a copy). If the mirrored folder is the whole home folder, it goes
  in `Casper Restores`.
- **Several folders**: the same, per folder, in one call. The fit check
  covers the total.
- **History limit.** Only the last 30 days exist (every version for 7 days,
  then one per day). Asking for an older time answers plainly that it's
  beyond the history and offers the oldest available.
- **As of** means the file's content at that moment: the version written
  at or before the time and replaced after it. Times are the Mac's own;
  a bare date is the end of that day.
- **Replace mode** puts the chosen versions over the files at their own
  paths, atomically (written beside, then renamed). For a folder it does not
  delete files created after the time. Overwritten files remain in the
  mirrors' history, so replace can itself be undone for 30 days.

### Does it fit?

Before copying, the Mac reports free space. A copy needs the restored bytes
plus room for the largest file (decrypted beside the final place, then
renamed); a replace needs only twice the largest file, since it frees the
old one. A copy keeps a 256 MB margin; a replace needs only 16 MB beyond that (for a single file the two differ by little; for folders, a lot). If the copy doesn't fit but replace
does, the answer is "The copy needs 4.2 GB and your Mac has 1.1 GB free.
Replace the current file instead?" (never "delete something first"). If
even replace doesn't fit, it says how much space is short.

## 5. Surfaces and parity

| | Person's own agent (MCP) | Casper's guide (web, Telegram) |
|---|---|---|
| Find | `list_versions(folder, name_contains, deleted_only)` | same tool |
| One file | `restore_version(folder, name, at, mode)` | same tool |
| Folders as of a time | `restore_as_of(folders, as_of, mode)` | same tool |
| Picker | `show_restore_picker(folder, name_contains, deleted_only)`: the Casper app shows a list on the Mac, the pick restores a copy, and the call returns what was restored | `show_restore_picker` renders an inline picker on the page (search, per-file versions, deleted filter, folder + time); the person's click restores. In Telegram, which has no widget, it falls back to numbered buttons. |

Both go through the same functions in `casper_service` (`_mcp_*`). The
guide's tools are the same list, so parity is by construction: a test checks
that every restore tool is in both.

The Mac picker today is a native list (AppleScript's `choose from list`,
multiple selection): one row per file version, newest first. A richer
picker (search, grouped by file, a time scrubber) is a follow-up; the guide's
inline picker is the full design.

## 6. Existing app GUI that conflicts with principle 1 (follow-up, not changed)

- **"Recover from a Lost Mac…" menu item and its dialogs** (recovery v2 P1:
  kit chooser, passphrase, Restore confirmation, progress). Principle 1
  wants this agent- or guide-driven. Two things must stay in the app: the
  Restore yes is a trusted-path intent, and typing the kit passphrase must
  not go through an agent. A possible shape: the guide walks the steps and
  the app shows only the passphrase and the Restore dialog, on demand.
- **"Make Recovery Kit" menu item and Save panel.** The same shape: the
  guide or agent triggers it, the app shows only the passphrase and Save
  dialogs, on demand.
- **Standing menu items generally** (status, notifications): anything that
  is just a button to do something an agent can ask for.

## 7. Phases

1. **Core** (daemon + service + tools): dated copy next to the original,
   deleted files, folder(s) as of a time, replace, disk fit; MCP and guide.
2. **Picker**: the guide's inline picker, the Mac's on-demand picker for own
   agents, and a restore step in the lost-mac e2e scenario.

## 8. Not decided here

- Replace behind Casper's own dialog instead of the honor-system preview.
- Restoring someone else's shared files (a friend's folder is theirs).
- A time scrubber in the Mac picker.
