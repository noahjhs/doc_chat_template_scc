"""Prose rendering of Policy Layer rules, shared between pages/chat.py (the
run_shell_command tool description, so the model sees a host's effective
policy in plain English) and pages/resources.py (the policy-authoring UI's
read-only rule summaries) -- kept in one place so the model and the human
always see the exact same rendering of the exact same rule."""

# Must match auth_service/models.py's own BLACKLIST_MATCHES_NOTHING
# exactly -- duplicated rather than imported since auth_service is a
# separate, independently-deployed service with no shared package this
# app pulls from. A blank blacklist defaults to this ("matches nothing")
# rather than "" ("matches everything") specifically because a literal ""
# blacklist would reject every value, not accept every value the way an
# empty whitelist does -- see Pattern's own docstring.
BLACKLIST_MATCHES_NOTHING = r"[^\s\S]"


def describe_pattern(pattern):
    """Prose rendering of one {"whitelist":.., "blacklist":..} pattern --
    the one shape both a positional_constraints entry and an
    OptionConstraint's pattern take (no "*" sentinel anywhere). A missing
    value (a positional argument beyond what was supplied, or an option
    present with no value) is matched as "", so a blank pattern means
    "value not required" (an empty whitelist matches "" too, and the
    default blacklist can never match anything) and a whitelist of "^$"
    (matches only "") means "value not allowed"."""
    whitelist = pattern.get("whitelist") or ""
    blacklist = pattern.get("blacklist") or ""
    has_blacklist = blacklist and blacklist != BLACKLIST_MATCHES_NOTHING
    if whitelist == "^$" and not has_blacklist:
        return "value not allowed"
    parts = []
    if whitelist == "{roots}":
        parts.append("must be inside one of this host's addressable directories")
    elif whitelist:
        parts.append(f"must match {whitelist!r}")
    if has_blacklist:
        parts.append(f"must not match {blacklist!r}")
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
