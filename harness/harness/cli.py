"""Thin Typer CLI over harness/client.py -- session persistence, table/JSON
rendering (rich), and the interactive approve/deny + chat loops live here;
client.py itself stays a plain HTTP wrapper with no CLI dependency (see its
own module docstring). Verb naming follows the project's own settled
vocabulary (see the top-level plan): `eval` (pure rule matching, no
execution, no daemon) -> `call-tool [--mock]` (a live/mocked reproduction
of one tool call, via direct injection) -> `chat [--mock]` (a real
conversational turn, the model decides what to call) -- an escalating
ladder of what's actually exercised, "force"/"forced" language deliberately
retired throughout."""

import json
import os
import shlex
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import keyring
import typer
from rich.console import Console
from rich.table import Table

from . import client

# LocalAuthentication (Touch ID/password) is macOS-only -- pyobjc-framework-
# LocalAuthentication is a platform-conditional dependency (see
# pyproject.toml's `sys_platform == 'darwin'` marker), so this import must
# tolerate not existing at all on another platform. _authenticate_device_owner
# below degrades to "no biometric gate available" (never raises) either way.
try:
    from LocalAuthentication import LAContext, LAPolicyDeviceOwnerAuthenticationWithBiometrics
except ImportError:  # non-macOS, or the platform-conditional dep isn't installed
    LAContext = None

# Importing readline (before any input() call) is what makes input() support
# up/down-arrow history navigation and basic emacs-style line editing at
# all -- without it, input() still works, just with no history and minimal
# editing. Standard library on macOS/Linux (backed by libedit or GNU
# readline); not available on Windows without the separate pyreadline3
# package, so _run_interactive below tolerates this being None (no history
# navigation there, same as today, never a crash).
try:
    import readline
except ImportError:  # Windows without pyreadline3
    readline = None

app = typer.Typer(help="Casper backend test harness -- drives auth_service directly, no GUI in the loop.")

# Split into two --help panels, per the project's own established framing
# (see this module's own docstring): USER_PANEL is this CLI mocking the
# eventual real front end -- signup/login/logout/hosts/Environments/
# profile were built as explicit parity with what the old authenticated
# Streamlit pages did (see CLAUDE.md), the same actions a real end user
# takes once a real UI exists. DEV_PANEL is everything that tests the
# backend's own rule engine directly (eval -> call-tool -> chat's own
# "escalating ladder of what's actually exercised", policy authoring) or
# simulates a daemon/native-dialog that isn't really there (pair,
# pair-daemon, report-presence, attend, respond-approvals) -- things a
# real end user never does; only someone testing this project does.
USER_PANEL = "User-facing (mocks the eventual front end)"
DEV_PANEL = "Developer-facing (tests the backend directly)"

policy_app = typer.Typer(help="Manage policy layers.")
app.add_typer(policy_app, name="policy", rich_help_panel=DEV_PANEL)
hosts_app = typer.Typer(help="Manage known hosts.")
app.add_typer(hosts_app, name="hosts", rich_help_panel=USER_PANEL)
environment_app = typer.Typer(help="Manage Environments.")
app.add_typer(environment_app, name="environment", rich_help_panel=USER_PANEL)
profile_app = typer.Typer(help="Manage account profile/notification/permission preferences.")
app.add_typer(profile_app, name="profile", rich_help_panel=USER_PANEL)

console = Console()
err_console = Console(stderr=True)

DEFAULT_DOMAIN = "localhost:8100"
# This project's two actual deployed auth domains -- mirrors
# scripts/smoke_test.sh's own AUTH_DOMAIN defaults for dev/prod. --dev/--prod
# (see _resolve_domain) are just a memorable shorthand for these, so a
# caller never has to remember or retype the real hostname.
DEV_AUTH_DOMAIN = "dev-auth.casperagent.dev"
PROD_AUTH_DOMAIN = "auth.casperagent.dev"
SESSION_PATH = Path(os.environ.get("CASPER_HARNESS_SESSION", str(Path.home() / ".casper-harness" / "session.json")))
# Remembers, per username, which domain was last used to log in as it --
# separate from SESSION_PATH (which holds only the one CURRENTLY active
# session) since this needs to persist across switching users/domains, not
# just describe the current one. Keyed by username alone (not
# domain+username) -- deliberately: the whole point is "what domain does
# this username belong to", so it's a username -> domain map, not the other
# way around.
KNOWN_DOMAINS_PATH = SESSION_PATH.parent / "known_domains.json"
# Remembers the in-progress `harness chat` conversation (the `turn` state
# conversations.py's stateless POST /conversations/step hands back and
# expects back on the next call -- see its own module docstring) across
# separate `harness` launches, same "don't make me retype/re-establish
# things" spirit as everything else here. Scoped to the (domain, username)
# it was saved under, checked on load -- switching accounts should never
# resume a stranger's conversation.
CHAT_STATE_PATH = SESSION_PATH.parent / "chat_state.json"
# Interactive-mode command history (see _run_interactive) -- persisted
# across separate `harness` launches, same "don't make me retype things"
# spirit as everything else here, not just scrollable within one session.
HISTORY_PATH = SESSION_PATH.parent / "history"
HISTORY_MAX_ENTRIES = 1000

# The OS keychain service name every saved password is filed under (see
# keyring's own docs -- it namespaces by (service, username) pairs).
# _keyring_key folds domain into keyring's "username" field since the same
# account username can plausibly exist on both dev and prod as unrelated
# accounts.
KEYRING_SERVICE = "casper-harness"


def _keyring_key(domain: str, username: str) -> str:
    return f"{domain}:{username}"


def _load_session() -> Optional[dict]:
    if not SESSION_PATH.exists():
        return None
    return json.loads(SESSION_PATH.read_text())


def _save_session(domain: str, username: str, token: str) -> None:
    SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    SESSION_PATH.write_text(json.dumps({"domain": domain, "username": username, "token": token}))


def _require_session() -> tuple[str, str]:
    session = _load_session()
    if session is None:
        err_console.print("[red]Not logged in.[/red] Run `harness login` or `harness signup` first.")
        raise typer.Exit(code=1)
    return session["domain"], session["token"]


def _handle_api_error(e: client.ApiError):
    err_console.print(f"[red]Error {e.status_code}:[/red] {e.detail}")
    raise typer.Exit(code=1)


def _require_flag_when_noninteractive(flag: str) -> None:
    """Guards an interactive prompt (the username argument, or --password
    -- and, transitively, login's own Touch ID gate that only runs once a
    username is in hand -- see _authenticate_device_owner) against an
    automated/scripted caller that omitted it: without this, a
    non-interactive run wouldn't just fail cleanly, it would either hang
    forever on typer.prompt (no human to type anything) or -- worse, for a
    saved password specifically -- hang forever on a REAL system Touch
    ID/password dialog nobody is present to answer, on any machine that
    happens to have an active GUI session (the mini, notably, which runs
    unattended). sys.stdin.isatty() is the standard, simple way to tell "a
    human is plausibly at a real terminal" apart from
    "piped/redirected/scripted" -- not foolproof (a script could itself
    run inside a real pty), but exactly right for this: the one thing that
    actually matters is whether there's a reasonable expectation of
    headless/automated use, and passing the value explicitly is always
    the correct, hang-free way to drive that regardless of this heuristic
    either way."""
    if not sys.stdin.isatty():
        err_console.print(
            f"[red]Not running interactively -- pass {flag} explicitly[/red] "
            "(required for scripted/automated use; omitting it here would otherwise hang)."
        )
        raise typer.Exit(code=1)


def _authenticate_device_owner(reason: str) -> bool:
    """Prompts the OS's own Touch ID/password dialog (LocalAuthentication --
    the same framework Safari/Chrome gate password-autofill behind) and
    blocks until the user responds. This is a deliberate step up from
    keyring's own default (silent once the requesting process is trusted,
    no prompt of any kind) -- part of this CLI's job is to mirror the
    experience the eventual front end will have, not just work functionally
    (see this module's own docstring), so a saved password should feel like
    a browser's saved password, not a bypass.

    Uses LAPolicyDeviceOwnerAuthenticationWithBiometrics, NOT the plain
    LAPolicyDeviceOwnerAuthentication this started out with -- switched
    after a real, reported false pass: the plain policy has a documented
    macOS behavior where it can succeed silently, with no prompt shown at
    all, whenever the Mac is already unlocked and actively in use (it
    treats "you're already sitting at an unlocked session" as sufficient
    proof of device-owner presence on its own) -- which is true for
    essentially every real terminal session, so it was never actually
    gating anything in practice. The Biometrics-only variant has no such
    session-already-unlocked shortcut; it always requires either a fresh
    Touch ID read or its own "Enter Password" fallback button, both inside
    the same real, visible system sheet -- so the fallback a browser's own
    Touch ID prompt gives you is still there, just genuinely gated behind
    that visible sheet rather than an invisible one this project's own
    password could bypass entirely. Returns False (never raises) on any
    failure, cancellation, or unavailability (including simply not being
    on macOS, no biometry enrolled at all, or the platform-conditional
    pyobjc dependency not being installed -- see LAContext's own import
    above) -- callers fall back to an interactive password prompt for the
    saved account itself, exactly like declining a browser's Touch ID
    dialog still lets you type the password by hand."""
    if LAContext is None:
        return False
    context = LAContext.alloc().init()
    can_evaluate, _err = context.canEvaluatePolicy_error_(LAPolicyDeviceOwnerAuthenticationWithBiometrics, None)
    if not can_evaluate:
        return False

    done = threading.Event()
    outcome = {"success": False}

    def _reply(success, _error):
        outcome["success"] = bool(success)
        done.set()

    context.evaluatePolicy_localizedReason_reply_(LAPolicyDeviceOwnerAuthenticationWithBiometrics, reason, _reply)
    done.wait()
    return outcome["success"]


def _maybe_save_password(key: str, password: str) -> None:
    """Mirrors a browser's own "save this password?" prompt -- asks before
    writing a password to the keychain. Only ever called (see signup/login)
    for a password a human just typed into an interactive prompt -- never
    for one reused from the keychain (nothing new to save then), and never
    for one supplied via --password (a scripted/automated caller isn't the
    thing a browser's own save-prompt reacts to; that path doesn't call
    this at all, so a --password run is never saved and never asks)."""
    if typer.confirm("Save this password to the keychain for next time?", default=True):
        keyring.set_password(KEYRING_SERVICE, key, password)
    else:
        console.print("[dim]Not saved.[/dim]")


def _load_known_domains() -> dict:
    if not KNOWN_DOMAINS_PATH.exists():
        return {}
    return json.loads(KNOWN_DOMAINS_PATH.read_text())


def _remember_domain(username: str, domain: str) -> None:
    known = _load_known_domains()
    known[username] = domain
    KNOWN_DOMAINS_PATH.parent.mkdir(parents=True, exist_ok=True)
    KNOWN_DOMAINS_PATH.write_text(json.dumps(known))


def _load_chat_state(domain: str, username: str) -> Optional[dict]:
    """Returns the saved `turn`/`host`/`mock` for THIS (domain, username)
    only -- None for a missing file, a different account's saved state, or
    a corrupt file (treated the same as "nothing saved yet")."""
    if not CHAT_STATE_PATH.exists():
        return None
    try:
        saved = json.loads(CHAT_STATE_PATH.read_text())
    except ValueError:
        return None
    if saved.get("domain") != domain or saved.get("username") != username:
        return None
    return saved


def _save_chat_state(domain: str, username: str, host: Optional[str], mock: bool, turn: dict) -> None:
    CHAT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CHAT_STATE_PATH.write_text(json.dumps({"domain": domain, "username": username, "host": host, "mock": mock, "turn": turn}))


def _clear_chat_state() -> None:
    CHAT_STATE_PATH.unlink(missing_ok=True)


def _resolve_domain(username: str, domain: Optional[str], dev: bool, prod: bool) -> str:
    """--dev/--prod win whenever given. Otherwise an explicit --domain (or
    CASPER_HARNESS_DOMAIN -- typer's envvar handling can't tell those two
    apart, so this doesn't try to) wins next. Only when NONE of those three
    were given does this fall back to whatever domain `username` last
    logged into (see _remember_domain), then finally DEFAULT_DOMAIN if
    even that's unknown."""
    if dev and prod:
        err_console.print("[red]--dev and --prod are mutually exclusive.[/red]")
        raise typer.Exit(code=1)
    if dev:
        return DEV_AUTH_DOMAIN
    if prod:
        return PROD_AUTH_DOMAIN
    if domain is not None:
        return domain
    return _load_known_domains().get(username, DEFAULT_DOMAIN)


def _run_interactive() -> None:
    """A REPL over this same Typer app -- each line typed is exactly the
    argument list that would otherwise follow `harness` on a real command
    line (so e.g. `login demo --dev` here is exactly `harness login demo
    --dev` from a shell), dispatched via Click's own documented embedding
    mechanism (standalone_mode=False -- see Click's docs on "using Click
    in a REPL"): app(...) then returns/raises instead of calling
    sys.exit() itself, so one bad/failing command doesn't kill the whole
    session. A plain `typer.Exit` (e.g. from _handle_api_error) is
    swallowed by Click itself in this mode (returned as an exit code,
    never raised).

    typer.Abort (Ctrl-C mid password-prompt) is caught by class -- it's
    Typer's own public, stable exception. A genuine usage error (unknown
    command, bad option, ...) is NOT caught by class: this Typer version
    (0.27) fully vendors its OWN private copy of Click's exceptions
    (typer._click.exceptions), entirely unrelated to the top-level click
    package's classes -- confirmed directly (isinstance/issubclass both
    False against click.exceptions.ClickException) -- so depending on
    click.exceptions.ClickException here silently never matches, and
    depending on typer._click.exceptions.ClickException instead is
    depending on a private, unstable-across-versions module path. Duck-
    typing on the one thing every one of these actually guarantees (a
    .show() method, per Click's own ClickException API contract, which
    this vendored fork faithfully replicates) is what's actually robust
    here -- anything else unexpected still gets printed (as
    "ExceptionType: message"), never silently swallowed, just without the
    same formatted "Usage: ..." presentation.

    Up/down-arrow history navigation comes from readline itself (see this
    module's own import of it) -- input() automatically records each line
    into readline's history the moment readline is imported, with no
    add_history() call needed here; what this function adds on top is
    just PERSISTENCE across separate `harness` launches (read the saved
    file in before the loop, write it back out after) -- the same
    "don't make me retype things" spirit as the remembered domain/
    keychain password elsewhere in this file. Silently does neither if
    readline isn't available (Windows without pyreadline3) -- history
    navigation itself just doesn't work there, same as it wouldn't with a
    bare `input()` and no readline at all.

    Requires an existing session -- see main()'s own callback, which gates
    entry to this function on _require_session() before ever calling it,
    so this itself doesn't need to check. Logging out from inside the
    loop (`logout`, which clears SESSION_PATH -- see its own command) ends
    the session the same way `exit`/Ctrl-D does, checked generically after
    every dispatched command (whether the session file still exists, not
    by matching the literal text "logout") so this stays correct if some
    other future command ever clears it too. The reverse isn't true:
    exiting the REPL (`exit`/`quit`/Ctrl-D) never touches the session --
    only an explicit `logout` does."""
    if readline is not None:
        readline.set_history_length(HISTORY_MAX_ENTRIES)
        try:
            readline.read_history_file(HISTORY_PATH)
        except FileNotFoundError:
            pass
        except OSError as e:
            err_console.print(f"[dim]Couldn't load command history: {e}[/dim]")

    console.print("Casper harness -- interactive mode. Type a command (no leading 'harness'), or 'exit'/Ctrl-D to quit.")
    try:
        while True:
            try:
                line = input("harness> ")
            except EOFError:
                console.print()
                break
            except KeyboardInterrupt:
                console.print()
                continue
            line = line.strip()
            if not line:
                continue
            if line in ("exit", "quit"):
                break
            try:
                args = shlex.split(line)
            except ValueError as e:
                err_console.print(f"[red]{e}[/red]")
                continue
            try:
                app(args=args, prog_name="harness", standalone_mode=False)
            except typer.Abort:
                err_console.print("Aborted.")
            except KeyboardInterrupt:
                err_console.print()
            except Exception as e:
                if callable(getattr(e, "show", None)):
                    e.show()
                else:
                    err_console.print(f"[red]{type(e).__name__}:[/red] {e}")
            if _load_session() is None:
                console.print("Logged out -- leaving interactive mode.")
                break
    finally:
        if readline is not None:
            HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
            try:
                readline.write_history_file(HISTORY_PATH)
            except OSError as e:
                err_console.print(f"[dim]Couldn't save command history: {e}[/dim]")


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    debug: bool = typer.Option(False, "--debug", "--raw", help="Print every request/response."),
):
    """Casper backend test harness. Run with no command for interactive
    mode. If not already logged in, prompts for a username and logs in
    first -- exactly the same flow as `harness login` with no other flags
    (see _do_login), never a special case of its own -- then enters
    interactive mode; `harness login`/`signup` themselves still never
    enter interactive mode on their own, whether run here or from inside
    it."""
    client.toggle_debug(debug)
    if ctx.invoked_subcommand is None:
        if _load_session() is None:
            _require_flag_when_noninteractive("`harness login <username> --password ...`")
            username = typer.prompt("Username")
            _do_login(username, None, False, False, None)
        _run_interactive()


@app.command(rich_help_panel=USER_PANEL, short_help="Create a new account and save the resulting session.")
def signup(
    username: str,
    domain: Optional[str] = typer.Option(None, "--domain", envvar="CASPER_HARNESS_DOMAIN"),
    dev: bool = typer.Option(False, "--dev", help=f"Use the dev deployment ({DEV_AUTH_DOMAIN})."),
    prod: bool = typer.Option(False, "--prod", help=f"Use the prod deployment ({PROD_AUTH_DOMAIN})."),
    password: Optional[str] = typer.Option(None, "--password", hide_input=True, help="Omit to be prompted (with confirmation)."),
):
    """Create a new account and save the resulting session. When the
    password is typed interactively (not supplied via --password), asks
    before saving it to the OS keychain -- same as a browser's own
    save-password prompt. --password is required for scripted/automated
    use (see _require_flag_when_noninteractive)."""
    domain = _resolve_domain(username, domain, dev, prod)
    typed_fresh = password is None
    if password is None:
        _require_flag_when_noninteractive("--password")
        password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)
    try:
        result = client.signup(domain, username, password)
    except client.ApiError as e:
        _handle_api_error(e)
    if typed_fresh:
        _maybe_save_password(_keyring_key(domain, result["username"]), password)
    _remember_domain(result["username"], domain)
    _save_session(domain, result["username"], result["token"])
    console.print(f"[green]Signed up as {result['username']}.[/green] Session saved to {SESSION_PATH}.")


def _do_login(username: str, domain: Optional[str], dev: bool, prod: bool, password: Optional[str]) -> None:
    """Everything `login` does once it already has a username in hand --
    factored out so main()'s own not-logged-in fallback (see its own
    callback below) can drive the exact same flow inline (after prompting
    for a username itself) rather than duplicating it."""
    domain = _resolve_domain(username, domain, dev, prod)
    key = _keyring_key(domain, username)

    if password is None:
        _require_flag_when_noninteractive("--password")  # only raises if non-interactive -- see its own docstring

    from_keychain = False
    typed_fresh = False
    if password is None:
        stored = keyring.get_password(KEYRING_SERVICE, key)
        if stored is not None:
            if _authenticate_device_owner(f"unlock the saved Casper harness password for {username}@{domain}"):
                password = stored
                from_keychain = True
            else:
                err_console.print("[dim]Touch ID/password check declined or unavailable -- type it instead.[/dim]")
    if password is None:
        password = typer.prompt("Password", hide_input=True)
        typed_fresh = True

    try:
        result = client.signin(domain, username, password)
    except client.ApiError as e:
        if not (from_keychain and e.status_code == 401):
            _handle_api_error(e)
        # The saved password no longer works (changed/rotated elsewhere) --
        # drop it rather than keep silently failing with it, and fall back
        # to one interactive prompt.
        keyring.delete_password(KEYRING_SERVICE, key)
        err_console.print("[yellow]Saved password didn't work -- it's been forgotten.[/yellow]")
        password = typer.prompt("Password", hide_input=True)
        typed_fresh = True
        try:
            result = client.signin(domain, username, password)
        except client.ApiError as e2:
            _handle_api_error(e2)

    if typed_fresh:
        _maybe_save_password(key, password)
    _remember_domain(result["username"], domain)
    _save_session(domain, result["username"], result["token"])
    console.print(f"[green]Signed in as {result['username']}.[/green] Session saved to {SESSION_PATH}.")


@app.command(rich_help_panel=USER_PANEL, short_help="Sign in and save the resulting session.")
def login(
    username: Optional[str] = typer.Argument(None, help="Omit to be prompted."),
    domain: Optional[str] = typer.Option(None, "--domain", envvar="CASPER_HARNESS_DOMAIN"),
    dev: bool = typer.Option(False, "--dev", help=f"Use the dev deployment ({DEV_AUTH_DOMAIN})."),
    prod: bool = typer.Option(False, "--prod", help=f"Use the prod deployment ({PROD_AUTH_DOMAIN})."),
    password: Optional[str] = typer.Option(
        None, "--password", hide_input=True, help="Omit to use a saved OS-keychain password, or be prompted."
    ),
):
    """Sign in and save the resulting session. Without --domain/--dev/--prod,
    reuses whichever domain this username last logged into. Without
    --password, reuses a password already saved in the OS keychain for this
    domain+username -- gated behind a Touch ID/password check each time
    (see `harness forget-password` to clear it), same as a browser's own
    saved-password autofill -- falling back to an interactive prompt the
    first time, if Touch ID is declined/unavailable, or if the saved
    password stops working. A freshly-typed password that signs in
    successfully is offered to be saved (see _maybe_save_password) rather
    than saved automatically -- same as a browser's own save-password
    prompt. The username argument and --password are both required for
    scripted/automated use (see _require_flag_when_noninteractive) --
    checked as each is actually needed, not both up front, so a script
    that already passes username positionally only needs --password too,
    never a redundant flag for the one it already gave inline."""
    if username is None:
        _require_flag_when_noninteractive("the username argument")
        username = typer.prompt("Username")
    _do_login(username, domain, dev, prod, password)


@app.command("forget-password", rich_help_panel=USER_PANEL, short_help="Clear a saved OS-keychain password.", no_args_is_help=True)
def forget_password(
    username: str,
    domain: Optional[str] = typer.Option(None, "--domain", envvar="CASPER_HARNESS_DOMAIN"),
    dev: bool = typer.Option(False, "--dev", help=f"Use the dev deployment ({DEV_AUTH_DOMAIN})."),
    prod: bool = typer.Option(False, "--prod", help=f"Use the prod deployment ({PROD_AUTH_DOMAIN})."),
):
    """Clear a password saved in the OS keychain by a previous `login` --
    does not touch the current session (see `logout` for that)."""
    domain = _resolve_domain(username, domain, dev, prod)
    try:
        keyring.delete_password(KEYRING_SERVICE, _keyring_key(domain, username))
    except keyring.errors.PasswordDeleteError:
        console.print(f"No saved password for {username}@{domain}.")
        return
    console.print(f"[green]Forgot the saved password for {username}@{domain}.[/green]")


@app.command(rich_help_panel=USER_PANEL)
def whoami():
    """Show the currently saved session, if any."""
    session = _load_session()
    if session is None:
        console.print("Not logged in.")
        raise typer.Exit(code=1)
    console.print(f"{session['username']} @ {session['domain']}")


@app.command(rich_help_panel=USER_PANEL, short_help="Discard the saved session.")
def logout():
    """Discard the saved session -- leaves any OS-keychain-saved password
    (see `login`) and remembered domain (see `login`'s own docstring)
    alone, so the next `login` for this username still skips both prompts.
    Use `forget-password` to also clear the saved password."""
    if SESSION_PATH.exists():
        SESSION_PATH.unlink()
    console.print("Logged out.")


@hosts_app.command("list")
def hosts_list():
    """List the caller's own known hosts."""
    domain, token = _require_session()
    try:
        result = client.list_hosts(domain, token)
    except client.ApiError as e:
        _handle_api_error(e)
    table = Table("id", "label", "connected", "cwd")
    for h in result["hosts"]:
        table.add_row(str(h["host_id"]), h["label"], str(h["connected"]), h["cwd"])
    console.print(table)


@hosts_app.command("rename", no_args_is_help=True)
def hosts_rename(host: str, label: str):
    """Rename one of the caller's own hosts (by current label or id)."""
    domain, token = _require_session()
    try:
        host_id = _resolve_host_id(domain, token, host)
        client.rename_host(domain, token, host_id, label)
    except client.ApiError as e:
        _handle_api_error(e)
    console.print("[green]Renamed.[/green]")


@hosts_app.command("forget", no_args_is_help=True)
def hosts_forget(host: str):
    """Forget one of the caller's own hosts (by label or id) -- removes it
    from every Environment too."""
    domain, token = _require_session()
    try:
        host_id = _resolve_host_id(domain, token, host)
        client.forget_host(domain, token, host_id)
    except client.ApiError as e:
        _handle_api_error(e)
    console.print("[green]Forgotten.[/green]")


def _agent_config_dir(domain: str) -> Path:
    """Where a real Casper.app on THIS machine keeps its own state for the
    given auth domain -- mirrors agent/internal/config/appdir.go's
    appConfigSubdir() (duplicated, not imported -- independent Go/Python
    processes, same convention already used for pair_url_scheme/
    _pair_url_scheme elsewhere in this project). Only meaningful because
    `pair-daemon` itself is documented macOS-only and same-machine-only."""
    subdir = "Casper-dev" if domain.strip().lower().startswith("dev-") else "Casper"
    return Path.home() / "Library" / "Application Support" / subdir


def _read_pairing_status(domain: str) -> Optional[dict]:
    """Best-effort read of the real daemon's own record of its last
    casper://pair attempt (see agent/internal/config/pairing_status.go) --
    None if the file's missing/corrupt/unreadable, exactly like the daemon's
    own LoadLastPairingResult tolerates the same on its side."""
    try:
        return json.loads((_agent_config_dir(domain) / "pairing_status.json").read_text())
    except (OSError, ValueError):
        return None


@app.command("pair-daemon", rich_help_panel=DEV_PANEL, short_help="Pair a REAL Casper.app on this machine (manual verification only).")
def pair_daemon(
    timeout: float = typer.Option(15.0, "--timeout", help="Seconds to wait for the host to appear connected."),
):
    """Pair a REAL, already-installed Casper.app on this machine -- fires
    the same casper://pair hand-off a browser sign-in click would (see
    client.trigger_real_pairing), then polls GET /hosts until a newly-
    connected host shows up. macOS only. Not automatable in CI -- a manual
    verification tool, same posture as a real (non-mock) `harness chat`
    run: use this after any change that could affect pairing, not as part
    of routine automated testing (use `pair`, below, for that -- a fake
    host needs no real daemon at all)."""
    session = _load_session()
    if session is None:
        err_console.print("[red]Not logged in.[/red] Run `harness login` or `harness signup` first.")
        raise typer.Exit(code=1)
    domain, token, username = session["domain"], session["token"], session["username"]
    fired_at = time.time()
    try:
        before = {h["host_id"] for h in client.list_hosts(domain, token)["hosts"] if h["connected"]}
        client.trigger_real_pairing(domain, username, token)
    except client.ApiError as e:
        _handle_api_error(e)
    except RuntimeError as e:
        err_console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1) from e

    console.print("Pairing dispatched -- waiting for a newly-connected host...")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # The daemon's own record of what just happened is definitive --
        # check it before (or instead of) waiting out the full timeout on
        # a pairing-exchange error (e.g. an unreachable auth service) a
        # native dialog would otherwise be the only place to see (see
        # agent/cmd/casper/daemon.go's handlePairURL). A daemon can be
        # paired to more than one Casper account at once now, so pairing a
        # machine that's already paired to a different account is no
        # longer an error case here.
        status = _read_pairing_status(domain)
        if status and status.get("result") != "ok":
            try:
                fresh = datetime.fromisoformat(status["at"]).timestamp() >= fired_at
            except (KeyError, ValueError):
                fresh = False
            if fresh:
                err_console.print(f"[red]{status.get('message', 'Pairing failed.')}[/red]")
                raise typer.Exit(code=1)
        try:
            hosts = client.list_hosts(domain, token)["hosts"]
        except client.ApiError as e:
            _handle_api_error(e)
        newly_connected = [h for h in hosts if h["connected"] and h["host_id"] not in before]
        if newly_connected:
            console.print("[green]Paired.[/green]")
            for h in newly_connected:
                console.print(f"  {h['label']} (id={h['host_id']})")
            return
        time.sleep(1)
    err_console.print(
        "[red]Timed out waiting for a newly-connected host.[/red] Confirm Casper.app is installed "
        "and registered for casper://, and try again."
    )
    raise typer.Exit(code=1)


@app.command(rich_help_panel=DEV_PANEL, short_help="Pair a fake/test host under a routing_key -- no real daemon needed.", no_args_is_help=True)
def pair(
    routing_key: str,
    hostname: Optional[str] = typer.Option(None, "--hostname"),
    label: Optional[str] = typer.Option(None, "--label"),
):
    """Pair a (possibly fake/test) host under a routing_key -- a real
    daemon does this itself; useful here for standing up a test host with
    no live daemon at all, e.g. to exercise policy layer attachment/eval."""
    domain, token = _require_session()
    try:
        result = client.pair_host(domain, token, routing_key, hostname, label)
    except client.ApiError as e:
        _handle_api_error(e)
    console.print(result)


@app.command("report-presence", rich_help_panel=DEV_PANEL, short_help="Make a paired test host show up as connected.", no_args_is_help=True)
def report_presence(
    device_token: str,
    local_agent_url: str = typer.Option(..., "--url"),
    cwd: str = typer.Option("", "--cwd", help="The host's own confined directory."),
):
    """Make a paired test host "connected" (see `pair`), with the given
    cwd -- lets a test host show up as connected (and its cwd
    displayed) with no real machine involved."""
    domain, _token = _require_session()
    try:
        result = client.report_host_presence(domain, device_token, local_agent_url, cwd)
    except client.ApiError as e:
        _handle_api_error(e)
    console.print(result)


@app.command(rich_help_panel=DEV_PANEL, short_help="Set which host receives native-dialog approval prompts.")
def attend(
    host: Optional[str] = typer.Argument(None, help="A host label or id -- omit with --clear to unset."),
    clear: bool = typer.Option(False, "--clear", help="Clear the attended host instead of setting one."),
):
    """Set (or clear) which of the caller's own hosts is "attended" --
    where an "ask"-tier approval's native-dialog prompt would go. Works
    the same whether `host` is a real daemon or a `pair`-faked one; see
    `respond-approvals` for standing in for the dialog itself."""
    domain, token = _require_session()
    try:
        if clear:
            client.clear_attended_host(domain, token)
            console.print("[green]Cleared.[/green]")
            return
        if host is None:
            result = client.get_attended_host(domain, token)
            console.print(result)
            return
        host_id = _resolve_host_id(domain, token, host)
        result = client.set_attended_host(domain, token, host_id)
    except client.ApiError as e:
        _handle_api_error(e)
    console.print(result)


@app.command("respond-approvals", rich_help_panel=DEV_PANEL, short_help="Stand in for the native-dialog approval relay.", no_args_is_help=True)
def respond_approvals(
    device_token: str,
    approve: bool = typer.Option(False, "--approve", help="Auto-approve every pending approval."),
    deny: bool = typer.Option(False, "--deny", help="Auto-deny every pending approval."),
    once: bool = typer.Option(False, "--once", help="Answer at most one approval, then exit."),
):
    """Stands in for the native-dialog relay entirely -- polls
    GET /hosts/pending-approvals with device_token (the same long-poll a
    real daemon's own dialog.go loop runs) and answers each one, either
    automatically (--approve/--deny) or by prompting interactively (the
    default). device_token belongs to whichever host `attend` designated
    -- real or `pair`-faked; see auth_service/main.py's own
    list_pending_approvals_for_attended_host for why there's no
    requirement that this be the daemon actually dispatching the call.
    Runs until interrupted (Ctrl-C) unless --once."""
    if approve and deny:
        err_console.print("[red]Specify at most one of --approve/--deny.[/red]")
        raise typer.Exit(code=1)
    domain, _token = _require_session()
    console.print("Waiting for pending approvals (Ctrl-C to stop)...")
    try:
        while True:
            result = client.list_pending_approvals(domain, device_token)
            for pending in result.get("pending_approvals", []):
                if pending.get("decision") is not None:
                    continue
                console.print(
                    f"[yellow]Approval requested[/yellow] on {pending['host_label']}: "
                    f"{pending['binary']} {pending['args']}".strip()
                )
                if approve:
                    decision = "allow"
                elif deny:
                    decision = "deny"
                else:
                    decision = typer.prompt("Approve? [allow/deny]", default="allow")
                client.decide_pending_approval(domain, device_token, pending["id"], decision)
                console.print(f"[green]{decision}.[/green]")
                if once:
                    return
    except client.ApiError as e:
        _handle_api_error(e)
    except KeyboardInterrupt:
        console.print("\nStopped.")


@environment_app.command("list")
def environment_list():
    domain, token = _require_session()
    try:
        result = client.list_environments(domain, token)
    except client.ApiError as e:
        _handle_api_error(e)
    table = Table("id", "name", "host_ids")
    for env in result["environments"]:
        table.add_row(str(env["id"]), env["name"], ", ".join(str(h) for h in env["host_ids"]))
    console.print(table)


@environment_app.command("create", no_args_is_help=True)
def environment_create(name: str):
    domain, token = _require_session()
    try:
        result = client.create_environment(domain, token, name)
    except client.ApiError as e:
        _handle_api_error(e)
    console.print(result)


def _resolve_environment_id(domain: str, token: str, name_or_id: str) -> int:
    if name_or_id.isdigit():
        return int(name_or_id)
    environments = client.list_environments(domain, token)["environments"]
    match = next((e for e in environments if e["name"] == name_or_id), None)
    if match is None:
        err_console.print(f"[red]No Environment named {name_or_id!r}.[/red]")
        raise typer.Exit(code=1)
    return match["id"]


@environment_app.command("rename", no_args_is_help=True)
def environment_rename(environment: str, name: str):
    domain, token = _require_session()
    try:
        environment_id = _resolve_environment_id(domain, token, environment)
        result = client.rename_environment(domain, token, environment_id, name)
    except client.ApiError as e:
        _handle_api_error(e)
    console.print(result)


@environment_app.command("delete", no_args_is_help=True)
def environment_delete(environment: str):
    domain, token = _require_session()
    try:
        environment_id = _resolve_environment_id(domain, token, environment)
        client.delete_environment(domain, token, environment_id)
    except client.ApiError as e:
        _handle_api_error(e)
    console.print("[green]Deleted.[/green]")


@environment_app.command("attach", no_args_is_help=True)
def environment_attach(environment: str, host: str):
    """Add a host (by label or id) to an Environment (by name or id)."""
    domain, token = _require_session()
    try:
        environment_id = _resolve_environment_id(domain, token, environment)
        host_id = _resolve_host_id(domain, token, host)
        result = client.add_host_to_environment(domain, token, environment_id, host_id)
    except client.ApiError as e:
        _handle_api_error(e)
    console.print(result)


@environment_app.command("detach", no_args_is_help=True)
def environment_detach(environment: str, host: str):
    """Remove a host (by label or id) from an Environment (by name or id)."""
    domain, token = _require_session()
    try:
        environment_id = _resolve_environment_id(domain, token, environment)
        host_id = _resolve_host_id(domain, token, host)
        result = client.remove_host_from_environment(domain, token, environment_id, host_id)
    except client.ApiError as e:
        _handle_api_error(e)
    console.print(result)


@profile_app.command("get")
def profile_get():
    domain, token = _require_session()
    try:
        result = client.get_profile(domain, token)
    except client.ApiError as e:
        _handle_api_error(e)
    console.print(result)


@profile_app.command("set")
def profile_set(
    email: Optional[str] = typer.Option(None, "--email"),
    email_notifications: Optional[bool] = typer.Option(None, "--email-notifications/--no-email-notifications"),
    sms_number: Optional[str] = typer.Option(None, "--sms"),
    sms_notifications: Optional[bool] = typer.Option(None, "--sms-notifications/--no-sms-notifications"),
    allow_configure_command_sets: Optional[bool] = typer.Option(None, "--allow-command-sets/--no-allow-command-sets"),
    allow_configure_apps: Optional[bool] = typer.Option(None, "--allow-apps/--no-allow-apps"),
    allow_configure_hosts: Optional[bool] = typer.Option(None, "--allow-hosts/--no-allow-hosts"),
    allow_configure_environments: Optional[bool] = typer.Option(None, "--allow-environments/--no-allow-environments"),
    allow_configure_local_agents: Optional[bool] = typer.Option(None, "--allow-local-agents/--no-allow-local-agents"),
    system_prompt: Optional[str] = typer.Option(
        None, "--system-prompt", help="Custom instructions prepended to every chat turn. Pass '' to clear it."
    ),
    system_prompt_file: Optional[Path] = typer.Option(
        None, "--system-prompt-file", help="Read the system prompt from a file instead of passing it inline."
    ),
):
    """Merge-update the caller's own profile -- only flags actually passed
    are sent; everything else is left untouched server-side. See `profile
    get` to read the current system prompt back."""
    if system_prompt is not None and system_prompt_file is not None:
        err_console.print("[red]--system-prompt and --system-prompt-file are mutually exclusive.[/red]")
        raise typer.Exit(code=1)
    if system_prompt_file is not None:
        system_prompt = system_prompt_file.read_text()
    fields = {
        "email": email,
        "email_notifications_enabled": email_notifications,
        "sms_number": sms_number,
        "sms_notifications_enabled": sms_notifications,
        "allow_configure_command_sets": allow_configure_command_sets,
        "allow_configure_apps": allow_configure_apps,
        "allow_configure_hosts": allow_configure_hosts,
        "allow_configure_environments": allow_configure_environments,
        "allow_configure_local_agents": allow_configure_local_agents,
        "system_prompt": system_prompt,
    }
    fields = {k: v for k, v in fields.items() if v is not None}
    domain, token = _require_session()
    try:
        result = client.update_profile(domain, token, **fields)
    except client.ApiError as e:
        _handle_api_error(e)
    console.print(result)


def _print_layer(layer: dict):
    console.print(f"[bold]{layer['name']}[/bold] (id={layer['id']}, hosts={layer['host_ids']})")
    table = Table("#", "positional", "options", "tier")
    for i, rule in enumerate(layer["rules"]):
        positional = "; ".join(f"{p.get('whitelist', '')!r}/{p.get('blacklist', '')!r}" for p in rule["positional_constraints"])
        options = "; ".join(
            f"{o.get('short') or o.get('long')}:{o['pattern'].get('whitelist', '')!r}" for o in rule["option_constraints"]
        )
        table.add_row(str(i), positional, options, rule["tier"])
    console.print(table)


@policy_app.command("list")
def policy_list():
    """List every policy layer the caller owns."""
    domain, token = _require_session()
    try:
        result = client.list_policy_layers(domain, token)
    except client.ApiError as e:
        _handle_api_error(e)
    for layer in result["policy_layers"]:
        _print_layer(layer)


@policy_app.command("apply", no_args_is_help=True)
def policy_apply(yaml_path: str):
    """kubectl apply-style: create-or-replace a policy layer's rules from
    a YAML file -- see harness/client.py's load_policy_layer_yaml for the
    file format."""
    domain, token = _require_session()
    try:
        layer = client.apply_policy_layer(domain, token, yaml_path)
    except client.ApiError as e:
        _handle_api_error(e)
    console.print("[green]Applied.[/green]")
    _print_layer(layer)


@policy_app.command("delete", no_args_is_help=True)
def policy_delete(name_or_id: str):
    """Delete a policy layer by name or numeric id."""
    domain, token = _require_session()
    try:
        layer_id = _resolve_layer_id(domain, token, name_or_id)
        client.delete_policy_layer(domain, token, layer_id)
    except client.ApiError as e:
        _handle_api_error(e)
    console.print("[green]Deleted.[/green]")


def _resolve_layer_id(domain: str, token: str, name_or_id: str) -> int:
    if name_or_id.isdigit():
        return int(name_or_id)
    layers = client.list_policy_layers(domain, token)["policy_layers"]
    match = next((layer for layer in layers if layer["name"] == name_or_id), None)
    if match is None:
        err_console.print(f"[red]No policy layer named {name_or_id!r}.[/red]")
        raise typer.Exit(code=1)
    return match["id"]


def _resolve_host_id(domain: str, token: str, label_or_id: str) -> int:
    if label_or_id.isdigit():
        return int(label_or_id)
    all_hosts = client.list_hosts(domain, token)["hosts"]
    match = next((h for h in all_hosts if h["label"] == label_or_id), None)
    if match is None:
        err_console.print(f"[red]No host labeled {label_or_id!r}.[/red]")
        raise typer.Exit(code=1)
    return match["host_id"]


@policy_app.command("attach", no_args_is_help=True)
def policy_attach(layer: str, host: str):
    """Attach a policy layer (by name or id) to a host (by label or id)."""
    domain, token = _require_session()
    try:
        layer_id = _resolve_layer_id(domain, token, layer)
        host_id = _resolve_host_id(domain, token, host)
        result = client.add_policy_layer_to_host(domain, token, layer_id, host_id)
    except client.ApiError as e:
        _handle_api_error(e)
    _print_layer(result)


@policy_app.command("detach", no_args_is_help=True)
def policy_detach(layer: str, host: str):
    """Detach a policy layer (by name or id) from a host (by label or id)."""
    domain, token = _require_session()
    try:
        layer_id = _resolve_layer_id(domain, token, layer)
        host_id = _resolve_host_id(domain, token, host)
        result = client.remove_policy_layer_from_host(domain, token, layer_id, host_id)
    except client.ApiError as e:
        _handle_api_error(e)
    _print_layer(result)


@app.command(rich_help_panel=DEV_PANEL, short_help="Evaluate a hypothetical call against a composed policy.")
def eval(
    layer: list[str] = typer.Option([], "--layer", "-l", help="A policy layer name or id -- may be repeated."),
    arg: list[str] = typer.Option([], "--arg", "-a", help="A positional argument, in order -- may be repeated."),
    option: list[str] = typer.Option(
        [], "--option", "-o", help="short=VALUE, long=VALUE, or a bare short/long with no value -- may be repeated."
    ),
    cwd: str = typer.Option("", "--cwd", help="The hypothetical call's cwd -- checked against a rule's own cwd pattern."),
):
    """Evaluate a hypothetical call against a composed policy -- no
    execution, no daemon involved. The fastest, most deterministic rung of
    the testing ladder."""
    domain, token = _require_session()
    try:
        layer_ids = [_resolve_layer_id(domain, token, name_or_id) for name_or_id in layer]
        options = [_parse_option(o) for o in option]
        result = client.eval_policy(domain, token, layer_ids, positional_args=list(arg), options=options, cwd=cwd)
    except client.ApiError as e:
        _handle_api_error(e)
    console.print(result)


def _parse_option(spec: str) -> dict:
    """"long=value" / "long" (no "=") -- a single leading letter before
    "=" or standalone is treated as short, anything longer as long, same
    convention run_shell_command's own tool schema follows."""
    name, _, value = spec.partition("=")
    key = "short" if len(name) == 1 else "long"
    result = {key: name}
    if value:
        result["value"] = value
    return result


def _print_step_result(result: dict):
    if result["status"] == "done":
        console.print(f"[bold cyan]assistant:[/bold cyan] {result['message']}")
        for entry in result["turn"]["aggregate"].get("shell_command_calls", []):
            console.print(f"  [dim]$ {entry['args'].get('positional_args')} -> {entry['output']}[/dim]")
    else:
        pending = result["pending_approval"]
        console.print(
            f"[yellow]Approval needed[/yellow] on {pending.get('host')}: "
            f"{pending['args'].get('positional_args') or pending['args']}"
        )


def _resolve_pending_approval(turn: dict, domain: str, token: str, mock: bool, default_host: Optional[str], auto: Optional[str]):
    """Interactively (or via --approve/--deny) resolves a paused turn,
    looping until it's fully done -- shared by call-tool and chat."""
    while True:
        decision = auto
        if decision is None:
            decision = typer.prompt("Approve this call? [allow/deny]", default="allow")
        try:
            result = client.conversation_step(
                domain, token, turn=turn, approval_decision=decision, default_host=default_host, mock=mock
            )
        except client.ApiError as e:
            _handle_api_error(e)
        _print_step_result(result)
        if result["status"] == "done":
            return result
        turn = result["turn"]


@app.command("call-tool", rich_help_panel=DEV_PANEL, short_help="Inject one tool call directly, as if the model proposed it.", no_args_is_help=True)
def call_tool_cmd(
    name: str,
    arg: list[str] = typer.Option([], "--arg", help="key=json_value -- may be repeated, e.g. --arg positional_args='[\"npm\",\"run\"]'"),
    host: Optional[str] = typer.Option(None, "--host"),
    mock: bool = typer.Option(False, "--mock", help="Skip the real daemon/storage dispatch, return a canned result."),
    approve: bool = typer.Option(False, "--approve", help="Auto-approve any resulting ask-tier pause."),
    deny: bool = typer.Option(False, "--deny", help="Auto-deny any resulting ask-tier pause."),
):
    """Inject a tool call directly, as if the model had already decided to
    make it -- a live (or, with --mock, daemon-free) reproduction of one
    tool call, exercising the exact same tier-decision/dispatch/approval
    path a real model-issued call goes through."""
    domain, token = _require_session()
    arguments = {}
    for a in arg:
        key, _, raw_value = a.partition("=")
        try:
            arguments[key] = json.loads(raw_value)
        except json.JSONDecodeError:
            arguments[key] = raw_value
    try:
        result = client.call_tool(domain, token, name, arguments, mock=mock, default_host=host)
    except client.ApiError as e:
        _handle_api_error(e)
    _print_step_result(result)
    if result["status"] == "pending_approval":
        auto = "allow" if approve else ("deny" if deny else None)
        _resolve_pending_approval(result["turn"], domain, token, mock, host, auto)


@app.command(rich_help_panel=DEV_PANEL, short_help="Have a real conversational turn -- the model decides what to call.")
def chat(
    host: Optional[str] = typer.Option(None, "--host"),
    mock: bool = typer.Option(False, "--mock", help="Skip the real daemon/storage dispatch, return a canned result."),
    new: bool = typer.Option(False, "--new", help="Start a fresh conversation instead of resuming the last one."),
):
    """A real conversational turn -- the model decides what (if anything)
    to call. Ctrl-C, or typing 'exit'/'quit', to leave -- an empty line is
    ignored (just re-prompts) rather than ending the conversation, so a
    stray Enter press doesn't lose your place mid-chat. Remembers the
    conversation across separate `harness` launches for the same logged-in
    account -- running `harness chat` again picks up where you left off,
    unless --new is given or you type 'new'/'reset' during the
    conversation."""
    domain, token = _require_session()
    username = _load_session()["username"]
    turn = None
    resumed = False
    if not new:
        saved = _load_chat_state(domain, username)
        if saved is not None:
            turn = saved.get("turn")
            if host is None:
                host = saved.get("host")
            mock = mock or bool(saved.get("mock", False))
            resumed = turn is not None
    console.print("Casper harness chat. Type 'exit' or 'quit' (or Ctrl-C) to leave, 'new' to start over.")
    if resumed:
        console.print("[dim]Resuming your previous conversation.[/dim]")
    while True:
        try:
            message = typer.prompt("you")
        except (typer.Abort, KeyboardInterrupt):
            break
        stripped = message.strip().lower()
        if not stripped:
            continue
        if stripped in ("exit", "quit"):
            break
        if stripped in ("new", "reset"):
            turn = None
            _clear_chat_state()
            console.print("[dim]Started a new conversation.[/dim]")
            continue
        try:
            result = client.chat_step(domain, token, message=message, turn=turn, default_host=host, mock=mock)
        except client.ApiError as e:
            _handle_api_error(e)
        _print_step_result(result)
        if result["status"] == "pending_approval":
            result = _resolve_pending_approval(result["turn"], domain, token, mock, host, None)
        turn = result["turn"]
        _save_chat_state(domain, username, host, mock, turn)


if __name__ == "__main__":
    app()
