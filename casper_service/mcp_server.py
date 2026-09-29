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


class _Unauthorized(Exception):
    pass


def _caller(svc: Services, ctx: Context) -> int:
    headers = ctx.headers or {}
    agent = svc.resolve_agent(headers.get("authorization", ""))
    if agent is None:
        raise _Unauthorized("Invalid or missing agent token.")
    return agent["user_id"]


def build(svc: Services) -> MCPServer:
    server = MCPServer(name="casper", instructions=INSTRUCTIONS)

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
            "May pause for the destination owner's approval -- you'll be notified when it finishes."
        )
    )
    def backup_push(ctx: Context, source_host: str, path: str, dest_host: str) -> str:
        return svc.backup_push(_caller(svc, ctx), source_host, path, dest_host)

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
