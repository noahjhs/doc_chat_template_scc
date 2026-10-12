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
from dataclasses import dataclass, field
from typing import Callable

from db import get_db
from mcp_server import VOICE

MAX_TOOL_ROUNDS = 8
HISTORY_LIMIT = 60

SYSTEM_PROMPT = """You are Casper's guide, setting Casper up with a person by chat. Casper keeps
their important folders mirrored on friends' computers: continuously,
encrypted so friends can never read them, with 30 days of history. It also
lets them give space to friends. macOS only; early alpha. Its community defaults to
generosity: giving space asks nothing back. This is your own know-how: act
like you've always known it.

""" + VOICE + """FORMAT: plain text only. This chat is read on the web and in Telegram, where
formatting marks show literally. No Markdown: no **bold**, headings, bullet
markers or [text](links). Put a link bare.

RULES
- One ask per message. Never two questions, never a question plus an offer.
- Anything that shares, invites or mirrors: call with preview=true, give the
  one-line plan, act (preview=false) only on their yes.
- Never pick a folder or a friend for them; suggest one ("Documents?").
- You can't confirm their own decisions, and you can't run anything on their
  Mac. Never say it's protected until protection_status says so.
- Inviting a friend: their first name, then "Email or Telegram?"
  (Telegram: a ready-to-forward copy in their own Telegram), then, the
  first time, their group's name (e.g. "Mutual Aid"). One at a time.
  20 GB is a good default.
- The NOW section below is where this person is in setting up. Do that and
  nothing else; if they ask about something else, answer briefly, then
  come back to it.
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
    _tool("create_invite", "Invite one friend with space on the person's machine. for_whom: their first name. group: the person's "
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


@dataclass
class Facts:
    """Where the person really is, from Casper's own state -- never inferred
    from the conversation."""
    mac_connected: bool = False
    invite: dict | None = None  # {"code", "inviter", "state": "open" | "accepted"} for a code they gave
    mirror_hosts: list = field(default_factory=list)  # where they hold mirror space, e.g. "riley/Rileys-MacBook"
    catcher_hosts: list = field(default_factory=list)
    folder: str = "none"  # their newest mirror: "none" | "awaiting_consent" | "active"
    protected: bool = False
    recovery_kit: bool = False
    offers_space: bool = False


@dataclass
class Stage:
    name: str
    now: str  # the one thing to do this turn
    tools: tuple = ()  # what it may change ("*" = anything); the read-only tools are always there
    choices: tuple = ()  # its buttons, unless a plan was just proposed
    say: str = ""  # its exact line when the step begins -- written by Casper, not the model


READ_ONLY = ("my_casper", "list_hosts", "list_offerings", "protection_status", "list_versions", "list_approvals")


def stage_for(facts: Facts, history: list[dict], download_url: str) -> Stage:
    """The setup gates, in order: one current step, decided by Casper's
    state. Once-only asks (the recovery kit, giving back) count as done once
    they've been asked."""
    asked = {m.get("_stage") for m in history if m.get("role") == "assistant"}
    inv = facts.invite
    if inv and inv["state"] == "open":
        return Stage("accept_invite", f"They have {inv['inviter']}'s invite ({inv['code']}). Preview it (redeem_invite, "
                     "preview=true) and give only the one-line plan, ending \"Accept?\". On their yes, accept it.", ("redeem_invite",))
    if not facts.mac_connected:
        lead = "If something just happened (an invite accepted), say it in a few words first. " if inv else ""
        return Stage("install", f"Their Mac isn't connected yet. {lead}Say exactly \"Download Casper and open it: {download_url}\" "
                     "and nothing more. Opening it connects their Mac; you'll hear \"My Mac is connected.\"",
                     say=f"Download Casper and open it: {download_url}")
    if facts.mirror_hosts and facts.folder == "none":
        catcher = f' Their catcher: catcher="{facts.catcher_hosts[0]}".' if facts.catcher_hosts else (
            " They have no catcher; leave it out unless they ask.")
        return Stage("choose_folder", "Ask only which folder to keep safe, suggesting Documents. On their answer, preview "
                     f"mirror_folder (source_host: their Mac from list_hosts; mirrors: {facts.mirror_hosts}).{catcher} "
                     "Give the one-line plan ending \"Start?\", and start it on their yes.", ("mirror_folder",),
                     ("Documents", "Another folder"), say="Which folder should Casper keep safe? Documents is a good start.")
    if facts.folder == "awaiting_consent":
        return Stage("consent", "A Casper dialog is waiting on their Mac. Say only: \"Click Allow in the Casper dialog on your "
                     "Mac.\" When they say it's done, check protection_status.", (), ("Done",),
                     say="Click Allow in the Casper dialog on your Mac.")
    if facts.folder == "active" and not facts.protected:
        return Stage("syncing", "The first copy is on its way. Check protection_status; say in one line how far along it is, "
                     "or \"Protected.\" once it is.")
    if facts.folder == "active" and not facts.recovery_kit and "recovery_kit" not in asked:
        return Stage("recovery_kit", "Say only: \"Protected. Last step: your recovery kit. In Casper's menu (the ghost in the "
                     "menu bar), choose Make Recovery Kit.\" If they ask why: without it, a lost Mac can't be rebuilt.", (), ("Done", "Later"),
                     say="Protected. Last step: your recovery kit. In Casper's menu (the ghost in the menu bar), choose Make Recovery Kit.")
    if inv and not facts.offers_space and "give_back" not in asked:
        return Stage("give_back", f"Ask only: \"Want to offer {inv['inviter']} space on your Mac too?\" It's a gift, never owed. If yes, invite {inv['inviter']} "
                     "(create_invite) the usual way.", ("publish_offering", "create_invite"), ("Yes", "No thanks"),
                     say=f"Want to offer {inv['inviter']} space on your Mac too?")
    return Stage("open", "Set up. Help with whatever they ask, one ask at a time.", ("*",))


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


def plain(text: str) -> str:
    """For Telegram, which shows formatting marks literally: drops Markdown
    the model used anyway (bold, code, headings) and turns [text](url) into
    "text: url"."""
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"\1: \2", text)
    text = re.sub(r"\*\*([^*]+)\*\*|__([^_]+)__", lambda m: m.group(1) or m.group(2), text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    return re.sub(r"(?m)^#{1,6}\s+", "", text)


def _for_model(messages: list[dict]) -> list[dict]:
    return [{k: v for k, v in m.items() if not k.startswith("_")} for m in messages]


def _entering(stage: Stage, history: list[dict]) -> bool:
    """Is this step new -- the guide's last word was from another step?"""
    last = next((m for m in reversed(history) if m.get("role") == "assistant" and m.get("_stage")), None)
    return last is None or last["_stage"] != stage.name


def _tools_for(stage: Stage | None) -> list[dict]:
    if stage is None or "*" in stage.tools:
        return TOOLS
    allowed = set(READ_ONLY) | set(stage.tools)
    return [t for t in TOOLS if t["function"]["name"] in allowed]


def run(client, model: str, services, user_id: int, history: list[dict], user_text: str, prompt: str,
        on_tool: Callable[[str, dict], None] | None = None,
        stage_fn: Callable[[list[dict]], Stage] | None = None) -> tuple[str, list[dict]]:
    """One person-turn: the model may call tools for several rounds before
    answering. Returns (reply, the new history including this turn); the
    final assistant message carries any buttons as "_choices" and its step
    as "_stage". stage_fn (messages -> Stage) gates the turn: the current
    step's instruction and only its tools, re-checked after every round so
    a step that just completed hands straight on to the next."""
    messages = list(history) + [{"role": "user", "content": user_text}]
    proposed = False
    previewed: set[str] = set()  # tools previewed in this turn: acting on one needs the person's answer first
    acted = False  # something changed this turn (a confirmed action)
    stage = None
    for _ in range(MAX_TOOL_ROUNDS):
        stage = stage_fn(messages) if stage_fn else None
        now = f"\nNOW ({stage.name}): {stage.now}\n" if stage else ""
        resp = client.chat.completions.create(model=model, messages=[{"role": "system", "content": prompt + now}] + _for_model(messages),
                                              tools=_tools_for(stage), temperature=0.2)
        msg = resp.choices[0].message
        calls = msg.tool_calls or []
        entry = {"role": "assistant", "content": msg.content or ""}
        if calls:
            entry["tool_calls"] = [{"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments or "{}"}} for c in calls]
        messages.append(entry)
        if not calls:
            reply = (msg.content or "").strip()
            if stage and stage.say and not proposed and _entering(stage, history) and "?" not in user_text:
                # A scripted step begins: Casper says its line. The model keeps
                # at most a short note on what just happened (e.g. "Accepted.").
                note = reply.split("\n")[0].strip() if acted else ""
                keep = note and "?" not in note and len(note) <= 90 and stage.say not in note
                note = note if keep else ("Done." if acted else "")
                reply = (note + "\n\n" if note else "") + stage.say
                entry["content"] = reply
            choices = choices_for(reply, proposed) or (list(stage.choices) if stage else [])
            if choices:
                entry["_choices"] = choices
            if stage:
                entry["_stage"] = stage.name
            return reply, messages
        for c in calls:
            try:
                args = json.loads(c.function.arguments or "{}")
            except ValueError:
                args = {}
            if on_tool:
                on_tool(c.function.name, args)
            if c.function.name in previewed and args.get("preview") is False:
                result = "Not done: you just showed this plan. Stop here and wait for the person's answer."
            elif c.function.name not in {t["function"]["name"] for t in _tools_for(stage)}:
                result = "Not now: that's not this step. Stay on the step in NOW."
            else:
                result = _call_tool(services, user_id, c.function.name, args)
            if result.startswith("PREVIEW"):
                proposed = True
                previewed.add(c.function.name)
            elif args.get("preview") is False:
                acted = True
            messages.append({"role": "tool", "tool_call_id": c.id, "content": result[:6000]})
    return "That didn't go through. Say it another way?", messages


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
