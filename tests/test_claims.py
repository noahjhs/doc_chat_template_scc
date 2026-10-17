"""The claims module (casper_service/claims.py, docs/product/claims.md): each
past case where Casper told someone something untrue is a test here, plus the
rules that stop the next one: stale evidence never gives a positive claim,
logic reads claim values not text, and nothing outside claims.py writes the
claim sentences."""

import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from test_v1_platform import CASPER_SERVICE_DIR, _pair, _signup, _user_id, app_env  # noqa: F401  (app_env is a fixture)

from fake_daemon import FakeDaemon

NEVER = "1969-12-31T16:00:00-08:00"


def _iso(ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=ago)).isoformat(timespec="seconds")


def _peer(**kw):
    return {"device_id": "DEVICE1", "role": "mirror", "connected": True, "completion": 100, "need_files": 0, "last_seen": "2026-10-12T01:00:00Z", **kw}


def _status(ago: float = 0, **kw) -> dict:
    st = {"local_files": 5, "local_bytes": 2048, "unprotected_files": 0, "at_risk_files": 0, "unreadable_items": 0, "peers": [_peer()], **kw}
    return {**st, "reported_at": _iso(ago)}


FOLDER = {"id": "f1", "label": "Documents", "status": "active", "restore_host_id": None, "owner_user_id": 1, "owner_host_id": 1}


@pytest.fixture()
def claims(app_env):
    main, _ = app_env
    return main.claims, main


def report(claims_mod, main, st, folder=FOLDER, **kw):
    with main.get_db() as db:
        return claims_mod.folder_report(db, folder, {"DEVICE1": "riley/Riley's Mac"}, st=st, **kw)


# --- the truth bugs, one test each ---------------------------------------------
def test_not_protected_while_the_copy_is_still_running(claims):
    """2026-10-16 run: the guide said Protected ~20 s after Mirror while the holder said "still copying".
    Connected and nothing flagged is not a finished copy."""
    c, main = claims
    for need, completion in ((12, 100), (0, 41)):
        got = report(c, main, _status(peers=[_peer(need_files=need, completion=completion)]))
        assert not got.holds and "NOT protected yet" in got.text and ": protected." not in got.text
    assert report(c, main, _status()).holds


def test_never_safe_while_nothing_was_ever_copied(claims):
    """Noah's walkthrough: the Mac never reached a mirror, nothing was missing on paper, the guide said Protected.
    Syncthing says 100% for a peer it never reached."""
    c, main = claims
    never = _status(peers=[_peer(connected=False, completion=100, last_seen=NEVER)])
    got = report(c, main, never)
    assert got.state == c.VIOLATED and "no mirror has connected yet" in got.text
    assert c.copy_done(never) is False
    assert got.data["copy_done"] is False


def test_an_empty_or_unscanned_folder_is_not_protected(claims):
    """Inventory finding 3: 100% of nothing is not protection."""
    c, main = claims
    for files in (0, None):
        st = _status(local_files=files)
        got = report(c, main, st)
        assert got.state == c.UNKNOWN and not got.holds and "nothing in it has been seen" in got.text
        assert c.copy_done(st) is False


def test_an_accepted_invite_is_never_reported_as_not_accepted(claims):
    """Riley's agent told her Noah hadn't accepted after he had (the invite leaves the 'open' list at first mirror)."""
    c, main = claims
    with main.get_db() as db:
        base = {"id": 3, "note": "Noah", "used_at": "2026-10-12T01:00:00+00:00", "expires_at": "2026-10-19T01:00:00+00:00"}
        pending = c.invite_accepted(db, {**base, "used_by_user_id": None, "used_at": None, "completed_at": None}, None)
        mid = c.invite_accepted(db, {**base, "used_by_user_id": 2, "completed_at": None}, "noah")
        done = c.invite_accepted(db, {**base, "used_by_user_id": 2, "completed_at": "2026-10-12T02:00:00+00:00"}, "noah")
    assert pending.state == c.VIOLATED and "not accepted yet" in pending.text
    assert mid.holds and mid.text == "accepted by noah, still setting up" and mid.data["setup_done"] is False
    assert done.holds and done.data["setup_done"] and "accepted 2026-10-12" in done.text and "not accepted" not in done.text


def test_riley_is_not_offline_while_plainly_reporting(app_env):
    """A casper_service restart empties presence for every computer until its next beat. A Mac whose status report
    arrived seconds ago (or that a friend's fresh report shows connected) is online."""
    main, client = app_env
    c = main.claims
    with client, FakeDaemon(respond_with=lambda b: {}) as sd:
        _, riley = _signup(client, "riley")
        _pair(client, riley, "rk-riley", "riley-mac", sd.url)
        host_id = next(h["host_id"] for h in client.get("/hosts", headers=riley).json()["hosts"] if h["label"] == "riley-mac")
        uid = _user_id(main, "riley")
        with main.get_db() as db:
            assert c.mac_online(db, host_id, "riley-mac", presence=True).holds  # beating
            assert c.mac_online(db, host_id, "riley-mac", presence=True).text == "online"
            nothing = c.mac_online(db, host_id, "riley-mac", presence=False)  # restarted, silent
            assert not nothing.holds and nothing.text == "offline"
            db.execute("PRAGMA foreign_keys = OFF")
            db.execute("INSERT INTO mirror_folders (id, owner_user_id, owner_host_id, path, label, status) VALUES ('f1', ?, ?, '~/Documents', 'Documents', 'active')",
                       (uid, host_id))
            db.execute("INSERT INTO mirror_status (folder_id, status, updated_at) VALUES ('f1', ?, ?)", (json.dumps(_status()), _iso(10)))
            assert c.mac_online(db, host_id, "riley-mac", presence=False).holds  # reported 10 s ago
            db.execute("UPDATE mirror_status SET updated_at = ?", (_iso(900),))
            stale = c.mac_online(db, host_id, "riley-mac", presence=False)
            assert not stale.holds and stale.data["last_heard"] is not None  # a quarter hour of silence: offline, with when we last heard
        # and list_hosts / my_casper say the same thing
        main._live.clear()
        with main.get_db() as db:
            db.execute("UPDATE mirror_status SET updated_at = ?", (_iso(5),))
        assert main._mcp_list_hosts(uid)[0]["connected"] is True
        assert "riley-mac (online)" in main._mcp_my_casper(uid)


# --- freshness ------------------------------------------------------------------
def test_a_report_that_went_quiet_never_supports_protected(claims):
    c, main = claims
    fresh = report(c, main, _status(ago=30))
    assert fresh.holds and "protected." in fresh.text
    stale = report(c, main, _status(ago=60 * 60), owner_host="Riley's Mac")
    assert stale.state == c.UNKNOWN and not stale.holds
    assert ": protected." not in stale.text and "not confirmed" in stale.text and "1 h" in stale.text
    assert "not confirmed (no report" in stale.text  # the mirror lines lapse too: no "up to date" from an old report
    assert "up to date" not in stale.text


def test_a_stale_bad_report_stays_bad(claims):
    """Freshness only takes positives away: an old 'not protected' is never softened."""
    c, main = claims
    old_bad = report(c, main, _status(ago=3600, unprotected_files=3, unprotected_oldest_unix=time.time() - 7200))
    assert old_bad.state == c.VIOLATED and "3 recent change(s)" in old_bad.text
    unreadable = report(c, main, _status(ago=3600, unreadable_items=2))
    assert unreadable.state == c.VIOLATED and "2 items can't be read" in unreadable.text


def test_weaker_evidence_never_gives_a_stronger_claim(claims):
    """Property: over every combination of report content and age, staleness can only take a positive away."""
    c, main = claims
    variants = []
    for peers in ([], [_peer()], [_peer(connected=False)], [_peer(need_files=3)], [_peer(connected=False, last_seen=NEVER)]):
        for extra in ({}, {"unprotected_files": 2}, {"unreadable_items": 1}, {"at_risk_files": 2}, {"local_files": 0}):
            variants.append({"peers": peers, **extra})
    rank = {c.VIOLATED: 0, c.UNKNOWN: 1, c.HOLDS: 2}
    for v in variants:
        fresh, stale = report(c, main, _status(ago=1, **v)), report(c, main, _status(ago=10_000, **v))
        assert rank[stale.state] <= rank[fresh.state], v
        assert not stale.holds, v
        if fresh.holds:  # a positive sentence only with a positive state
            assert "protected." in fresh.text
        if not fresh.holds:
            assert ": protected." not in fresh.text, v


def test_no_report_at_all_is_unknown(claims):
    c, main = claims
    got = report(c, main, {}, owner_host="Riley's Mac")
    assert got.state == c.UNKNOWN and "no report from Riley's Mac yet" in got.text


def test_copy_progress_says_so_when_the_report_stops(claims):
    c, main = claims
    f = FOLDER
    with main.get_db() as db:
        db.execute("PRAGMA foreign_keys = OFF")
        db.execute("INSERT INTO mirror_folders (id, owner_user_id, owner_host_id, path, label, status) VALUES ('f1', 1, 1, '~/Documents', 'Documents', 'active')")
        db.execute("INSERT INTO mirror_status (folder_id, status, updated_at) VALUES ('f1', ?, ?)", (json.dumps(_status(peers=[_peer(completion=42)])), _iso(5)))
        assert c.copy_progress(db, f).text == "Copying Documents: 42%"
        db.execute("UPDATE mirror_status SET updated_at = ?", (_iso(1800),))
        got = c.copy_progress(db, f)
    assert not got.holds and "paused at 42%" in got.text and "30 min" in got.text


def test_the_givers_view_is_not_confirmed_when_the_friend_goes_quiet(claims):
    c, main = claims
    with main.get_db() as db:
        db.execute("PRAGMA foreign_keys = OFF")
        db.execute("INSERT INTO mirror_status (folder_id, status, updated_at) VALUES ('f1', ?, ?)", (json.dumps(_status()), _iso(5)))
        assert c.holding_text(1, 2048, *c.holder_view(db, ["f1"])[1:3]).endswith("up to date")
        db.execute("UPDATE mirror_status SET updated_at = ?", (_iso(7200),))
        total, copying, unconfirmed, _ = c.holder_view(db, ["f1"])
    assert "not confirmed lately" in c.holding_text(1, total, copying, unconfirmed) and "up to date" not in c.holding_text(1, total, copying, unconfirmed)


# --- logic reads values, never text ------------------------------------------------
def test_the_guide_reads_the_claim_not_the_sentence(app_env, monkeypatch):
    """Inventory finding 2: the guide's `protected` fact used to be `": protected." in _status_text(...)`; a copy edit
    changed the setup flow. Now the wording can be anything."""
    main, client = app_env
    with client:
        _, riley = _signup(client, "riley")
        uid = _user_id(main, "riley")
        with main.get_db() as db:
            db.execute("PRAGMA foreign_keys = OFF")
            db.execute("INSERT INTO mirror_folders (id, owner_user_id, owner_host_id, path, label, status) VALUES ('f1', ?, 1, '~/Documents', 'Documents', 'active')", (uid,))
            db.execute("INSERT INTO mirror_status (folder_id, status, updated_at) VALUES ('f1', ?, ?)", (json.dumps(_status()), _iso(5)))
        monkeypatch.setattr(main, "_status_text", lambda *a, **k: "gibberish nobody could parse")
        assert main._guide_facts(uid, []).protected is True
        with main.get_db() as db:  # the same report, gone quiet: the fact turns false
            db.execute("UPDATE mirror_status SET updated_at = ?", (_iso(3600),))
        assert main._guide_facts(uid, []).protected is False


# --- actions ----------------------------------------------------------------------
@pytest.mark.parametrize("result,state", [
    ("Done: 20 GB of mirror space on riley/Rileys-Mac.", "holds"),
    ("Sent noah a friend request. They'll be asked to accept.", "holds"),
    ("Can't use that invite: it has expired.", "violated"),
    ("Can't: that computer isn't connected.", "violated"),
    ("That didn't work: boom", "violated"),
    ("Not now: that's not this step.", "violated"),
    ("PREVIEW (nothing changed yet): Accept.", "unknown"),
    ("", "violated"),
])
def test_action_done_follows_the_result(claims, result, state):
    c, main = claims
    with main.get_db() as db:
        assert c.action_done(db, "redeem_invite", result).state == state


def test_the_guide_says_done_only_when_it_was(app_env):
    """Inventory finding 4: any tool call with preview=false made the guide say "Done." even when the result was a refusal."""
    import guide

    main, _ = app_env

    class Svc:
        def __init__(self, out):
            self.out = out

        def redeem_invite(self, uid, code, preview):
            return self.out

    class Call:
        def __init__(self, name, args):
            self.id, self.function = "c1", type("F", (), {"name": name, "arguments": json.dumps(args)})()

    def run(out):
        script = [[Call("redeem_invite", {"code": "C", "preview": False})], ""]

        class Client:
            class chat:
                class completions:
                    @staticmethod
                    def create(**kw):
                        step = script.pop(0)
                        msg = type("M", (), {"content": step if isinstance(step, str) else "", "tool_calls": None if isinstance(step, str) else step})()
                        return type("R", (), {"choices": [type("C", (), {"message": msg})()]})()

        stage = guide.Stage("install", "x", ("redeem_invite",), say="Download Casper.")
        reply, _ = guide.run(Client, "m", Svc(out), 1, [], "yes", "p", stage_fn=lambda m: stage)
        return reply

    assert run("Done: 20 GB of mirror space.") == "Done.\n\nDownload Casper."
    failed = run("Can't use that invite: it has expired.")
    assert "Done." not in failed and failed == "Download Casper."


# --- recovery: asleep is not lost ---------------------------------------------------
def test_a_recovered_mac_that_sleeps_does_not_become_lost_again(claims):
    """Inventory finding 5: `lost` was recomputed from who is connected this minute, so a recovered person whose new
    Mac slept for three minutes was sent back into "recovering"."""
    c, main = claims
    new_host, old_host = 7, 3
    profile = {"recovery_unlocked_at": "2026-10-16T10:00:00+00:00"}
    with main.get_db() as db:
        recovered = c.recovery(db, 1, profile, [{"owner_host_id": new_host, "restore_host_id": None}], {new_host}, {new_host})
        assert recovered.data["recovered"] and not recovered.data["recovering"]
        # the old Mac still owns a folder: that is lost, whatever is awake
        lost = c.recovery(db, 1, profile, [{"owner_host_id": old_host, "restore_host_id": None}], {new_host}, {new_host, old_host})
        assert lost.data["lost"] and lost.data["recovering"] and not lost.data["recovered"]
        # mid-restore on a new Mac that is asleep: still restoring, never "back"
        restoring = c.recovery(db, 1, profile, [{"owner_host_id": old_host, "restore_host_id": new_host}], {new_host}, {new_host, old_host})
        assert restoring.data["restoring"] and not restoring.data["recovered"] and restoring.data["new_mac"]


# --- the model's own words ----------------------------------------------------------
def test_a_reply_may_assert_protection_only_with_a_claim_behind_it(claims):
    c, main = claims
    holds = c.Issued(c.FOLDER_PROTECTED, "f1", c.HOLDS, "x")
    nope = c.Issued(c.FOLDER_PROTECTED, "f1", c.UNKNOWN, "x")
    for text in ("Your Documents folder is protected.", "All your files are safe.", "It's backed up and up to date."):
        assert not c.reply_backed(text, [])
        assert not c.reply_backed(text, [nope])
        assert c.reply_backed(text, [holds])
    for text in ("Documents isn't protected yet.", "NOT protected yet: copying.", "Nothing is safe until a mirror has it.", "Which folder?"):
        assert c.reply_backed(text, [])


def test_the_guide_replaces_an_unbacked_protected(app_env):
    import guide

    main, _ = app_env

    class Svc:
        def protection_status(self, uid):
            return "Documents: NOT protected yet: no mirror has connected yet, so nothing has been copied.\n  (5 files; as of x)\nNext: quote these lines."

    class Client:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    msg = type("M", (), {"content": "Great, your Documents are protected!", "tool_calls": None})()
                    return type("R", (), {"choices": [type("C", (), {"message": msg})()]})()

    reply, _ = guide.run(Client, "m", Svc(), 1, [], "how is it going", "p")
    assert reply == "Documents: NOT protected yet: no mirror has connected yet, so nothing has been copied."


# --- the log ------------------------------------------------------------------------
def test_issued_claims_are_logged_with_their_evidence(claims):
    c, main = claims
    with main.get_db() as db:
        c.folder_report(db, FOLDER, {}, st=_status(ago=5), surface="my_casper")
        c.folder_report(db, FOLDER, {}, st=_status(ago=6), surface="my_casper")  # same claim, same state: not repeated
        c.folder_report(db, FOLDER, {}, st=_status(ago=3600), surface="my_casper")  # lapsed: a new row
        rows = db.execute("SELECT claim, subject, state, surface, evidence, text FROM claim_log WHERE claim = 'folder_protected' ORDER BY id").fetchall()
    assert [r["state"] for r in rows] == ["holds", "unknown"]
    assert rows[0]["surface"] == "my_casper" and rows[0]["subject"] == "f1"
    ev = json.loads(rows[0]["evidence"])
    assert ev[0]["kind"] == "status_report" and ev[0]["level"] == 1 and ev[0]["fresh"] is True
    assert json.loads(rows[1]["evidence"])[0]["fresh"] is False


def test_a_required_readback_is_the_hook_for_stronger_evidence(claims):
    """The pilot rests on a fresh report (E1). With REQUIRE_READBACK on, 'protected' also needs a fresh sampled read (E3)."""
    c, main = claims
    c.REQUIRE_READBACK = True
    try:
        got = report(c, main, _status())
        assert not got.holds and "No mirror has read back a sample" in got.text
        c.register_readback(lambda db, f, now: c.Evidence("sampled_read", f["id"], "owner's computer", "random block read-back", c.E3, now - 60, {}, c.READBACK_MAX_AGE))
        assert report(c, main, _status()).holds
    finally:
        c.REQUIRE_READBACK = False
        c._readback_sources.clear()


# --- nothing else writes these sentences ----------------------------------------------
def test_only_claims_writes_the_claim_sentences():
    """The CI check from docs/product/claims.md 5.3: claim vocabulary outside claims.py is a failure. Rules text for
    models (prompts, tool descriptions) is allowed; a line may opt out with `# claims-ok` and a reason."""
    banned = [r"\bup to date\b", r"NOT protected", r"NOT fully protected", r"\bprotected\.", r"can't be read", r"\(online\)", r"'online'", r"'offline'",
              r"keeps? (?:it|them) mirrored", r"safe on your friends", r"is back on", r"never finished copying"]
    pattern = re.compile("|".join(banned))
    allowed_files = {"claims.py"}
    offenders = []
    for path in sorted(Path(CASPER_SERVICE_DIR).glob("*.py")):
        if path.name in allowed_files:
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if "claims-ok" in line:
                continue
            if pattern.search(line) and not _is_rules_text(path.name, line):
                offenders.append(f"{path.name}:{n}: {line.strip()[:100]}")
    assert not offenders, "claim sentences written outside claims.py:\n" + "\n".join(offenders)


def _is_rules_text(name: str, line: str) -> bool:
    """Instructions TO a model (never say..., only when...) rather than a sentence said to a person."""
    s = line.strip().lower()
    return name in ("mcp_server.py", "guide.py") and (
        "never" in s or "until" in s or "don't" in s or s.startswith(('"', "'", "#", "-", "_tool(")) or "description=" in s)
