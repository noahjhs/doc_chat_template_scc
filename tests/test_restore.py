"""File restore (docs/product/restore-design.md): the pure rules, and the
tools driven against a fake owner Mac and mirror (no Syncthing; the real
bytes are covered by test_mirror_integration.py)."""

import base64
import inspect
import json
import threading
from datetime import datetime, timedelta, timezone

from fake_daemon import FakeDaemon
from test_v1_platform import _ok, _pair, _signup, _user_id, app_env  # noqa: F401  (app_env is the fixture)

PDT = timezone(timedelta(hours=-7))


def _t(days_ago, hour=10):
    d = (datetime.now(PDT) - timedelta(days=days_ago)).replace(hour=hour, minute=0, second=0, microsecond=0)
    return d.isoformat()


def _v(name, at, modified, size=1000, exists_now=True, deleted=False):
    return {"name": name, "at": at, "modified": modified, "size": size, "exists_now": exists_now, "deleted": deleted,
            "encrypted_path": f"enc/{name}"}


def test_which_version_was_live_at_a_moment(app_env):
    import restore as r
    vs = [_v("a.txt", "2026-10-14T10:00:00-07:00", "2026-10-13T09:00:00-07:00"),
          _v("a.txt", "2026-10-15T10:00:00-07:00", "2026-10-14T10:00:00-07:00")]
    local = r.offset_of(vs)
    live = lambda when_: (r.live_at(vs, r.when(when_, local)) or {}).get("at")  # noqa: E731
    assert live("2026-10-13T12:00") == "2026-10-14T10:00:00-07:00"  # the first was current, replaced next day
    assert live("2026-10-14") == "2026-10-15T10:00:00-07:00"  # a bare date is the end of that day
    assert live("2026-10-16") is None  # unchanged since: the current file is the answer
    assert live("2026-10-12") is None  # didn't exist yet
    more = vs + [_v("b.txt", "2026-10-16T10:00:00-07:00", "2026-10-12T00:00:00-07:00")]
    assert set(r.as_of(more, r.when("2026-10-14", local))) == {"a.txt", "b.txt"}


def test_a_restore_fits_as_a_copy_else_as_a_replacement_else_not_at_all(app_env):
    import restore as r
    big = 10 << 30
    assert r.fit([100], 0, 1 << 30) == "copy"
    assert r.fit([big], 0, 25 << 30) == "copy"
    assert r.fit([big], 0, (20 << 30) + (100 << 20)) == "replace"  # room to work, not to keep both
    assert r.fit([big], 0, 5 << 30) == "none"
    assert r.fit([4 << 30], 2 << 30, int(9.5 * (1 << 30))) == "replace"  # a folder: its copy needs everything


def test_names_and_times_are_resolved_the_way_a_person_says_them(app_env):
    import restore as r
    vs = [_v("Budget 2026.xlsx", "2026-10-14T10:00:00-07:00", "2026-10-13T09:00:00-07:00"),
          _v("Budget 2026.xlsx", "2026-10-15T10:00:00-07:00", "2026-10-14T10:00:00-07:00"),
          _v("budget-old.xlsx", "2026-10-15T11:00:00-07:00", "2026-10-01T10:00:00-07:00"),
          _v("notes.txt", "2026-10-15T12:00:00-07:00", "2026-10-15T08:00:00-07:00")]
    local = r.offset_of(vs)
    assert r.pick_version(vs, "notes", "", local)[1]["name"] == "notes.txt"  # one match: no question
    names, none = r.pick_version(vs, "budget", "", local)
    assert none is None and names == ["Budget 2026.xlsx", "budget-old.xlsx"]  # ambiguous: the caller shows the picker
    assert r.pick_version(vs, "Budget 2026.xlsx", "", local)[1]["at"] == "2026-10-15T10:00:00-07:00"  # newest earlier
    assert r.pick_version(vs, "Budget 2026.xlsx", "2026-10-14T10:00:00-07:00", local)[1]["modified"] == "2026-10-13T09:00:00-07:00"
    assert r.pick_version(vs, "Budget 2026.xlsx", "2026-10-13T12:00", local)[1]["at"] == "2026-10-14T10:00:00-07:00"


class World:
    """A fake owner Mac and mirror behind main.backups.daemon_call."""

    def __init__(self, main, monkeypatch, versions, free=100 << 30, local_bytes=5000, label="Documents"):
        self.calls, self.free, self.local_bytes, self.versions = [], free, local_bytes, versions
        folder = {"id": "f1", "label": label, "owner_host_id": 1}

        def listing(uid, name):
            if name not in (label, "Pictures"):
                raise main.mirroring.MirrorError(f"No folder called {name}.")
            return folder, {"label": "mac"}, {"label": "peer"}, self.versions

        monkeypatch.setattr(main, "_version_listing", listing)
        monkeypatch.setattr(main.backups, "daemon_call", lambda config, action, **kw: self.call(action, **kw))
        monkeypatch.setattr(main, "_host_config", lambda *a: {"label": "mac"})

    def call(self, action, **kw):
        self.calls.append((action, kw))
        if action == "mirror_restore_plan":
            dest = f"/Users/x/Documents (as of {kw['as_of'][:10]})" if kw.get("scope") == "folder" and kw.get("mode") != "replace" else ""
            return _ok(free_bytes=self.free, local_bytes=self.local_bytes if dest else 0, dest=dest)[1]
        if action == "mirror_read_version":
            return _ok(content=base64.b64encode(b"x").decode(), total=1)[1]
        if action == "mirror_restore_version_finish":
            return _ok(restored_to=f"/Users/x/Documents/{kw['encrypted_path'].split('/')[-1]} (restored)")[1]
        if action == "mirror_restore_local":
            return _ok(files=3, bytes=5000)[1]
        return _ok()[1]

    def did(self, action):
        return [kw for a, kw in self.calls if a == action]


def test_restoring_a_file_is_a_copy_and_needs_no_confirmation(app_env, monkeypatch):
    main, _ = app_env
    w = World(main, monkeypatch, [_v("notes.txt", _t(1), _t(2)), _v("notes.txt", _t(3), _t(4)), _v("other.txt", _t(1), _t(2))])
    out = main._mcp_restore_version(1, "Documents", "notes")  # a part of the name, no time: the newest earlier version
    assert "nothing was overwritten" in out and "a new copy next to the original" in out
    (finish,) = w.did("mirror_restore_version_finish")
    assert finish["mode"] == "copy" and finish["scope"] == "file" and finish["stamp"] == _t(2)
    assert [a for a, _ in w.calls] == ["mirror_restore_plan", "mirror_read_version", "mirror_restore_version_chunk", "mirror_restore_version_finish"]


def test_a_name_that_matches_many_files_isnt_guessed(app_env, monkeypatch):
    main, _ = app_env
    w = World(main, monkeypatch, [_v("a1.txt", _t(1), _t(2)), _v("a2.txt", _t(1), _t(2))])
    out = main._mcp_restore_version(1, "Documents", "a")
    assert "2 files match" in out and "show_restore_picker" in out and not w.did("mirror_read_version")


def test_when_a_copy_doesnt_fit_replace_is_offered_never_deleting(app_env, monkeypatch):
    main, _ = app_env
    big = 10 << 30
    w = World(main, monkeypatch, [_v("video.mov", _t(1), _t(2), size=big)], free=(20 << 30) + (100 << 20))
    out = main._mcp_restore_version(1, "Documents", "video.mov")
    assert "free" in out and "mode='replace'" in out and "delete" not in out.replace("deleted", "")
    assert not w.did("mirror_read_version")  # nothing moved
    preview = main._mcp_restore_version(1, "Documents", "video.mov", mode="replace")
    assert preview.startswith("PREVIEW") and not w.did("mirror_read_version")  # replace waits for the person's yes
    done = main._mcp_restore_version(1, "Documents", "video.mov", mode="replace", preview=False)
    assert "in place of the current file" in done and "30 days" in done
    assert w.did("mirror_restore_version_finish")[0]["mode"] == "replace"
    w.free = 1 << 30  # not even a replacement fits
    assert "doesn't fit even as a replacement" in main._mcp_restore_version(1, "Documents", "video.mov")


def test_deleted_files_are_listed_and_restore_where_they_were(app_env, monkeypatch):
    main, _ = app_env
    World(main, monkeypatch, [_v("gone.txt", _t(1), _t(2), exists_now=False), _v("here.txt", _t(1), _t(2))])
    only = main._mcp_list_versions(1, "Documents", "", True)
    assert "gone.txt" in only and "here.txt" not in only and "deleted afterwards" in only
    assert "No deleted files" in main._mcp_list_versions(1, "Documents", "nothing-like-this", True)


def test_folders_as_of_a_time_copy_beside_the_originals(app_env, monkeypatch):
    main, _ = app_env
    day = (datetime.now(PDT) - timedelta(days=5)).strftime("%Y-%m-%d")
    w = World(main, monkeypatch, [
        _v("changed.txt", _t(2), _t(8)),  # replaced after the moment, written before: comes from history
        _v("deleted.txt", _t(3), _t(9), exists_now=False),
        _v("born-later.txt", _t(2), _t(4)),  # written after the moment: not there then
        _v("long-ago.txt", _t(10), _t(12)),  # replaced before the moment: the current file is the answer
    ])
    out = main._mcp_restore_as_of(1, ["Documents"], day)
    assert "2 files from history and 3 unchanged ones" in out and f"Documents (as of {day})" in out and "nothing was overwritten" in out
    assert sorted(kw["encrypted_path"] for kw in w.did("mirror_restore_version_finish")) == ["enc/changed.txt", "enc/deleted.txt"]
    assert all(kw["path"] == f"/Users/x/Documents (as of {day})" for kw in w.did("mirror_restore_version_finish"))
    (local,) = w.did("mirror_restore_local")
    assert local["names"] == ["changed.txt", "deleted.txt"] and local["path"].endswith(f"(as of {day})")  # not copied twice


def test_several_folders_one_call_and_the_history_limit(app_env, monkeypatch):
    main, _ = app_env
    w = World(main, monkeypatch, [_v("a.txt", _t(1), _t(3))])
    day = (datetime.now(PDT) - timedelta(days=2)).strftime("%Y-%m-%d")
    out = main._mcp_restore_as_of(1, ["Documents", "Pictures"], day)
    assert out.count("\n") == 1 and len(w.did("mirror_restore_local")) == 2
    assert "30 days of history" in main._mcp_restore_as_of(1, ["Documents"], "2020-01-01")
    assert "hasn't happened" in main._mcp_restore_as_of(1, ["Documents"], (datetime.now(PDT) + timedelta(days=3)).strftime("%Y-%m-%d"))
    assert "can't read" in main._mcp_restore_as_of(1, ["Documents"], "last tuesday")
    assert "No folder called Nope" in main._mcp_restore_as_of(1, ["Nope"], day)


def test_folder_restore_that_doesnt_fit_offers_replace(app_env, monkeypatch):
    main, _ = app_env
    w = World(main, monkeypatch, [_v("big.bin", _t(1), _t(3), size=4 << 30)], free=int(9.5 * (1 << 30)), local_bytes=2 << 30)
    day = (datetime.now(PDT) - timedelta(days=2)).strftime("%Y-%m-%d")
    out = main._mcp_restore_as_of(1, ["Documents"], day)
    assert "mode='replace'" in out and not w.did("mirror_read_version")
    assert main._mcp_restore_as_of(1, ["Documents"], day, mode="replace").startswith("PREVIEW")
    done = main._mcp_restore_as_of(1, ["Documents"], day, mode="replace", preview=False)
    assert "over the current ones" in done and not w.did("mirror_restore_local")  # replace copies nothing else


def test_the_agents_picker_restores_what_the_person_picks_on_their_mac(app_env, monkeypatch):
    """show_restore_picker: the app lists versions on the Mac; the pick (made
    where the agent can't click) restores a copy of each chosen one."""
    main, client = app_env
    main.notify = lambda *a, **k: None
    versions = [_v("a.txt", _t(1), _t(2)), _v("b.txt", _t(2), _t(3))]
    with client, FakeDaemon(respond_with=lambda b: _ok()) as sd:
        _, riley = _signup(client, "riley")
        device = _pair(client, riley, "rk-riley", "riley-mac", sd.url)
        uid = _user_id(main, "riley")
        w = World(main, monkeypatch, versions)

        def answering(reply):
            def call(config, action, **kw):
                if action == "mirror_restore_pick":
                    threading.Timer(0.2, lambda: client.post("/hosts/restore-picked", json={"request_id": kw["approval_id"], **reply(kw)}, headers=device)).start()
                    return _ok(opened=True)[1]
                return w.call(action, **kw)
            return call

        def pick_second(kw):
            assert len(kw["items"]) == 2 and "a.txt" in kw["items"][0] and "written" in kw["items"][0]
            return {"picked": [1]}

        monkeypatch.setattr(main.backups, "daemon_call", answering(pick_second))
        out = main._mcp_show_restore_picker(uid, "Documents")
        assert "b.txt" in out and "nothing was overwritten" in out and "a.txt" not in out
        assert client.post("/hosts/restore-picked", json={"request_id": "nope", "picked": [0]}, headers=device).status_code == 404
        monkeypatch.setattr(main.backups, "daemon_call", answering(lambda kw: {"cancelled": True}))
        assert "closed the list" in main._mcp_show_restore_picker(uid, "Documents")


def test_the_guides_inline_picker_lists_and_restores_on_the_persons_click(app_env, monkeypatch):
    main, client = app_env
    with client, FakeDaemon(respond_with=lambda b: _ok()) as sd:
        _, riley = _signup(client, "riley")
        _pair(client, riley, "rk-riley", "riley-mac", sd.url)
        rid = _user_id(main, "riley")
        with main.get_db() as db:
            db.execute("INSERT INTO mirror_folders (id, owner_user_id, owner_host_id, path, label, status) VALUES ('f1', ?, 1, '/Users/x/Documents', 'Documents', 'active')", (rid,))
        w = World(main, monkeypatch, [_v("gone.txt", _t(1), _t(2), exists_now=False), _v("here.txt", _t(1), _t(2))])
        assert client.get("/restore/picker").status_code == 401
        listed = client.get("/restore/picker", headers=riley).json()
        assert listed["folders"] == ["Documents"] and {v["name"] for v in listed["versions"]} == {"gone.txt", "here.txt"}
        only = client.get("/restore/picker?deleted_only=true", headers=riley).json()
        assert [v["name"] for v in only["versions"]] == ["gone.txt"] and only["versions"][0]["deleted"]
        res = client.post("/restore/pick", json={"folder": "Documents", "picks": [{"name": "gone.txt", "at": only["versions"][0]["at"]}]}, headers=riley).json()
        assert "nothing was overwritten" in res["message"] and w.did("mirror_restore_version_finish")
        assert client.post("/restore/pick", json={"folder": "Documents", "picks": []}, headers=riley).status_code == 400
        day = (datetime.now(PDT) - timedelta(days=1)).strftime("%Y-%m-%d")
        assert "as of" in client.post("/restore/pick", json={"folders": ["Documents"], "as_of": day}, headers=riley).json()["message"]
        # The result joins the conversation, so the guide knows what happened.
        assert "as of" in main.guide.visible(main.guide.load_history(rid))[-1]["text"]


def test_the_guide_draws_the_picker_and_both_surfaces_have_every_restore_tool(app_env):
    main, _ = app_env
    import guide
    import mcp_server

    tools = {"list_versions", "restore_version", "restore_as_of", "show_restore_picker"}
    assert tools <= {t["function"]["name"] for t in guide.TOOLS}
    assert all(getattr(main._mcp_services, t) is not None for t in tools)
    assert all(f"def {t}(" in inspect.getsource(mcp_server) for t in tools)

    class Model:
        n = 0

        def create(self, **kw):
            Model.n += 1
            if Model.n == 1:
                args = json.dumps({"folder": "Documents", "deleted_only": True})
                call = type("Call", (), {"id": "1", "function": type("F", (), {"name": "show_restore_picker", "arguments": args})()})()
                msg = type("M", (), {"content": "", "tool_calls": [call]})()
            else:
                msg = type("M", (), {"content": "Pick what to bring back.", "tool_calls": None})()
            return type("R", (), {"choices": [type("C", (), {"message": msg})()]})()

    client = type("Cl", (), {"chat": type("Ch", (), {"completions": Model()})()})()
    reply, messages = guide.run(client, "m", main._mcp_services, 1, [], "I deleted a file", "p")
    assert reply == "Pick what to bring back." and messages[-1]["_picker"] == {"folder": "Documents", "q": "", "deleted_only": True}
    shown = guide.visible(messages)
    assert shown[-1]["picker"]["deleted_only"] is True
    assert "picker" not in guide.visible(messages + [{"role": "assistant", "content": "later"}])[-2]  # only the latest one is live


def test_short_path_hides_the_username_on_mac_and_windows(app_env):
    main, _ = app_env

    assert main._short_path("/Users/sam/Casper Restores/Documents") == "~/Casper Restores/Documents"
    assert main._short_path(r"C:\Users\Sam\Casper Restores\Documents") == "~/Casper Restores/Documents"
    assert main._short_path(r"C:\Users\Sam\OneDrive\Documents (as of 2026-10-10)") == "~/OneDrive/Documents (as of 2026-10-10)"
    assert main._short_path("D:/Users/Sam") == "~"
    assert main._short_path("") == ""
