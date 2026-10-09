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
import re
from typing import Callable

from db import get_db
from mcp_server import VOICE

MAX_TOOL_ROUNDS = 8
HISTORY_LIMIT = 60

SYSTEM_PROMPT = """You are Casper's guide, setting Casper up with a person by chat. Casper keeps
their important folders mirrored on friends' computers -- continuously,
encrypted so friends can never read them, with 30 days of history -- and lets
them give space to friends. macOS only; early alpha. Its community defaults to
generosity: giving space asks nothing back. This is your own know-how: act
like you've always known it.

""" + VOICE + """RULES
- Anything that shares, invites or mirrors: call with preview=true, give the
  one-line plan, act (preview=false) only on their yes.
- Never pick a folder or a friend for them; suggest one ("Documents?").
- You can't confirm their own decisions. After starting a mirror, say: "Click
  Allow in the Casper dialog on your Mac." Never say it's protected until
  protection_status says so.
- You can't run anything on their Mac. If it isn't connected yet (list_hosts:
  no machine with role owner and connected true), say exactly this and
  nothing more: "Download Casper and open it: {download_url}". Opening it
  signs them in and connects the Mac; this page tells you when ("My Mac is
  connected."). No steps, no warnings about macOS prompts.
- An invite code (CASPER-XXXX-XXXX-XXXX) can be accepted (redeem_invite)
  before their Mac is connected; mirroring needs the Mac. After accepting,
  one line: "Offer <friend> space back? Not expected."
- Inviting a friend: their first name and how to send it (email address, their
  own Telegram to forward, or a message they pass on). The first time, also
  their group's name (e.g. "Mutual Aid"). 20 GB is a good default.
- After the first mirror is protected: the recovery kit. On their Mac, the
  Casper app makes it (Terminal: `/Applications/CasperGo/Casper.app/Contents/MacOS/Casper setup recovery-kit`).
  One line on why: without it, a lost Mac can't be rebuilt.
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
    _tool("create_invite", "Invite a friend with space on the person's machine (single-use). for_whom: their first name. group: the person's "
          "circle (ask the first time). Delivery: email and/or send_telegram; else a message they pass on. Sets up mirror space if they "
          "have none. quota_gb defaults to 20. preview=true first.",
          {"for_whom": _S, "quota_gb": _N, "email": _S, "send_telegram": _B, "group": _S, "host": _S, "preview": _B}, []),
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
            out = services.create_invite(user_id, float(args.get("quota_gb", 20)), None, args.get("for_whom", ""), args.get("preview", True),
                                         args.get("group", ""), args.get("email", ""), bool(args.get("send_telegram", False)), args.get("host", ""))
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


def system_prompt(download_url: str, signin_url: str = "") -> str:
    return SYSTEM_PROMPT.format(download_url=download_url)


def choices_for(reply: str, proposed: bool) -> list[str]:
    """Buttons instead of typing: a turn that proposed something (a preview)
    and ends with a short question ("Accept?", "Send?") gets that answer
    and "Not now" as buttons."""
    last = reply.strip().splitlines()[-1].strip() if reply.strip() else ""
    m = re.search(r"(?:^|[.!:]\s+|\u2014\s*|--\s*)([A-Z][A-Za-z']*(?: [a-z']+){0,2})\?$", last)
    if not proposed or not m:
        return []
    return [m.group(1), "Not now"]


def _for_model(messages: list[dict]) -> list[dict]:
    return [{k: v for k, v in m.items() if not k.startswith("_")} for m in messages]


def run(client, model: str, services, user_id: int, history: list[dict], user_text: str, prompt: str,
        on_tool: Callable[[str, dict], None] | None = None) -> tuple[str, list[dict]]:
    """One person-turn: the model may call tools for several rounds before
    answering. Returns (reply, the new history including this turn); the
    final assistant message carries any buttons as "_choices"."""
    messages = list(history) + [{"role": "user", "content": user_text}]
    proposed = False
    for _ in range(MAX_TOOL_ROUNDS):
        resp = client.chat.completions.create(model=model, messages=[{"role": "system", "content": prompt}] + _for_model(messages),
                                              tools=TOOLS, temperature=0.2)
        msg = resp.choices[0].message
        calls = msg.tool_calls or []
        entry = {"role": "assistant", "content": msg.content or ""}
        if calls:
            entry["tool_calls"] = [{"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments or "{}"}} for c in calls]
        messages.append(entry)
        if not calls:
            reply = (msg.content or "").strip()
            if choices := choices_for(reply, proposed):
                entry["_choices"] = choices
            return reply, messages
        for c in calls:
            try:
                args = json.loads(c.function.arguments or "{}")
            except ValueError:
                args = {}
            if on_tool:
                on_tool(c.function.name, args)
            result = _call_tool(services, user_id, c.function.name, args)
            proposed = proposed or result.startswith("PREVIEW")
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
    shown = [{"role": m["role"], "text": m["content"], **({"choices": m["_choices"]} if m.get("_choices") else {})}
             for m in messages if m["role"] in ("user", "assistant") and m.get("content") and not m.get("tool_calls")]
    for m in shown[:-1]:  # only the latest message's buttons still apply
        m.pop("choices", None)
    return shown


def client_from_env():
    from openai import OpenAI

    return OpenAI(base_url=os.environ.get("NOUS_BASE_URL", "https://inference-api.nousresearch.com/v1"),
                  api_key=os.environ.get("NOUS_API_KEY", ""))
