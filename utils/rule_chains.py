"""Prose rendering of Rule Chain constraints, shared between pages/chat.py
(the run_rule_chain_call tool description, so the model sees a rule chain's
rules in plain English) and pages/resources.py (the rule-authoring UI's
read-only rule summaries) -- kept in one place so the model and the human
always see the exact same rendering of the exact same rule."""


def describe_pattern(pattern):
    """Prose rendering of one positional_constraints entry ("*", or a real
    {"whitelist":.., "blacklist":..} object) or an option's pattern field
    (which can also be None -- "must be present with no value")."""
    if pattern is None:
        return "no value"
    if pattern == "*":
        return "any value"
    parts = []
    if pattern.get("whitelist") == "{roots}":
        parts.append("must be inside one of this host's addressable directories")
    elif pattern.get("whitelist"):
        parts.append(f"must match {pattern['whitelist']!r}")
    if pattern.get("blacklist"):
        parts.append(f"must not match {pattern['blacklist']!r}")
    return ", ".join(parts) if parts else "unconstrained"


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
        lines.append(f"option {name}: {describe_pattern(opt.get('pattern'))}")
    body = "; ".join(lines) if lines else "(no constraints)"
    return f"tier={rule['tier']}: {body}"
