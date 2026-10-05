"""Casper's MCP server -- the front door for bring-your-own agents (see
docs/product/README.md's pivot, and docs/product/v1-implementation-plan.md's
Phase 1). Mounted into main.py's FastAPI app at /mcp over the streamable
HTTP transport, stateless (every request is independently authenticated;
no server-side MCP session to leak across callers).

Deliberately thin: every tool here only authenticates the caller and hands
off to a plain function main.py supplies (see build()'s `svc`), so the
actual logic stays testable without an MCP client, and this module never
imports main.py (which would be circular).

Auth: `Authorization: Bearer <agent token>` (see main.py's agent_tokens
table / `harness agent token create`). Checked twice on purpose --
AgentAuthASGI rejects an unauthenticated request with a plain 401 before
the MCP transport ever sees it, and every tool re-resolves the token from
its own request's headers to learn WHO is calling. A header is client-
supplied input, but a bearer token looked up against our own table is
exactly the credential check it's meant for, not an identity claim taken
on faith. The agent acts on behalf of the token's owner, with at most that
owner's permissions (docs/product/trust-framework.md's "Agents as
subjects", v1).

Tools return plain text: an ask-tier pause is reported in words the calling
agent can relay as-is ("waiting for Sam to approve -- you'll be notified"),
never a status an agent has to know how to poll -- most MCP clients don't
implement the Tasks extension yet."""

from dataclasses import dataclass
from typing import Any, Callable

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

INSTRUCTIONS = """Casper gives you governed access to machines ("hosts") -- the user's own, and
ones friends have shared with them. Every call is checked by the target
host's own policy: it may run immediately, be denied, or pause until the
host's owner approves it. When a call pauses, tell the user plainly who
needs to approve it; they'll be notified of the outcome, so don't poll.
Use list_hosts first to see what's available."""


@dataclass
class Services:
    """Everything the tools need from main.py -- plain callables taking the
    already-authenticated caller's user_id first."""

    resolve_agent: Callable[[str], dict | None]  # Authorization header -> {"user_id", "agent_token_id", ...} or None
    list_hosts: Callable[[int], list[dict]]
    run_shell_command: Callable[..., str]
    list_offerings: Callable[[int], list[dict]]
    request_access: Callable[..., str]
    backup_push: Callable[..., str]
    backup_status: Callable[..., str]
    backup_list: Callable[..., str]
    backup_restore: Callable[..., str]
    backup_delete: Callable[..., str]
    # Onboarding (docs/product/scenarios/agent-onboarding.md); filled in by
    # main.py after construction.
    my_casper: Callable[..., str] | None = None
    add_friend: Callable[..., str] | None = None
    publish_offering: Callable[..., str] | None = None
    create_invite: Callable[..., str] | None = None
    redeem_invite: Callable[..., str] | None = None
    list_approvals: Callable[..., str] | None = None
    decide_approval: Callable[..., str] | None = None
    revoke: Callable[..., str] | None = None
    # Mirroring (docs/product/scenarios/mirroring.md).
    mirror_folder: Callable[..., str] | None = None
    protection_status: Callable[..., str] | None = None
    list_versions: Callable[..., str] | None = None
    restore_version: Callable[..., str] | None = None
    restore_folder: Callable[..., str] | None = None
    stop_mirroring: Callable[..., str] | None = None


class _Unauthorized(Exception):
    pass


def _caller(svc: Services, ctx: Context) -> int:
    headers = ctx.headers or {}
    agent = svc.resolve_agent(headers.get("authorization", ""))
    if agent is None:
        raise _Unauthorized("Invalid or missing agent token.")
    return agent["user_id"]


def _logged(fn):
    """Print a tool's traceback to the service log before the MCP SDK turns
    it into a bare 'Error executing tool' for the agent."""
    import functools
    import traceback

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except _Unauthorized:
            raise
        except Exception:
            traceback.print_exc()
            raise

    return wrapper


def build(svc: Services) -> MCPServer:
    server = MCPServer(name="casper", instructions=INSTRUCTIONS)
    _tool = server.tool

    def logged_tool(*a, **k):
        register = _tool(*a, **k)
        return lambda fn: register(_logged(fn))

    server.tool = logged_tool

    @server.tool(description="List the hosts you can use: your own, and ones friends have shared with you (with your role and any quota).")
    def list_hosts(ctx: Context) -> list[dict[str, Any]]:
        return svc.list_hosts(_caller(svc, ctx))

    @server.tool(
        description=(
            "Run one command on one of YOUR OWN hosts, with structured arguments (never a shell string). "
            "positional_args[0] is the binary itself. options are named entries like "
            '{"long": "all"} or {"short": "n", "value": "5"}. path is the directory to run in (defaults to the host\'s home). '
            "The host's policy decides: it runs, is denied, or waits for the owner's approval."
        )
    )
    def run_shell_command(
        ctx: Context,
        host: str,
        positional_args: list[str],
        options: list[dict[str, Any]] | None = None,
        path: str | None = None,
    ) -> str:
        return svc.run_shell_command(_caller(svc, ctx), host, positional_args, options or [], path)

    @server.tool(description="List what your friends are offering to share (e.g. backup space on their machine), including what you already hold.")
    def list_offerings(ctx: Context) -> list[dict[str, Any]]:
        return svc.list_offerings(_caller(svc, ctx))

    @server.tool(description="Request one of a friend's offerings (see list_offerings). quota_gb is how much space you're asking for, for backup space. The friend is notified and decides.")
    def request_access(ctx: Context, offering_id: int, quota_gb: float) -> str:
        return svc.request_access(_caller(svc, ctx), offering_id, quota_gb)

    @server.tool(
        description=(
            "Back up a folder from one of your own hosts to a host where you hold backup space (see list_hosts). "
            "The folder is encrypted on your own machine first; the destination can never read it. "
            "Ask the person which folder first -- never pick one for them. Call with preview=true (the default), show the "
            "person the plan, and only call again with preview=false once they agree. "
            "May pause for the destination owner's approval -- you'll be notified when it finishes."
        )
    )
    def backup_push(ctx: Context, source_host: str, path: str, dest_host: str, preview: bool = True) -> str:
        return svc.backup_push(_caller(svc, ctx), source_host, path, dest_host, preview)

    @server.tool(description="Show the status of one backup (by id), or of your recent backups if no id is given.")
    def backup_status(ctx: Context, backup_id: str | None = None) -> str:
        return svc.backup_status(_caller(svc, ctx), backup_id)

    @server.tool(description="List the backups you have stored on other people's hosts.")
    def backup_list(ctx: Context) -> str:
        return svc.backup_list(_caller(svc, ctx))

    @server.tool(
        description=(
            "Restore one of your backups (by id) to one of your own hosts. It's decrypted there, into a new "
            "'Casper Restores' folder in the home directory -- nothing existing is overwritten."
        )
    )
    def backup_restore(ctx: Context, backup_id: str, dest_host: str) -> str:
        return svc.backup_restore(_caller(svc, ctx), backup_id, dest_host)

    @server.tool(description="Delete one of your backups (by id) from the host storing it.")
    def backup_delete(ctx: Context, backup_id: str) -> str:
        return svc.backup_delete(_caller(svc, ctx), backup_id)

    # --- Onboarding and the ledger -------------------------------------------
    @server.tool(description="The person's whole Casper picture in plain words: their machines, friends, what they offer, open invites, space given and held, backups, and anything waiting on a decision. Use it to answer \"what have I shared?\" and to check state before acting.")
    def my_casper(ctx: Context) -> str:
        return svc.my_casper(_caller(svc, ctx))

    @server.tool(description="Send a friend request by username. The other person must accept. (To set someone up who isn't on Casper yet, use create_invite instead.)")
    def add_friend(ctx: Context, username: str) -> str:
        return svc.add_friend(_caller(svc, ctx), username)

    @server.tool(
        description=(
            "Offer space on one of the person's own machines to their friends. kind='mirror' (usual): friends' folders are mirrored "
            "here, encrypted, with 30 days of history. kind='catcher': for an always-on machine -- it briefly holds only the changes "
            "friends' mirrors haven't received yet. kind='backup': snapshot backups (older feature). max_gb is the most any one friend "
            "can get. Offerings ask nothing in return -- Casper defaults to generosity. "
            "Call with preview=true first, show the person the plan, and only call again with preview=false once they agree."
        )
    )
    def publish_offering(ctx: Context, host: str, max_gb: float, kind: str = "mirror", approve_each_backup: bool = False, preview: bool = True) -> str:
        return svc.publish_offering(_caller(svc, ctx), host, max_gb, approve_each_backup, preview, kind)

    @server.tool(
        description=(
            "Create a single-use invite to the person's offering for one friend, with quota_gb of space. Returns the code and a ready-to-send "
            "message. for_whom is the friend's first name -- the invitation greets them by it. group is the name of the person's circle "
            "the friend is welcomed into (e.g. 'Mutual Aid'; ask what they call it the first time -- after that their one group is the default). "
            "Delivery: email (the friend's address) and/or send_telegram=true (a copy in the person's own Telegram, to forward); otherwise "
            "they pass the message on themselves. Call with preview=true first and confirm with the person."
        )
    )
    def create_invite(ctx: Context, quota_gb: float, for_whom: str = "", offering_id: int | None = None, group: str = "",
                      email: str = "", send_telegram: bool = False, preview: bool = True) -> str:
        return svc.create_invite(_caller(svc, ctx), quota_gb, offering_id, for_whom, preview, group, email, send_telegram)

    @server.tool(
        description=(
            "Use an invite code a friend sent: become their friend on Casper and receive the backup space it offers. "
            "Call with preview=true first, show the person what it means, and redeem (preview=false) once they agree."
        )
    )
    def redeem_invite(ctx: Context, code: str, preview: bool = True) -> str:
        return svc.redeem_invite(_caller(svc, ctx), code, preview)

    @server.tool(description="Requests from OTHER people waiting for the person's decision (friend requests, requests for space, a friend's backup needing approval). Ask the person; never decide for them.")
    def list_approvals(ctx: Context) -> str:
        return svc.list_approvals(_caller(svc, ctx))

    @server.tool(description="Record the person's own decision on one request from list_approvals -- only after they've told you what they want. The person's own requests can't be approved here.")
    def decide_approval(ctx: Context, approval_id: str, approve: bool) -> str:
        return svc.decide_approval(_caller(svc, ctx), approval_id, approve)

    @server.tool(description="Undo: kind='grant' (end backup space given or held, by grant id), 'invite' (cancel an unused invite), 'offering' (withdraw an offering), or 'friend' (by username; also ends space between you). Ids are in my_casper. Confirm with the person first.")
    def revoke(ctx: Context, kind: str, id_or_name: str) -> str:
        return svc.revoke(_caller(svc, ctx), kind, id_or_name)

    # --- Mirroring -------------------------------------------------------------
    @server.tool(
        description=(
            "Mirror a folder from one of the person's own machines to friends' machines where they hold mirror space "
            "(names as list_hosts shows them, e.g. ['sam/sam-mini']). Ask the person which folder (Documents or Desktop are good "
            "defaults; not Photos) and which friends. Optionally a catcher (a friend's always-on machine, catcher space) -- or, "
            "only if they have none, use_casper_catcher=true (Casper's own server, encrypted). preview=true first, show the plan; "
            "then preview=false starts it, and the PERSON must confirm in a Casper dialog on their Mac or in Telegram -- you can't."
        )
    )
    def mirror_folder(ctx: Context, source_host: str, path: str, mirrors: list[str], catcher: str = "", use_casper_catcher: bool = False, label: str = "", preview: bool = True) -> str:
        return svc.mirror_folder(_caller(svc, ctx), source_host, path, mirrors, catcher, use_casper_catcher, label, preview)

    @server.tool(description="How well each mirrored folder is protected right now: whether every change is on a mirror, which mirrors are up to date or asleep, and any recent changes not yet protected.")
    def protection_status(ctx: Context) -> str:
        return svc.protection_status(_caller(svc, ctx))

    @server.tool(description="List earlier versions of files in a mirrored folder (by its name, e.g. 'Documents'), optionally filtered by part of a file name. Versions live on the mirrors, encrypted; their names are decrypted on the person's own Mac.")
    def list_versions(ctx: Context, folder: str, name_contains: str = "") -> str:
        return svc.list_versions(_caller(svc, ctx), folder, name_contains)

    @server.tool(description="Bring back one earlier version (name and at exactly as list_versions shows). It's restored as a new copy under 'Casper Restores' -- nothing is overwritten.")
    def restore_version(ctx: Context, folder: str, name: str, at: str) -> str:
        return svc.restore_version(_caller(svc, ctx), folder, name, at)

    @server.tool(description="Rebuild a whole mirrored folder onto another (e.g. a replacement) Mac of the person's, from the mirrors and catcher. That Mac needs the recovery kit imported first (`Casper setup restore`).")
    def restore_folder(ctx: Context, folder: str, dest_host: str, dest_path: str = "") -> str:
        return svc.restore_folder(_caller(svc, ctx), folder, dest_host, dest_path)

    @server.tool(description="Stop mirroring a folder. Friends' copies are deleted after 7 days. Confirm with the person first.")
    def stop_mirroring(ctx: Context, folder: str) -> str:
        return svc.stop_mirroring(_caller(svc, ctx), folder)

    return server


def asgi_app(server: MCPServer, svc: Services):
    """The streamable-HTTP ASGI app to mount at /mcp, wrapped so an
    unauthenticated request gets a plain 401 before the MCP transport runs.
    DNS-rebinding protection is off: that guards a server listening on
    localhost against a browser being tricked into calling it, whereas this
    one is a public endpoint behind the same Cloudflare Tunnel as every
    other casper_service route, authenticated by bearer token."""
    starlette_app = server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    inner = next(r.app for r in starlette_app.routes if getattr(r, "path", None) == "/mcp")
    return _AgentAuthASGI(inner, svc)


class _AgentAuthASGI:
    """A class, not a function: Starlette treats a plain function endpoint
    as a request->response handler, but an object as a raw ASGI app."""

    def __init__(self, inner, svc: Services):
        self.inner = inner
        self.svc = svc

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            if self.svc.resolve_agent(headers.get("authorization", "")) is None:
                response = JSONResponse({"detail": "Invalid or missing agent token."}, status_code=401)
                await response(scope, receive, send)
                return
        await self.inner(scope, receive, send)
