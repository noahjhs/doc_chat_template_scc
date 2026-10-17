"""Claims: the one place Casper says anything about the promise it makes
("protected", "up to date", "online", "your folders are back", "Done").
Design and the inventory it answers: docs/product/claims.md.

A claim is a typed statement about a subject (a folder, a computer, an
invite...). It is issued from named EVIDENCE, each piece with an observation
time and a freshness limit, and rendered ONLY from that evidence:

  * three states: HOLDS / VIOLATED / UNKNOWN. No evidence, expired evidence
    and missing fields all give UNKNOWN, never a positive.
  * asymmetric: a positive claim needs fresh evidence; a cautious one
    ("not protected yet") never does. Weaker evidence never gives a stronger
    sentence (tests/test_claims.py checks this over the evidence combinations).
  * logic reads `Issued.state` / `Issued.data`, people read `Issued.text`.
    Nothing parses a rendered sentence, and nothing outside this module
    writes these sentences (tests/test_claims.py greps for it).
  * every issued claim is written to `claim_log` with its evidence.

Evidence levels (docs/product/claims.md 5.1): E0 asserted, E1 reported by the
component itself, E2 observed by another party, E3 challenged (a sampled
read-back that matched), E4 exercised (a verified restore). The pilot has
E1 only: a fresh status report with an age limit. See `readback_evidence`
for where E3 plugs in.
"""

import contextvars
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import mirroring
import trust

HOLDS, VIOLATED, UNKNOWN = "holds", "violated", "unknown"
E0, E1, E2, E3, E4 = range(5)

# Freshness limits (leases): past these a positive claim lapses to UNKNOWN.
REPORT_MAX_AGE = 120  # the owner's Mac reports every 15 s while Casper runs
PRESENCE_MAX_AGE = 180  # = main.LIVE_TTL_SECONDS: the app beats about once a minute
KIT_MAX_AGE = 180 * 86400  # a recovery kit is a one-time event; this is how long we vouch for the record
PEER_SEEN_MAX_AGE = REPORT_MAX_AGE

# Set when a sampled read-back from a mirror is required before "Protected"
# (E3, docs/product/claims.md 5.4 #4). Pilot: off. Flip it together with
# registering a source with `register_readback`.
REQUIRE_READBACK = False
READBACK_MAX_AGE = 48 * 3600

FOLDER_PROTECTED = "folder_protected"
PEER_CURRENT = "peer_current"
HOLDER_CURRENT = "holder_current"
COPY_PROGRESS = "copy_progress"
ITEMS_READABLE = "items_readable"
MAC_ONLINE = "mac_online"
KIT_SAVED = "kit_saved"
INVITE_ACCEPTED = "invite_accepted"
ACTION_DONE = "action_done"
RECOVERY = "recovery"
HISTORY_SCOPE = "history_scope"
FOLDER_RESTORE = "folder_restore"
FOLDERS_SAFE = "folders_safe"


# --- evidence ---------------------------------------------------------------
@dataclass(frozen=True)
class Evidence:
    kind: str
    subject: str
    observer: str  # who saw it: "owner's Mac", "casper_service", "the person"...
    method: str  # how: "status report", "presence beat", "database row"...
    level: int
    observed_at: float | None  # unix seconds; None = never observed
    value: dict = field(default_factory=dict, compare=False, hash=False)
    max_age: float | None = None  # None = never lapses

    def age(self, now: float) -> float | None:
        return None if self.observed_at is None else max(0.0, now - self.observed_at)

    def fresh(self, now: float) -> bool:
        a = self.age(now)
        return a is not None and (self.max_age is None or a <= self.max_age)

    def ref(self, now: float) -> dict:
        return {"kind": self.kind, "subject": self.subject, "observer": self.observer, "method": self.method, "level": self.level,
                "observed_at": self.observed_at, "fresh": self.fresh(now)}


@dataclass
class Issued:
    claim: str
    subject: str
    state: str
    text: str
    as_of: float | None = None
    evidence: list = field(default_factory=list)  # Evidence refs (dicts)
    data: dict = field(default_factory=dict)  # typed values for logic -- read these, never `text`
    surface: str = ""

    @property
    def holds(self) -> bool:
        return self.state == HOLDS


def to_unix(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)  # SQLite CURRENT_TIMESTAMP is UTC, written without a zone
    return dt.timestamp()


def age_words(seconds: float | None) -> str:
    if seconds is None:
        return "a long time"
    s = int(seconds)
    if s < 90:
        return "under a minute" if s < 60 else "1 min"
    if s < 3600:
        return f"{s // 60} min"
    if s < 86400:
        return f"{s // 3600} h"
    return f"{s // 86400} d"


def report_evidence(report: dict | None, subject: str, observer: str = "the owner's computer") -> Evidence:
    """A folder status report (what the owner's daemon says about its own Syncthing)."""
    return Evidence("status_report", subject, observer, "status report", E1,
                    to_unix(report.get("reported_at")) if report else None, report or {}, REPORT_MAX_AGE)


# Sources of stronger evidence. A source is fn(db, folder_row, now) -> Evidence | None.
# Nothing is registered in the pilot (REQUIRE_READBACK is off).
_readback_sources: list = []


def register_readback(fn) -> None:
    """Hook for the sampled read-back spot check: the owner's daemon asks a mirror for a few
    random encrypted blocks and compares them with what it computes locally; the result is
    an Evidence(kind="sampled_read", level=E3, max_age=READBACK_MAX_AGE). With
    REQUIRE_READBACK on, FOLDER_PROTECTED holds only with a fresh one for >= 1 mirror."""
    _readback_sources.append(fn)


def readback_evidence(db, folder, now: float) -> list[Evidence]:
    out = []
    for fn in _readback_sources:
        ev = fn(db, folder, now)
        if ev is not None:
            out.append(ev)
    return out


# --- the log and the turn ledger -------------------------------------------
_ledger: contextvars.ContextVar = contextvars.ContextVar("claims_ledger", default=None)
_LOG_QUIET_SECONDS = 600
_digits = re.compile(r"\d+")


def begin_turn() -> list:
    """Starts collecting the claims issued while one reply is being made (the guide's turn)."""
    box: list = []
    _ledger.set(box)
    return box


def issued_this_turn() -> list[Issued]:
    return list(_ledger.get() or [])


def _log(db, c: Issued, now: float) -> None:
    box = _ledger.get()
    if box is not None:
        box.append(c)
    if db is None:
        return
    try:
        fingerprint = _digits.sub("#", c.text)
        last = db.execute("SELECT state, text, created_at FROM claim_log WHERE claim = ? AND subject = ? AND surface = ? "
                          "ORDER BY id DESC LIMIT 1", (c.claim, c.subject, c.surface)).fetchone()
        if last and last["state"] == c.state and _digits.sub("#", last["text"]) == fingerprint:
            seen = to_unix(last["created_at"])
            if seen is not None and now - seen < _LOG_QUIET_SECONDS:
                return
        db.execute("INSERT INTO claim_log (claim, subject, state, as_of, evidence, text, surface, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (c.claim, c.subject, c.state, c.as_of, json.dumps(c.evidence), c.text, c.surface, trust.now_iso()))
    except Exception:  # the log must never stop a claim from being made
        pass


def _issue(db, now: float, claim: str, subject: str, state: str, text: str, evidence: list, data: dict | None = None,
           surface: str = "", as_of: float | None = None) -> Issued:
    c = Issued(claim, subject, state, text, as_of, [e.ref(now) if isinstance(e, Evidence) else e for e in evidence], data or {}, surface)
    _log(db, c, now)
    return c


# --- mirror status: protected, up to date, copying, unreadable ----------------
def _mirrors(st: dict) -> list[dict]:
    return [p for p in st.get("peers", []) if p.get("role") == "mirror"]


def peer_seen(p: dict) -> bool:
    """A peer has been connected at least once (Syncthing says 100% for one it never reached)."""
    return bool(p.get("connected")) or not str(p.get("last_seen", "")).startswith("1969")


def copy_done(st: dict) -> bool:
    """The first copy is on a mirror, from a report: files exist, nothing unprotected or unreadable, and a
    connected mirror that has been reached says it needs nothing. Ignores age: callers ask `fresh` too."""
    return bool(st) and bool(st.get("local_files")) and not st.get("unprotected_files") and not st.get("unreadable_items") and any(
        p.get("connected") and peer_seen(p) and p.get("completion", 0) >= 100 and not p.get("need_files") for p in _mirrors(st))


def _assess(st: dict) -> tuple[str, str]:
    """(state, kind) from the report's contents alone, as if it were fresh. Freshness then only demotes a HOLDS."""
    peers = _mirrors(st)
    ever = any(peer_seen(p) for p in peers)
    unreadable, unprotected = st.get("unreadable_items") or 0, st.get("unprotected_files") or 0
    if not st.get("local_files"):
        return UNKNOWN, "empty"  # nothing seen in it yet: nothing to protect, and 100% of nothing proves nothing
    if not ever:
        return VIOLATED, "never_connected"
    if unreadable:
        return VIOLATED, "unreadable"
    if unprotected:
        return VIOLATED, "unprotected"
    if copy_done(st):
        return HOLDS, "protected"
    # nothing flagged and a mirror reached once, but none shows a finished copy right now
    complete_asleep = any(peer_seen(p) and p.get("completion", 0) >= 100 and not p.get("need_files") for p in peers)
    if st.get("at_risk_files") or complete_asleep:
        return HOLDS, "catcher" if st.get("at_risk_files") else "asleep"
    return UNKNOWN, "copying"


def _peer_line(p: dict, who: str, fresh: bool, age: float | None) -> tuple[str, str]:
    if not fresh:
        return UNKNOWN, f"  - {p.get('role')}: {who}: not confirmed (no report for {age_words(age)})"
    if p.get("connected") and p.get("completion", 0) >= 100 and not p.get("need_files") and peer_seen(p):
        return HOLDS, f"  - {p.get('role')}: {who}: up to date"
    if p.get("connected"):
        return UNKNOWN, f"  - {p.get('role')}: {who}: connected, catching up"
    return VIOLATED, f"  - {p.get('role')}: {who}: offline (last seen {(p.get('last_seen') or '?')[:16]})"


def folder_report(db, f, names: dict, owner_host: str = "", restore_host: str = "", surface: str = "",
                  unprotected_names=None, now: float | None = None, st: dict | None = None) -> Issued:
    """The protection claim for one folder, with its per-mirror lines. `names` maps a Syncthing device id to
    who it is. `unprotected_names()` (optional) lists the files, asked live from the owner's Mac.
    `st` overrides the stored report (tests); otherwise the latest one is read."""
    now = time.time() if now is None else now
    sub = f["id"]
    label = f["label"]
    if st is None:
        st = mirroring.latest_status(db, sub) or {}
    ev = report_evidence(st or None, sub)
    refs = [ev, *readback_evidence(db, f, now)]
    fresh, age = ev.fresh(now), ev.age(now)
    if f["status"] == "awaiting_consent":
        return _issue(db, now, FOLDER_PROTECTED, sub, VIOLATED,
                      f"{label}: NOT protected yet, waiting for the person to click Mirror in the Casper dialog (or Telegram).",
                      [], {"kind": "awaiting_consent"}, surface)
    if f["restore_host_id"] and st.get("restoring"):
        return folder_restore(db, f, st, restore_host, surface, now)
    if not st:
        return _issue(db, now, FOLDER_PROTECTED, sub, UNKNOWN, f"{label}: starting up (no report from {owner_host or 'the computer'} yet).",
                      refs, {"kind": "no_report", "fresh": False}, surface)
    state, kind = _assess(st)
    unreadable, unprotected = st.get("unreadable_items") or 0, st.get("unprotected_files") or 0
    data = {"kind": kind, "fresh": fresh, "age": age, "unreadable": unreadable, "unprotected": unprotected,
            "copy_done": copy_done(st) and fresh, "local_files": st.get("local_files", 0)}
    if state == HOLDS and (not fresh or (REQUIRE_READBACK and not any(
            e.kind == "sampled_read" and e.level >= E3 and e.fresh(now) for e in refs[1:]))):
        state, kind = UNKNOWN, ("stale" if not fresh else "unverified")
        data["kind"] = kind
    lines: list[str] = []
    if kind == "protected":
        lines.append(f"{label}: protected. Every change is on at least one mirror.")
    elif kind == "asleep":
        lines.append(f"{label}: protected. Every change is on a mirror that is asleep right now.")
    elif kind == "catcher":
        lines.append(f"{label}: " + (f"{st['at_risk_files']} readable change(s)" if unreadable else f"protected. {st['at_risk_files']} recent change(s)")
                     + " are held by the catcher until a mirror wakes up.")
    elif kind == "never_connected":
        lines.append(f"{label}: NOT protected yet: no mirror has connected yet, so nothing has been copied.")
    elif kind == "empty":
        lines.append(f"{label}: nothing in it has been seen yet, so there is nothing protected to report.")
    elif kind == "copying":
        lines.append(f"{label}: NOT protected yet: still copying to the mirror. It counts once a mirror has everything.")
    elif kind == "stale":
        lines.append(f"{label}: not confirmed. {owner_host or 'The computer'} hasn't reported for {age_words(age)}, "
                     "so Casper can't say whether recent changes are on a mirror (its last report was fine).")
    elif kind == "unverified":
        lines.append(f"{label}: not confirmed. No mirror has read back a sample of it lately.")
    elif unprotected:  # kind unprotected, or unreadable besides
        lines.append(f"{label}: {unprotected} recent change(s) not on any mirror or catcher yet (oldest "
                     f"{int((now - st.get('unprotected_oldest_unix', now)) / 60)} min).")
        files = (unprotected_names() if unprotected_names else None) or []
        if files:
            more = f" (and {unprotected - min(len(files), 10)} more)" if unprotected > min(len(files), 10) else ""
            lines.append("  not yet protected: " + ", ".join(files[:10]) + more)
    elif st.get("at_risk_files") and unreadable:
        lines.append(f"{label}: {st['at_risk_files']} readable change(s) are held by the catcher until a mirror wakes up.")
    if unreadable:  # never "protected" while something in it can't be read
        n = unreadable
        lines.insert(0, f"{label}: {n} item{'s' if n != 1 else ''} can't be read, so {'they aren' if n != 1 else 'it isn'}'t copied. "
                        "NOT fully protected." + (" Everything else is on a mirror." if not unprotected and not st.get("at_risk_files") else ""))
    # per-mirror lines: each is its own claim (peer_current)
    for p in st.get("peers", []):
        dev = str(p.get("device_id") or "?")
        who = names.get(dev, dev[:7])
        pstate, line = _peer_line(p, who, fresh, age)
        lines.append(line)
        _issue(db, now, PEER_CURRENT, f"{sub}:{dev[:7]}", pstate, line.strip(), [ev], {"fresh": fresh}, surface)
    stamp = (st.get("reported_at") or "")[:19]
    lines.append(f"  ({st.get('local_files', 0)} files, {_human(st.get('local_bytes', 0))}; as of {stamp} UTC, {age_words(age)} ago)")
    return _issue(db, now, FOLDER_PROTECTED, sub, state, "\n".join(lines), refs, data, surface, ev.observed_at)


def _human(n: int) -> str:
    import backups

    return backups.human(n)


def folder_restore(db, f, st: dict, restore_host: str, surface: str = "", now: float | None = None) -> Issued:
    """A folder coming back onto a replacement Mac."""
    now = time.time() if now is None else now
    ev = report_evidence(st, f["id"], "the new computer")
    label, sub = f["label"], f["id"]
    if st.get("empty_for_s", 0) >= EMPTY_RESTORE_AFTER_SECONDS:
        return _issue(db, now, FOLDER_RESTORE, sub, VIOLATED,
                      f"{label}: nothing to restore: its mirrors hold nothing for it (the first copy never finished before the computer was lost).",
                      [ev], {"empty": True, "restored": False}, surface, ev.observed_at)
    settled = st.get("need_files") == 0 and st.get("state") == "idle" and bool(st.get("local_files"))
    done = settled and ev.fresh(now)
    word = "restored" if done else "restoring"
    return _issue(db, now, FOLDER_RESTORE, sub, HOLDS if done else UNKNOWN,
                  f"{label}: {word} on {restore_host} -- {st.get('local_files', 0)} files so far."
                  + ("" if ev.fresh(now) else f" (no report for {age_words(ev.age(now))})"),
                  [ev], {"empty": False, "restored": done}, surface, ev.observed_at)


EMPTY_RESTORE_AFTER_SECONDS = 60  # idle with nothing in it this long: the mirrors hold nothing for it


def restore_empty(st: dict) -> bool:
    """A restoring folder whose mirrors have nothing for it (it never finished copying before the Mac was lost)."""
    return bool(st.get("restoring")) and st.get("empty_for_s", 0) >= EMPTY_RESTORE_AFTER_SECONDS


def folder_done(db, f, surface: str = "", now: float | None = None) -> Issued:
    """FOLDER_PROTECTED for logic (the guide's gate): `.data["done"]` is true only when the claim holds on a fresh
    report AND a connected mirror shows the first copy finished. Read the value, never the text."""
    c = folder_report(db, f, {}, surface=surface, now=now)
    c.data["done"] = c.holds and bool(c.data.get("copy_done"))
    return c


def unreadable_items(db, f, surface: str = "", now: float | None = None) -> Issued:
    """ITEMS_READABLE for a folder: VIOLATED with data["n"] while the owner's last report counts unreadable items
    (a negative needs no fresh report), HOLDS only on a fresh report with none, otherwise UNKNOWN."""
    now = time.time() if now is None else now
    st = mirroring.latest_status(db, f["id"]) or {}
    ev = report_evidence(st or None, f["id"])
    n = st.get("unreadable_items") or 0
    if n:
        return _issue(db, now, ITEMS_READABLE, f["id"], VIOLATED, f"{f['label']}: {n} item{'s' if n != 1 else ''} can't be read",
                      [ev], {"n": n, "label": f["label"]}, surface, ev.observed_at)
    state = HOLDS if st and ev.fresh(now) else UNKNOWN
    return _issue(db, now, ITEMS_READABLE, f["id"], state, "" if state == HOLDS else f"{f['label']}: not confirmed",
                  [ev], {"n": 0, "label": f["label"]}, surface, ev.observed_at)


def unreadable_line(items: dict) -> str:
    """"Pictures: 3 items can't be read; Music: 1 item can't be read" from {label: n}."""
    return "; ".join(f"{label}: {n} item{'s' if n != 1 else ''} can't be read" for label, n in items.items())


def copy_progress(db, f, surface: str = "", now: float | None = None) -> Issued:
    """How the first copy is going, from the owner's own report -- and only as fresh as that report."""
    now = time.time() if now is None else now
    st = mirroring.latest_status(db, f["id"]) or {}
    ev = report_evidence(st or None, f["id"])
    label = f["label"]
    mirrors = _mirrors(st)
    if not any(peer_seen(p) for p in mirrors):
        return _issue(db, now, COPY_PROGRESS, f["id"], UNKNOWN, f"Connecting to your friend's computer to copy {label}…",
                      [ev], {"pct": 0.0, "connected": False}, surface, ev.observed_at)
    pct = max((p.get("completion", 0) for p in mirrors), default=0)
    if not ev.fresh(now):
        return _issue(db, now, COPY_PROGRESS, f["id"], UNKNOWN,
                      f"Copying {label}: paused at {pct:.0f}%. Your computer hasn't reported for {age_words(ev.age(now))}; it carries on when it is on.",
                      [ev], {"pct": pct, "connected": True, "paused": True}, surface, ev.observed_at)
    return _issue(db, now, COPY_PROGRESS, f["id"], HOLDS, f"Copying {label}: {pct:.0f}%", [ev],
                  {"pct": pct, "connected": True, "paused": False}, surface, ev.observed_at)


def holder_view(db, folder_ids: list, surface: str = "", now: float | None = None) -> tuple[int, int, int, int]:
    """The giver's view of what friends mirror on their computer, from each friend's own report:
    (bytes held, folders still copying, folders not confirmed lately, folders confirmed current)."""
    now = time.time() if now is None else now
    total = copying = unconfirmed = current = 0
    for fid in folder_ids:
        st = mirroring.latest_status(db, fid) or {}
        ev = report_evidence(st or None, fid, "the friend's computer")
        total += st.get("local_bytes", 0) or 0
        if not copy_done(st):
            state, text = UNKNOWN, "still copying"
            copying += 1
        elif not ev.fresh(now):
            state, text = UNKNOWN, "not confirmed lately"
            unconfirmed += 1
        else:
            state, text = HOLDS, "up to date"
            current += 1
        _issue(db, now, HOLDER_CURRENT, fid, state, text, [ev], {}, surface, ev.observed_at)
    return total, copying, unconfirmed, current


def holding_text(n_folders: int, total_bytes: int, copying: int, unconfirmed: int) -> str:
    if not n_folders:
        return ", holding nothing yet"
    what = f"{n_folders} folder{'s' if n_folders > 1 else ''} of theirs" + (f" ({_human(total_bytes)})" if total_bytes else "")
    state = "still copying" if copying else ("not confirmed lately (their computer hasn't reported)" if unconfirmed else "up to date")
    return f", holding {what}, {state}"


# --- a computer is online -----------------------------------------------------
def mac_online(db, host_id: int, label: str, presence: bool, friend: bool = False, surface: str = "", now: float | None = None) -> Issued:
    """"online"/"offline" for a paired computer. Presence (the app's beat) is one piece of evidence; a fresh status
    report from that computer, or a friend's fresh report that this one is connected to them, are others -- so a
    casper_service restart, which empties presence, doesn't make a computer that is plainly reporting look off."""
    now = time.time() if now is None else now
    evs = [Evidence("presence", label, "casper_service", "presence beat", E1, now if presence else None, {"connected": presence}, PRESENCE_MAX_AGE)]
    row = db.execute(
        "SELECT MAX(ms.updated_at) AS t FROM mirror_status ms JOIN mirror_folders mf ON mf.id = ms.folder_id "
        "WHERE (mf.owner_host_id = ? AND mf.restore_host_id IS NULL) OR mf.restore_host_id = ?", (host_id, host_id)).fetchone()
    if row and row["t"]:
        evs.append(Evidence("status_report", label, "that computer", "status report", E1, to_unix(row["t"]), {}, REPORT_MAX_AGE))
    dev = mirroring._device_for_host(db, host_id)
    if dev:
        for r in db.execute("SELECT ms.status, ms.updated_at FROM mirror_targets mt JOIN mirror_status ms ON ms.folder_id = mt.folder_id "
                            "WHERE mt.peer_host_id = ? AND mt.removed_at IS NULL", (host_id,)).fetchall():
            peers = json.loads(r["status"]).get("peers", [])
            if any(p.get("device_id") == dev["device_id"] and p.get("connected") for p in peers):
                evs.append(Evidence("peer_connected", label, "the owner's computer", "status report", E1, to_unix(r["updated_at"]), {}, PEER_SEEN_MAX_AGE))
    best = max((e.observed_at for e in evs if e.fresh(now) and e.observed_at is not None), default=None)
    up = best is not None
    heard = max((e.observed_at for e in evs if e.observed_at is not None and e.kind != "presence"), default=None)
    return _issue(db, now, MAC_ONLINE, str(host_id), HOLDS if up else UNKNOWN, "online" if up else "offline",
                  evs, {"online": up, "last_heard": heard, "label": label}, surface, best)


# --- recovery kit --------------------------------------------------------------
def kit_saved(db, user_id: int, saved_at: str | None, surface: str = "", now: float | None = None) -> Issued:
    """A kit was made (the Mac reported it). Casper can't see the file afterwards, and whether a copy is
    kept off the Mac is an instruction, not something observed -- so it says "recorded", never "safe"."""
    now = time.time() if now is None else now
    t = to_unix(saved_at)
    ev = Evidence("kit_saved", str(user_id), "the person's computer", "event report", E1, t, {}, KIT_MAX_AGE)
    if not saved_at:
        return _issue(db, now, KIT_SAVED, str(user_id), UNKNOWN, "No recovery kit has been made.", [ev], {"saved": False}, surface)
    when = (saved_at or "")[:10]
    if ev.fresh(now):
        return _issue(db, now, KIT_SAVED, str(user_id), HOLDS, f"Recovery kit made {when}. Casper can't see the file, so keep a copy and the passphrase off this computer.",
                      [ev], {"saved": True}, surface, t)
    return _issue(db, now, KIT_SAVED, str(user_id), UNKNOWN, f"Recovery kit last made {when}; making a new one is cheap.", [ev], {"saved": True, "old": True}, surface, t)


# --- invites -------------------------------------------------------------------
def invite_accepted(db, inv: dict, who: str | None, surface: str = "", now: float | None = None) -> Issued:
    """From Casper's own record of the invite (a database row: E2). "Accepted" never reverts for a
    finished invite, and an unaccepted one says when it lapses."""
    now = time.time() if now is None else now
    ev = Evidence("invite_row", str(inv["id"]), "casper_service", "database row", E2, to_unix(inv.get("used_at")) or now, {}, None)
    if inv.get("used_by_user_id"):
        done = bool(inv.get("completed_at"))
        text = (f"{who}{' (invited as ' + inv['note'] + ')' if inv.get('note') else ''}: accepted {(inv.get('used_at') or '')[:10]}, set up"
                if done else f"accepted by {who}, still setting up")
        return _issue(db, now, INVITE_ACCEPTED, str(inv["id"]), HOLDS, text, [ev], {"accepted": True, "setup_done": done, "by": who}, surface, ev.observed_at)
    return _issue(db, now, INVITE_ACCEPTED, str(inv["id"]), VIOLATED, f"not accepted yet, expires {(inv.get('expires_at') or '')[:10]}",
                  [ev], {"accepted": False, "setup_done": False}, surface, ev.observed_at)


# --- actions -------------------------------------------------------------------
# What a tool says when it didn't do the thing. Results are free text (the model reads them), so the one
# place that decides "did it happen?" is here, not at each call site.
_FAILURE = re.compile(
    r"^(can't|cannot|not done|not now|that didn't work|missing argument|unknown tool|didn't|that's no longer|no such|"
    r"couldn't|failed|that isn't|that is not|isn't|nothing to|mode must|you can't|denied|declined|"
    r"[^\n]{0,60} (isn't one of your|isn't reachable|is offline))", re.I)
_PREVIEW = re.compile(r"^preview\b", re.I)


def action_done(db, action: str, result: str, subject: str = "", surface: str = "", now: float | None = None) -> Issued:
    """ACTION_DONE for one tool call, from its result: HOLDS only when the result isn't a refusal, a failure or a
    preview. The guide says "Done." from this state, never from "a call with preview=false was made"."""
    now = time.time() if now is None else now
    r = (result or "").strip()
    ev = Evidence("tool_result", action, "casper_service", "tool result", E2, now, {}, 60)
    if not r or _FAILURE.match(r):
        state, text = VIOLATED, "It didn't go through."
    elif _PREVIEW.match(r):
        state, text = UNKNOWN, "Nothing changed yet."
    else:
        state, text = HOLDS, "Done."
    return _issue(db, now, ACTION_DONE, subject or action, state, text, [ev], {"action": action}, surface, now)


# --- recovery after a lost computer (guide gates) -------------------------------
def recovery(db, user_id: int, profile: dict, folders: list, fresh_hosts: set, pairing_hosts: set, surface: str = "",
             now: float | None = None) -> Issued:
    """Where a recovery stands, from pairings and folder rows -- not from which computers happen to be awake.
    `fresh_hosts` are computers paired since the person said they lost one; `folders` are (owner_host_id, restore_host_id)
    rows. The new computer sleeping never moves anyone back a step (2026-10-16 inventory, finding 5):
    a folder still on a computer that is not one of the new ones is lost; one being restored to a new one is restoring."""
    now = time.time() if now is None else now
    restoring = any(r["restore_host_id"] in fresh_hosts for r in folders)
    lost = any(r["restore_host_id"] is None and r["owner_host_id"] not in fresh_hosts for r in folders)
    unlocked = bool(profile.get("recovery_unlocked_at"))
    recovering = restoring or lost
    recovered = unlocked and bool(folders) and not recovering
    ev = Evidence("recovery_rows", str(user_id), "casper_service", "database rows", E2, now, {}, None)
    state = HOLDS if recovered else UNKNOWN
    return _issue(db, now, RECOVERY, str(user_id), state, "folders are back" if recovered else "recovery under way",
                  [ev], {"restoring": restoring, "lost": lost, "recovering": recovering, "recovered": recovered,
                         "unlocked": unlocked, "new_mac": bool(fresh_hosts & pairing_hosts)}, surface, now)


# --- wording that other modules need (scripted lines, notifications) --------------
def say_protected_lead() -> str:
    return "Protected. "


def say_copy_started() -> str:
    return "Copying now. You can leave this page open."


def say_restore_started() -> str:
    return "Restoring now. You can leave this page open."


def say_unreadable(items: dict) -> str:
    return f"{unreadable_line(items)}. Casper can't copy those; everything else is being kept safe."


def say_folders_safe(labels: list, names) -> str:
    return f"Your {names(labels)} {'is' if len(labels) == 1 else 'are'} safe on your friends' computers. "


def say_folders_not_copied(labels: list, names) -> str:
    return f"{names(labels)} never finished copying, so {'it' if len(labels) == 1 else 'they'} can't come back. "


def say_folders_back(noun: str, landing: str, old_retired: bool) -> str:
    return (f"Your folders are back on this {noun}, and Casper keeps them mirrored." + landing
            + (" Your old computer is no longer paired with your account; its copies and history are kept." if old_retired else ""))


def say_nothing_to_bring_back(label: str) -> str:
    return f"{label}: nothing to bring back; it never finished copying to your friends' computers."


def say_restoring_progress(label: str, files: int) -> str:
    return f"Restoring {label}: {files} files so far…"


def say_promoted(label: str, host: str) -> str:
    return f"{label} is back on {host}, and this computer keeps it mirrored now."


def say_nudge(n: int, label: str) -> str:
    return (f"{n} recent change(s) in your {label} aren't on any mirror yet -- "
            "the computers mirroring it have been off for a while. Nothing to do if they'll be back soon; "
            "otherwise, ask your agent about adding a mirror or a catcher.")


def say_consent_asked(source_host: str) -> str:
    return (f"NOT protected yet. Nothing is mirrored until the person confirms. Tell them: a Casper dialog is open on {source_host}; "
            "click Mirror (or answer in Telegram). You can't confirm it for them. Once they say they have, check "
            "protection_status before telling them they're protected.")


def say_consent_asked_again(label: str, source_host: str) -> str:
    return (f"NOT protected yet. The dialog for {label} was shown again on {source_host}; the person clicks Mirror there "
            "(or answers in Telegram). You can't confirm it for them. Once they say they have, check protection_status.")


def say_mirroring_started(label: str, where: str) -> str:
    return f"Mirroring {label} to {where}. Changes reach them whenever both computers are on."


# --- the model's own words -------------------------------------------------------
# Words that assert the promise. A model reply may use them only when this turn issued a HOLDS claim of
# FOLDER_PROTECTED (a tool result it read); otherwise Casper's own sentence replaces the reply.
_ASSERT = re.compile(r"\b(protected|safe(?: and sound)?|backed up|up to date|nothing is gone|all (?:your )?(?:files|folders) (?:are|is))\b", re.I)
_NEGATED = re.compile(r"(not|n't|never|until|yet|if|unless|nothing|no|neither)\W+(?:\w+\W+){0,3}$", re.I)


def asserts_protection(text: str) -> bool:
    for m in _ASSERT.finditer(text or ""):
        if not _NEGATED.search(text[max(0, m.start() - 40):m.start()]):
            return True
    return False


def reply_backed(text: str, issued: list | None = None) -> bool:
    """May this model reply say what it says? True when it asserts nothing, or a fresh HOLDS FOLDER_PROTECTED was issued this turn."""
    if not asserts_protection(text):
        return True
    issued = issued_this_turn() if issued is None else issued
    return any(c.claim == FOLDER_PROTECTED and c.state == HOLDS for c in issued)
