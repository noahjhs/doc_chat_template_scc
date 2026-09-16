"""Prose rendering of Rule Chain constraints, shared between pages/chat.py
(the run_rule_chain_call tool description, so the model sees a rule chain's
rules in plain English) and pages/resources.py (the rule-authoring UI's
read-only rule summaries) -- kept in one place so the model and the human
always see the exact same rendering of the exact same rule."""


def describe_pattern(pattern):
    """Prose rendering of one {"whitelist":.., "blacklist":..} pattern --
    the one shape both a positional_constraints entry and an
    OptionConstraint's pattern take (no "*" sentinel anywhere). A missing
    value (a positional argument beyond what was supplied, or an option
    present with no value) is matched as "", so a blank pattern means
    "value not required" (an empty pattern matches "" too) and a
    whitelist of "^$" (matches only "") means "value not allowed"."""
    if pattern.get("whitelist") == "^$" and not pattern.get("blacklist"):
        return "value not allowed"
    parts = []
    if pattern.get("whitelist") == "{roots}":
        parts.append("must be inside one of this host's addressable directories")
    elif pattern.get("whitelist"):
        parts.append(f"must match {pattern['whitelist']!r}")
    if pattern.get("blacklist"):
        parts.append(f"must not match {pattern['blacklist']!r}")
    return ", ".join(parts) if parts else "value not required"


def describe_rule(rule):
    """One rule's constraints + tier in prose."""
    lines = []
    for i, pattern in enumerate(rule["positional_constraints"]):
        label = "binary" if i == 0 else f"position {i}"
        lines.append(f"{label}: {describe_pattern(pattern)}")
    for opt in rule["option_constraints"]:
        if opt.get("long") and opt.get("short"):
            name = f"--{opt['long']}/-{opt['short']}"
        elif opt.get("long"):
            name = f"--{opt['long']}"
        else:
            name = f"-{opt['short']}"
        lines.append(f"option {name}: {describe_pattern(opt.get('pattern') or {})}")
    body = "; ".join(lines) if lines else "(no constraints)"
    return f"tier={rule['tier']}: {body}"
