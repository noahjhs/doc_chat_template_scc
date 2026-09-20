"""Tests for harness/harness/cli.py -- the Typer CLI layer itself
(argument parsing, session/domain/chat-state file handling, prompting,
no-args-is-help) via typer.testing.CliRunner. tests/test_harness_flows.py
already covers harness/client.py end to end; this file is what's actually
new here -- the CLI layer had no automated coverage at all before this,
so real bugs found by hand this session (the `-dev`/`-d` short-option
collision, Typer 0.27 vendoring its own private Click exception classes)
went undetected until manual testing. Drives the exact same in-process
casper_service TestClient wiring test_harness_flows.py uses (client.set_client)
-- no real network, no live server."""

import json
import os
import sys
import tempfile

import pytest
import typer
from typer.testing import CliRunner

CASPER_SERVICE_DIR = os.path.join(os.path.dirname(__file__), "..", "casper_service")
DOMAIN = "test-auth.invalid"  # ignored entirely once client.set_client() is active

PASSWORD = "correct-horse-battery-staple"


@pytest.fixture()
def harness_env(tmp_path, monkeypatch):
    """Wires harness.cli/harness.client to an in-process casper_service
    TestClient (same mechanism tests/test_harness_flows.py uses) AND
    isolates every on-disk state file (session, known domains, chat
    state, readline history) into tmp_path, so tests never touch the
    real ~/.casper-harness. Yields (cli module, client module, CliRunner,
    casper_service's own main module -- for patching _get_openai_client)."""
    sys.path.insert(0, os.path.abspath(CASPER_SERVICE_DIR))
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["AUTH_DB_PATH"] = os.path.join(tmp, "users.db")
        os.environ["STORAGE_ROOT"] = os.path.join(tmp, "storage")
        for mod in ("main", "db", "models", "policy", "conversations"):
            sys.modules.pop(mod, None)
        import main as auth_main
        from fastapi.testclient import TestClient

        from harness import cli as harness_cli
        from harness import client as harness_client

        harness_client.set_client(TestClient(auth_main.app))
        monkeypatch.setattr(harness_cli, "SESSION_PATH", tmp_path / "session.json")
        monkeypatch.setattr(harness_cli, "KNOWN_DOMAINS_PATH", tmp_path / "known_domains.json")
        monkeypatch.setattr(harness_cli, "CHAT_STATE_PATH", tmp_path / "chat_state.json")
        monkeypatch.setattr(harness_cli, "HISTORY_PATH", tmp_path / "history")
        try:
            yield harness_cli, harness_client, CliRunner(), auth_main
        finally:
            harness_client.set_client(None)
    sys.path.remove(os.path.abspath(CASPER_SERVICE_DIR))
    for mod in ("main", "db", "models", "policy", "conversations"):
        sys.modules.pop(mod, None)


def _signup(cli, runner, username):
    result = runner.invoke(cli.app, ["signup", username, "--domain", DOMAIN, "--password", PASSWORD])
    assert result.exit_code == 0, result.output
    return result


class _FakeResponses:
    def __init__(self, texts):
        self._texts = list(texts)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        text = self._texts.pop(0) if self._texts else "ok"

        class _R:
            id = "resp"
            output_text = text
            output = []

        return _R()


class _FakeOpenAI:
    def __init__(self, texts):
        self.responses = _FakeResponses(texts)


# --- Regression: the real -dev/-d short-option collision found this session
def test_login_dash_dev_typo_is_rejected_not_silently_misparsed(harness_env):
    cli, _hc, runner, _main = harness_env
    result = runner.invoke(cli.app, ["login", "someone", "-dev", "--password", "x"])
    assert result.exit_code != 0
    assert "No such option: -d" in result.output


# --- _resolve_domain precedence (pure unit, no network/CLI involved)
def test_resolve_domain_precedence(harness_env, monkeypatch):
    cli, _hc, _runner, _main = harness_env
    monkeypatch.setattr(cli, "_load_known_domains", lambda: {"alice": "remembered.example"})

    assert cli._resolve_domain("alice", None, True, False) == cli.DEV_AUTH_DOMAIN
    assert cli._resolve_domain("alice", None, False, True) == cli.PROD_AUTH_DOMAIN
    assert cli._resolve_domain("alice", "explicit.example", False, False) == "explicit.example"
    # --dev/--prod win even over an explicit --domain.
    assert cli._resolve_domain("alice", "explicit.example", True, False) == cli.DEV_AUTH_DOMAIN
    assert cli._resolve_domain("alice", None, False, False) == "remembered.example"
    assert cli._resolve_domain("bob", None, False, False) == cli.DEFAULT_DOMAIN
    with pytest.raises(typer.Exit):
        cli._resolve_domain("alice", None, True, True)


# --- no_args_is_help -- representative sample, both directions
@pytest.mark.parametrize("args", [["hosts", "rename"], ["forget-password"], ["policy", "apply"], ["environment", "create"]])
def test_no_args_is_help_shows_full_help_not_a_bare_usage_error(harness_env, args):
    cli, _hc, runner, _main = harness_env
    result = runner.invoke(cli.app, args)
    assert "Usage:" in result.output
    assert "Arguments" in result.output or "--help" in result.output


@pytest.mark.parametrize("args", [["whoami"], ["hosts", "list"], ["policy", "list"], ["profile", "get"]])
def test_zero_arg_commands_run_normally_when_invoked_bare(harness_env, args):
    """These are meaningfully invokable with no arguments -- confirms
    no_args_is_help was applied selectively, not blanket. Not logged in
    here, so each fails on _require_session() -- the point is that it's
    THAT failure (exit 1, no session), not a help page."""
    cli, _hc, runner, _main = harness_env
    result = runner.invoke(cli.app, args)
    assert "Usage:" not in result.output
    assert result.exit_code == 1


# --- Non-interactive guard (login without --password must never hang)
def test_login_without_password_fails_fast_when_noninteractive(harness_env):
    cli, _hc, runner, _main = harness_env
    result = runner.invoke(cli.app, ["login", "someone"])
    assert result.exit_code == 1
    assert "Not running interactively" in result.output


# --- Session/known-domain persistence end to end
def test_signup_then_logout_then_login_remembers_domain(harness_env):
    cli, _hc, runner, _main = harness_env
    _signup(cli, runner, "clirunneruser")

    assert cli.SESSION_PATH.exists()
    session = json.loads(cli.SESSION_PATH.read_text())
    assert session["username"] == "clirunneruser"
    assert session["domain"] == DOMAIN
    known = json.loads(cli.KNOWN_DOMAINS_PATH.read_text())
    assert known["clirunneruser"] == DOMAIN

    logout_result = runner.invoke(cli.app, ["logout"])
    assert logout_result.exit_code == 0
    assert not cli.SESSION_PATH.exists()

    # No --domain/--dev/--prod this time -- resolved from known_domains.json.
    login_result = runner.invoke(cli.app, ["login", "clirunneruser", "--password", PASSWORD])
    assert login_result.exit_code == 0, login_result.output
    assert json.loads(cli.SESSION_PATH.read_text())["domain"] == DOMAIN


# --- Chat: empty line no longer ends the conversation
def test_chat_empty_line_does_not_end_conversation(harness_env):
    cli, _hc, runner, main = harness_env
    _signup(cli, runner, "chatuser1")
    fake = _FakeOpenAI(["hi there", "still here"])
    main._get_openai_client = lambda: fake

    result = runner.invoke(cli.app, ["chat", "--mock"], input="hello\n\nhello again\nexit\n")
    assert result.exit_code == 0, result.output
    assert len(fake.responses.calls) == 2  # the blank line never reached the API at all


# --- Chat: state persists across separate launches, --new starts fresh
def test_chat_resumes_across_separate_invocations_until_new(harness_env):
    cli, _hc, runner, main = harness_env
    _signup(cli, runner, "chatuser2")
    fake = _FakeOpenAI(["first reply"])
    main._get_openai_client = lambda: fake

    first = runner.invoke(cli.app, ["chat", "--mock"], input="hello\nexit\n")
    assert first.exit_code == 0, first.output
    assert cli.CHAT_STATE_PATH.exists()

    fake2 = _FakeOpenAI(["second reply"])
    main._get_openai_client = lambda: fake2
    second = runner.invoke(cli.app, ["chat", "--mock"], input="exit\n")
    assert "Resuming your previous conversation." in second.output

    fresh = runner.invoke(cli.app, ["chat", "--mock", "--new"], input="exit\n")
    assert "Resuming your previous conversation." not in fresh.output


def _pause_via_call_tool(cli, hc, domain, token, username, host_label="pausehost"):
    """Creates a real, server-side pending approval without needing a real
    OpenAI call (call_tool injects the tool call directly -- see
    conversations.py's new_turn_from_tool_call) -- used below to simulate
    exactly what chat_state.json would hold if `harness chat` itself had
    just paused and been killed before finishing the approval prompt (see
    the matching _save_chat_state call added right where chat detects a
    pause, before the -- possibly long-lived, possibly interrupted --
    resolution prompt). mock_tier="ask" is what actually produces the
    pause -- mocking never evaluates policy (see conversations.py's
    DispatchContext.mock_tier), so no real policy layer/rule needs
    authoring here at all; pairing + presence still matter, since host
    resolution (which host the pause is attributed to) is real."""
    pair = hc.pair_host(domain, token, f"rk-{username}", hostname=host_label)
    hc.report_host_presence(domain, pair["device_token"], f"https://relay.example/agent/{username}")
    paused = hc.call_tool(
        domain, token, "run_shell_command", {"positional_args": ["rm"]}, mock=True, mock_tier="ask", default_host=host_label
    )
    cli.CHAT_STATE_PATH.write_text(
        json.dumps({"domain": domain, "username": username, "host": host_label, "mock": True, "turn": paused["turn"]})
    )
    return paused["pending_approval"]["approval_id"]


# --- Regression: killing `chat` mid-approval-pause must not silently lose
# track of it -- the paused state has to survive on disk, and relaunching
# must surface it, not silently resume as if nothing happened.
def test_chat_resumes_into_a_pending_approval_left_by_a_prior_kill(harness_env):
    cli, hc, runner, main = harness_env
    # _get_openai_client() is evaluated eagerly as a plain argument to
    # run_turn(...) (see casper_service/main.py's _step_response) -- it must
    # return a usable client even for a call that pauses immediately and
    # never actually invokes .responses.create() on it, so this needs
    # mocking before ANY conversation_step call, not just an actual
    # resolution's own real follow-up hop.
    main._get_openai_client = lambda: _FakeOpenAI(["done"])
    _signup(cli, runner, "killeduser")
    session = json.loads(cli.SESSION_PATH.read_text())
    approval_id = _pause_via_call_tool(cli, hc, DOMAIN, session["token"], "killeduser")

    # CliRunner's own stdin is never a real tty (confirmed directly --
    # patching sys.stdin.isatty here has no effect, since CliRunner
    # substitutes its own stdin object during invoke()), so this can only
    # exercise the fail-clean, non-interactive path here, not a typed
    # "allow"/"deny" -- see harness/harness/cli.py's own
    # _require_flag_when_noninteractive for the identical constraint
    # elsewhere in this file. What actually matters, and what THIS
    # regression test is really about: no crash (a real KeyError was
    # found and fixed here -- the reconstructed result dict passed into
    # _resolve_pending_approval originally had no "turn" key), the
    # SAME approval is correctly identified by id, and it's left
    # genuinely still pending -- not lost, not duplicated.
    result = runner.invoke(cli.app, ["chat", "--mock"], input="exit\n")
    assert result.exit_code == 0, result.output
    assert "Picking up a pending approval from before" in result.output
    assert f"id={approval_id}" in result.output

    remaining = hc.list_pending_approvals(DOMAIN, session["token"])["pending_approvals"]
    assert [p["id"] for p in remaining] == [approval_id]


# --- The resolution mechanics chat's resume branch hands off to, tested
# directly (no tty simulation needed -- auto="allow" skips the prompt the
# same way call-tool's own --approve/--deny do): confirms the
# reconstructed result dict chat builds on resume (turn + a bare
# pending_approval.approval_id, not a full server response) is actually
# resolvable, start to finish, once a decision IS available.
def test_resolve_pending_approval_resumes_a_chat_reconstructed_result(harness_env):
    cli, hc, runner, main = harness_env
    main._get_openai_client = lambda: _FakeOpenAI(["done"])
    _signup(cli, runner, "resolveuser")
    session = json.loads(cli.SESSION_PATH.read_text())
    approval_id = _pause_via_call_tool(cli, hc, DOMAIN, session["token"], "resolveuser")
    turn = json.loads(cli.CHAT_STATE_PATH.read_text())["turn"]

    result = cli._resolve_pending_approval(
        {"turn": turn, "status": "pending_approval", "pending_approval": {"approval_id": approval_id}},
        DOMAIN,
        session["token"],
        "allow",
    )
    assert result["status"] == "done"
    calls = result["turn"]["aggregate"]["shell_command_calls"]
    assert len(calls) == 1
    assert json.loads(calls[0]["output"])["mock"] is True

    remaining = hc.list_pending_approvals(DOMAIN, session["token"])["pending_approvals"]
    assert remaining == []


def test_chat_notes_other_pending_approvals_from_a_different_source(harness_env):
    """An approval created via call-tool (or a different session) that
    this chat session never paused on -- must still be surfaced, not
    silently invisible."""
    cli, hc, runner, main = harness_env
    main._get_openai_client = lambda: _FakeOpenAI([])  # see the sibling test above for why this is needed even for a pure pause
    _signup(cli, runner, "otheruser")
    session = json.loads(cli.SESSION_PATH.read_text())
    pair = hc.pair_host(DOMAIN, session["token"], "rk-otheruser", hostname="otherhost")
    hc.report_host_presence(DOMAIN, pair["device_token"], "https://relay.example/agent/otheruser")
    hc.call_tool(
        DOMAIN, session["token"], "run_shell_command", {"positional_args": ["rm"]}, mock=True, mock_tier="ask", default_host="otherhost"
    )

    # No chat_state.json at all -- this session never touched `chat`.
    result = runner.invoke(cli.app, ["chat", "--mock"], input="exit\n")
    assert "You also have 1 other pending approval" in result.output


# --- Regression: Typer 0.27 vendors its own private Click exception
# classes -- an early version of _run_interactive's `except
# click.exceptions.ClickException` silently never matched, so an unknown
# command crashed the whole REPL instead of printing a friendly error.
def test_run_interactive_bogus_command_does_not_crash_the_repl(harness_env, monkeypatch, capsys):
    cli, _hc, runner, _main = harness_env
    _signup(cli, runner, "resilientuser")

    lines = iter(["bogus-command-xyz", "whoami", "exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(lines))

    cli._run_interactive()

    out = capsys.readouterr().out
    assert "resilientuser" in out  # whoami's own output -- proves the loop survived the bogus command
