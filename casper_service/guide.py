"""Casper's own guide: the onboarding agent for people who don't have one
(docs/product/scenarios/agent-onboarding.md, "people without an agent").

It's just another agent acting on the person's behalf -- the same tools, the
same limits as anyone's own agent (it can't give the person's own
confirmations either). It lives on the web (after sign-in) and in Telegram,
sharing one conversation per person. It can't run commands on the person's
Mac, so installing and pairing are clicks it walks them through; everything
after that it does through Casper's tools.

The model is any OpenAI-compatible chat-completions model (Nous Portal by
default: NOUS_BASE_URL / NOUS_API_KEY / GUIDE_MODEL) -- the cheapest one that
passes tests/guide_eval.py's scripted conversations."""

import json
import os
from typing import Callable

from db import get_db

MAX_TOOL_ROUNDS = 8
HISTORY_LIMIT = 60

SYSTEM_PROMPT = """You are Casper's guide, helping a person set up Casper by chat. Casper keeps
a person's important folders mirrored on friends' computers -- continuously,
encrypted so the friends can never read them, with 30 days of history -- and
lets people offer space on their own computer for friends. Casper is an early
alpha, macOS only. Its community defaults to generosity: offering space asks
nothing back.

How to work:
- Be brief and warm. One or two questions at a time, each with a suggested answer.
- Every sharing tool has preview=true by default: call it, tell the person the
  plan in plain words, and only call again with preview=false after they say yes.
- Never choose a folder or a friend for them -- suggest, then ask.
- You can't confirm the person's own decisions (starting a mirror): when you
  start one, tell them to click Allow in the Casper dialog on their Mac (or in
  Telegram). Never say it's done until protection_status says protected.
- You can't run anything on their Mac. Installing and pairing are clicks:
    1. Download Casper: {download_url} -- open it once (it asks about opening at
       login: suggest yes).
    2. Sign in at {signin_url} on that Mac -- that connects the Mac to their account.
  Use list_hosts to see whether a Mac is connected (role owner, connected true).
  Someone arriving with an invite code can accept it (redeem_invite) before
  their Mac is connected; mirroring needs the Mac.
- Inviting a friend: ask their first name (the invitation greets them by it),
  what the person calls their group the first time (e.g. "Mutual Aid"), and
  how to send it -- by email (their address), to the person's own Telegram to
  forward, or a message the person passes on themselves.
- After mirroring starts, suggest the recovery kit: on their Mac, it's made by
  the Casper app (their own agent or Terminal can run
  `/Applications/CasperGo/Casper.app/Contents/MacOS/Casper setup recovery-kit`);
  explain why it matters (losing the Mac otherwise loses the ability to rebuild).
- Teach as you go: friends hold encrypted copies they can't read; changes
  mirror within seconds while both computers are on; anything can be undone
  from history; they'll hear from Casper only if something needs them.
- If a tool says something can't be done, explain it plainly and suggest the next step.
"""

# OpenAI-style tool schemas for the subset of Casper's tools the guide uses.
_S = {"type": "string"}
_N = {"type": "number"}
_B = {"type": "boolean"}


def _tool(name: str, description: str, props: dict | None = None, required: list | None = None) -> dict:
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": props or {}, "required": required or []}}}


TOOLS = [
    _tool("my_casper", "The person's whole Casper picture: machines, friends, offerings, invites, space given and held, mirrored folders, anything waiting."),
    _tool("list_hosts", "Machines the person can use: their own (role owner, connected?) and friends' (role mirror_peer / catcher_peer)."),
    _tool("list_offerings", "What friends offer (mirror space, catcher space)."),
    _tool("redeem_invite", "Use a friend's invite code (CASPER-XXXX-XXXX-XXXX). preview=true first.", {"code": _S, "preview": _B}, ["code"]),
    _tool("add_friend", "Send a friend request by username.", {"username": _S}, ["username"]),
    _tool("publish_offering", "Offer space on one of the person's machines. kind 'mirror' (usual) or 'catcher' (always-on machines). preview=true first.",
          {"host": _S, "max_gb": _N, "kind": {"type": "string", "enum": ["mirror", "catcher"]}, "preview": _B}, ["host", "max_gb"]),
    _tool("create_invite", "Create a single-use invite to the person's offering. for_whom: the friend's first name (the invitation greets them). "
          "group: the person's circle the friend joins (e.g. 'Mutual Aid'; ask the first time). Delivery: email (friend's address) and/or "
          "send_telegram (a copy in the person's Telegram to forward); else returns a message for them to send. preview=true first.",
          {"quota_gb": _N, "for_whom": _S, "group": _S, "email": _S, "send_telegram": _B, "preview": _B}, ["quota_gb"]),
    _tool("mirror_folder", "Mirror a folder from the person's Mac to friends' machines (owner/host names from list_hosts). preview=true first; then the PERSON confirms in a Casper dialog.",
          {"source_host": _S, "path": _S, "mirrors": {"type": "array", "items": _S}, "catcher": _S, "use_casper_catcher": _B, "preview": _B},
          ["source_host", "path", "mirrors"]),
    _tool("protection_status", "Whether each mirrored folder is protected right now, mirror by mirror."),
    _tool("list_versions", "Earlier versions of files in a mirrored folder.", {"folder": _S, "name_contains": _S}, ["folder"]),
    _tool("restore_version", "Bring back one earlier version (name and at exactly as listed) as a new copy.", {"folder": _S, "name": _S, "at": _S}, ["folder", "name", "at"]),
    _tool("stop_mirroring", "Stop mirroring a folder (confirm first).", {"folder": _S}, ["folder"]),
    _tool("list_approvals", "Requests from other people waiting for the person's decision."),
    _tool("decide_approval", "Record the person's decision on one request from list_approvals.", {"approval_id": _S, "approve": _B}, ["approval_id", "approve"]),
    _tool("revoke", "Undo: kind grant|invite|offering|friend, by id or username (confirm first).", {"kind": _S, "id_or_name": _S}, ["kind", "id_or_name"]),
]


def _call_tool(services, user_id: int, name: str, args: dict) -> str:
    """Runs one tool through the same functions the MCP server uses."""
    try:
        if name == "redeem_invite":
            out = services.redeem_invite(user_id, args["code"], args.get("preview", True))
        elif name == "add_friend":
            out = services.add_friend(user_id, args["username"])
        elif name == "publish_offering":
            out = services.publish_offering(user_id, args["host"], float(args["max_gb"]), False, args.get("preview", True), args.get("kind", "mirror"))
        elif name == "create_invite":
            out = services.create_invite(user_id, float(args["quota_gb"]), None, args.get("for_whom", ""), args.get("preview", True),
                                         args.get("group", ""), args.get("email", ""), bool(args.get("send_telegram", False)))
        elif name == "mirror_folder":
            out = services.mirror_folder(user_id, args["source_host"], args["path"], args.get("mirrors") or [], args.get("catcher", ""),
                                         bool(args.get("use_casper_catcher", False)), "", args.get("preview", True))
        elif name == "list_versions":
            out = services.list_versions(user_id, args["folder"], args.get("name_contains", ""))
        elif name == "restore_version":
            out = services.restore_version(user_id, args["folder"], args["name"], args["at"])
        elif name == "stop_mirroring":
            out = services.stop_mirroring(user_id, args["folder"])
        elif name == "decide_approval":
            out = services.decide_approval(user_id, args["approval_id"], bool(args["approve"]))
        elif name == "revoke":
            out = services.revoke(user_id, args["kind"], args["id_or_name"])
        elif name in ("my_casper", "list_hosts", "list_offerings", "protection_status", "list_approvals"):
            out = getattr(services, name)(user_id)
        else:
            return f"Unknown tool {name}."
    except KeyError as e:
        return f"Missing argument {e}."
    except Exception as e:  # a tool failing must not end the conversation
        return f"That didn't work: {e}"
    return out if isinstance(out, str) else json.dumps(out)


def system_prompt(download_url: str, signin_url: str) -> str:
    return SYSTEM_PROMPT.format(download_url=download_url, signin_url=signin_url)


def run(client, model: str, services, user_id: int, history: list[dict], user_text: str, prompt: str,
        on_tool: Callable[[str, dict], None] | None = None) -> tuple[str, list[dict]]:
    """One person-turn: the model may call tools for several rounds before
    answering. Returns (reply, the new history including this turn)."""
    messages = list(history) + [{"role": "user", "content": user_text}]
    for _ in range(MAX_TOOL_ROUNDS):
        resp = client.chat.completions.create(model=model, messages=[{"role": "system", "content": prompt}] + messages,
                                              tools=TOOLS, temperature=0.2)
        msg = resp.choices[0].message
        calls = msg.tool_calls or []
        entry = {"role": "assistant", "content": msg.content or ""}
        if calls:
            entry["tool_calls"] = [{"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments or "{}"}} for c in calls]
        messages.append(entry)
        if not calls:
            return (msg.content or "").strip(), messages
        for c in calls:
            try:
                args = json.loads(c.function.arguments or "{}")
            except ValueError:
                args = {}
            if on_tool:
                on_tool(c.function.name, args)
            result = _call_tool(services, user_id, c.function.name, args)
            messages.append({"role": "tool", "tool_call_id": c.id, "content": result[:6000]})
    return "Sorry -- I got stuck working on that. Could you say it another way?", messages


# --- Stored conversations (one per person, shared by web and Telegram) -----------
def load_history(user_id: int) -> list[dict]:
    with get_db() as db:
        rows = db.execute(
            "SELECT message FROM guide_messages WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, HISTORY_LIMIT)
        ).fetchall()
    msgs = [json.loads(r["message"]) for r in reversed(rows)]
    while msgs and msgs[0].get("role") == "tool":  # never start mid tool exchange
        msgs.pop(0)
    while msgs and msgs[0].get("role") == "assistant" and msgs[0].get("tool_calls"):
        msgs.pop(0)
        while msgs and msgs[0].get("role") == "tool":
            msgs.pop(0)
    return msgs


def save_new(user_id: int, before: int, messages: list[dict]):
    with get_db() as db:
        for m in messages[before:]:
            db.execute("INSERT INTO guide_messages (user_id, message) VALUES (?, ?)", (user_id, json.dumps(m)))


def visible(messages: list[dict]) -> list[dict]:
    """What a person sees: their messages and the guide's replies."""
    return [{"role": m["role"], "text": m["content"]} for m in messages
            if m["role"] in ("user", "assistant") and m.get("content") and not m.get("tool_calls")]


def client_from_env():
    from openai import OpenAI

    return OpenAI(base_url=os.environ.get("NOUS_BASE_URL", "https://inference-api.nousresearch.com/v1"),
                  api_key=os.environ.get("NOUS_API_KEY", ""))
