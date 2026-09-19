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
import time
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import client

app = typer.Typer(help="Casper backend test harness -- drives auth_service directly, no GUI in the loop.")
policy_app = typer.Typer(help="Manage policy layers.")
app.add_typer(policy_app, name="policy")
hosts_app = typer.Typer(help="Manage known hosts.")
app.add_typer(hosts_app, name="hosts")
environment_app = typer.Typer(help="Manage Environments.")
app.add_typer(environment_app, name="environment")
profile_app = typer.Typer(help="Manage account profile/notification/permission preferences.")
app.add_typer(profile_app, name="profile")

console = Console()
err_console = Console(stderr=True)

DEFAULT_DOMAIN = "localhost:8100"
SESSION_PATH = Path(os.environ.get("CASPER_HARNESS_SESSION", str(Path.home() / ".casper-harness" / "session.json")))


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


@app.callback()
def main(debug: bool = typer.Option(False, "--debug", "--raw", help="Print every request/response.")):
    """Casper backend test harness."""
    client.toggle_debug(debug)


@app.command()
def signup(
    username: str,
    domain: str = typer.Option(DEFAULT_DOMAIN, "--domain", "-d", envvar="CASPER_HARNESS_DOMAIN"),
    password: str = typer.Option(..., prompt=True, hide_input=True, confirmation_prompt=True),
):
    """Create a new account and save the resulting session."""
    try:
        result = client.signup(domain, username, password)
    except client.ApiError as e:
        _handle_api_error(e)
    _save_session(domain, result["username"], result["token"])
    console.print(f"[green]Signed up as {result['username']}.[/green] Session saved to {SESSION_PATH}.")


@app.command()
def login(
    username: str,
    domain: str = typer.Option(DEFAULT_DOMAIN, "--domain", "-d", envvar="CASPER_HARNESS_DOMAIN"),
    password: str = typer.Option(..., prompt=True, hide_input=True),
):
    """Sign in and save the resulting session."""
    try:
        result = client.signin(domain, username, password)
    except client.ApiError as e:
        _handle_api_error(e)
    _save_session(domain, result["username"], result["token"])
    console.print(f"[green]Signed in as {result['username']}.[/green] Session saved to {SESSION_PATH}.")


@app.command()
def whoami():
    """Show the currently saved session, if any."""
    session = _load_session()
    if session is None:
        console.print("Not logged in.")
        raise typer.Exit(code=1)
    console.print(f"{session['username']} @ {session['domain']}")


@app.command()
def logout():
    """Discard the saved session."""
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


@hosts_app.command("rename")
def hosts_rename(host: str, label: str):
    """Rename one of the caller's own hosts (by current label or id)."""
    domain, token = _require_session()
    try:
        host_id = _resolve_host_id(domain, token, host)
        client.rename_host(domain, token, host_id, label)
    except client.ApiError as e:
        _handle_api_error(e)
    console.print("[green]Renamed.[/green]")


@hosts_app.command("forget")
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


@app.command("pair-daemon")
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
    try:
        before = {h["host_id"] for h in client.list_hosts(domain, token)["hosts"] if h["connected"]}
        client.trigger_real_pairing(username, token)
    except client.ApiError as e:
        _handle_api_error(e)
    except RuntimeError as e:
        err_console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1) from e

    console.print("Pairing dispatched -- waiting for a newly-connected host...")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
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


@app.command()
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


@app.command("report-presence")
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


@app.command()
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


@app.command("respond-approvals")
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


@environment_app.command("create")
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


@environment_app.command("rename")
def environment_rename(environment: str, name: str):
    domain, token = _require_session()
    try:
        environment_id = _resolve_environment_id(domain, token, environment)
        result = client.rename_environment(domain, token, environment_id, name)
    except client.ApiError as e:
        _handle_api_error(e)
    console.print(result)


@environment_app.command("delete")
def environment_delete(environment: str):
    domain, token = _require_session()
    try:
        environment_id = _resolve_environment_id(domain, token, environment)
        client.delete_environment(domain, token, environment_id)
    except client.ApiError as e:
        _handle_api_error(e)
    console.print("[green]Deleted.[/green]")


@environment_app.command("attach")
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


@environment_app.command("detach")
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
):
    """Merge-update the caller's own profile -- only flags actually passed
    are sent; everything else is left untouched server-side."""
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


@policy_app.command("apply")
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


@policy_app.command("delete")
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


@policy_app.command("attach")
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


@policy_app.command("detach")
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


@app.command()
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


@app.command("call-tool")
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


@app.command()
def chat(
    host: Optional[str] = typer.Option(None, "--host"),
    mock: bool = typer.Option(False, "--mock", help="Skip the real daemon/storage dispatch, return a canned result."),
):
    """A real conversational turn -- the model decides what (if anything)
    to call. Ctrl-C or an empty line to exit."""
    domain, token = _require_session()
    turn = None
    console.print("Casper harness chat. Empty line or Ctrl-C to exit.")
    while True:
        try:
            message = typer.prompt("you")
        except (typer.Abort, KeyboardInterrupt):
            break
        if not message.strip():
            break
        try:
            result = client.chat_step(domain, token, message=message, turn=turn, default_host=host, mock=mock)
        except client.ApiError as e:
            _handle_api_error(e)
        _print_step_result(result)
        if result["status"] == "pending_approval":
            result = _resolve_pending_approval(result["turn"], domain, token, mock, host, None)
        turn = result["turn"]


if __name__ == "__main__":
    app()
