#!/usr/bin/env python3
"""A readable transcript of a Claude Code session: just the conversation --
your messages and Claude's final reply to each -- without tool calls, code,
diffs, interim progress notes or system notices.

    tools/transcript.py                      the most recent session in this project
    tools/transcript.py SESSION.jsonl ...    specific sessions, oldest first
    tools/transcript.py --all                every session in this project
    -o FILE                                  where to write (default: ~/Documents/Casper conversation.md)

Sessions live in ~/.claude/projects/<this project>/*.jsonl."""

import json
import re
import sys
from datetime import datetime
from pathlib import Path

PROJECT = Path.home() / ".claude" / "projects" / str(Path(__file__).resolve().parent.parent).replace("/", "-").replace("_", "-")
SKIP_PREFIXES = ("<local-command", "<command-name>", "<command-message>", "<task-notification>", "[SYSTEM NOTIFICATION",
                 "Tool loaded.", "Caveat:")
SUMMARY_PREFIX = "This session is being continued from a previous conversation"


def _when(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%a %d %b %Y, %H:%M")
    except ValueError:
        return ts


def _clean_user(text: str) -> str:
    text = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.S)
    text = re.sub(r"\[Request interrupted by user[^\]]*\]", "", text)
    text = re.sub(r"<pasted_content[^>]*>(.*?)</pasted_content[^>]*>", r"\1", text, flags=re.S)
    return text.strip()


def turns(path: Path):
    """Yields ("you" | "claude" | "summary", timestamp, text), one Claude
    reply per turn: its last text before your next message."""
    pending = None  # Claude's latest text in the current turn
    for line in path.open():
        try:
            m = json.loads(line)
        except ValueError:
            continue
        kind = m.get("type")
        if kind == "user" and not m.get("isMeta"):
            content = m["message"]["content"]
            parts = [content] if isinstance(content, str) else [
                b.get("text", "") if b.get("type") == "text" else "[image]" if b.get("type") == "image" else None
                for b in content]
            results = [b for b in (content if isinstance(content, list) else []) if isinstance(b, dict) and b.get("type") == "tool_result"]
            if results:
                continue  # a tool's output, not you
            text = _clean_user("\n".join(p for p in parts if p))
            if not text or text.startswith(SKIP_PREFIXES):
                continue
            if pending:
                yield pending
                pending = None
            if text.startswith(SUMMARY_PREFIX):
                yield ("summary", m.get("timestamp", ""), "")
            else:
                yield ("you", m.get("timestamp", ""), text)
        elif kind == "attachment" and (m.get("attachment") or {}).get("type") == "queued_command" \
                and ((m["attachment"].get("origin") or {}).get("kind") == "human"):
            # Something you sent while Claude was mid-task.
            prompt = m["attachment"].get("prompt")
            said = prompt if isinstance(prompt, str) else " ".join(b.get("text", "") for b in prompt or [] if isinstance(b, dict))
            if _clean_user(said):
                yield ("you", m.get("timestamp", ""), "*(sent while Claude was working)* " + _clean_user(said))
        elif kind == "assistant":
            texts = [b["text"] for b in m["message"].get("content", []) if isinstance(b, dict) and b.get("type") == "text" and b["text"].strip()]
            if texts:
                pending = ("claude", m.get("timestamp", ""), texts[-1].strip())
    if pending:
        yield pending


def render(paths: list[Path]) -> str:
    out = ["# Casper: the conversation", "",
           f"Generated {datetime.now().strftime('%d %b %Y, %H:%M')} from {len(paths)} session(s). "
           "Only messages and final replies; tool calls and code changes are left out.", ""]
    for path in paths:
        out += ["", "---", f"## Session {path.stem[:8]}", ""]
        for who, ts, text in turns(path):
            if who == "summary":
                out += ["*(The conversation was summarized here to free up context; it continues below.)*", ""]
                continue
            out += [f"### {'You' if who == 'you' else 'Claude'} · {_when(ts)}", "", text, ""]
    return "\n".join(out)


def main(argv: list[str]) -> int:
    args = argv[1:]
    out = Path.home() / "Documents" / "Casper conversation.md"
    if "-o" in args:
        i = args.index("-o")
        out = Path(args[i + 1]).expanduser()
        del args[i:i + 2]
    sessions = sorted(PROJECT.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    if "--all" in args:
        paths = sessions
    elif args:
        paths = [Path(a).expanduser() for a in args]
    else:
        paths = sessions[-1:]
    if not paths:
        print(f"No sessions found in {PROJECT}")
        return 1
    out.write_text(render(paths))
    print(f"Wrote {out} ({sum(1 for p in paths for t in turns(p) if t[0] != 'summary')} messages)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
