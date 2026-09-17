"""The canonical Policy/Policy Layer rule-matching engine -- the server-side
source of truth for what a given (positional_args, options) call resolves
to under a composed Policy (concatenated Policy Layers). Mirrors the Go
daemon's own authoritative matcher (agent/internal/commands/policy.go)
exactly -- that daemon-side copy remains the one that's truly authoritative
for a real dispatched call (it alone has real filesystem access to check
where a shell command's own working directory ends up), but this is the
canonical *server-side* copy: both POST /policies/eval and
conversations.py's own tool-dispatch tier decision evaluate purely against
this."""

import re2

from models import BLACKLIST_MATCHES_NOTHING, Pattern, PolicyLayerRuleInfo


def _pattern_matches(value: str, pattern: Pattern) -> bool:
    """Matches if (value matches whitelist) AND (value does NOT match
    blacklist) -- see Pattern's own docstring for the full three-state
    semantics this implements. Uses google-re2, not stdlib re, to preserve
    the no-catastrophic-backtracking property the whole schema is built
    around, since these patterns evaluate agent-influenced input."""
    if pattern.whitelist and not re2.search(pattern.whitelist, value):
        return False
    if pattern.blacklist and re2.search(pattern.blacklist, value):
        return False
    return True


def _find_option(options: list[dict], short: str | None, long: str | None) -> dict | None:
    for opt in options:
        if (short and opt.get("short") == short) or (long and opt.get("long") == long):
            return opt
    return None


def _rule_matches(rule: PolicyLayerRuleInfo, positional_args: list[str], options: list[dict]) -> bool:
    """A positional_constraints entry beyond what was actually supplied is
    matched as "" -- same coercion the option loop below uses for a missing
    value -- so a blank pattern there means "value not required" with no
    separate sentinel needed."""
    for i, pattern in enumerate(rule.positional_constraints):
        value = positional_args[i] if i < len(positional_args) else ""
        if not _pattern_matches(value, pattern):
            return False
    for constraint in rule.option_constraints:
        supplied = _find_option(options, constraint.short, constraint.long)
        if supplied is None:
            return False
        # A missing value is matched as "" -- see OptionConstraint's own
        # docstring for why this makes a whitelist of "^$" the way to
        # require no value, with no separate presence/absence check needed.
        value = supplied.get("value")
        if value is None:
            value = ""
        if not _pattern_matches(value, constraint.pattern):
            return False
    return True


def compose_policy(
    layers: list[tuple[int, list[PolicyLayerRuleInfo]]],
) -> list[tuple[int, PolicyLayerRuleInfo]]:
    """Concatenates every policy layer's rules into one ordered
    (layer_id, rule) list -- v1's whole composition rule (sort layers by id
    ascending, then concatenate their rules in order), mirroring the Go
    daemon's own composePolicy exactly. A rule is only ever evaluated as
    part of a policy, never a layer standalone -- carrying the originating
    layer_id alongside each rule is what lets a caller learn which layer
    actually decided a match, without match_policy needing to know
    anything about layers itself."""
    ordered = sorted(layers, key=lambda pair: pair[0])
    composed = []
    for layer_id, rules in ordered:
        composed.extend((layer_id, rule) for rule in rules)
    return composed


def match_policy(
    composed: list[tuple[int, PolicyLayerRuleInfo]],
    positional_args: list[str],
    options: list[dict],
) -> tuple[int | None, PolicyLayerRuleInfo | None]:
    """First-match-wins over an already-composed policy (see compose_policy
    above). Returns (None, None) -- terminal deny -- when nothing matches,
    including when the policy has no rules at all (e.g. zero layers
    supplied)."""
    for layer_id, rule in composed:
        if _rule_matches(rule, positional_args, options):
            return layer_id, rule
    return None, None


def describe_pattern(pattern: Pattern) -> str:
    """Prose rendering of one Pattern, built server-side from a real
    Pattern object -- used by conversations.py's run_shell_command tool
    description, so the model sees a host's effective policy in plain
    English. See Pattern's own docstring (models.py) for the three-state
    whitelist/blacklist semantics this renders."""
    has_blacklist = pattern.blacklist and pattern.blacklist != BLACKLIST_MATCHES_NOTHING
    if pattern.whitelist == "^$" and not has_blacklist:
        return "value not allowed"
    parts = []
    if pattern.whitelist:
        parts.append(f"must match {pattern.whitelist!r}")
    if has_blacklist:
        parts.append(f"must not match {pattern.blacklist!r}")
    return ", ".join(parts) if parts else "value not required"


def describe_rule(rule: PolicyLayerRuleInfo) -> str:
    """One rule's constraints + tier in prose -- see describe_pattern."""
    lines = []
    for i, pattern in enumerate(rule.positional_constraints):
        label = "binary" if i == 0 else f"position {i}"
        lines.append(f"{label}: {describe_pattern(pattern)}")
    for opt in rule.option_constraints:
        if opt.long and opt.short:
            name = f"--{opt.long}/-{opt.short}"
        elif opt.long:
            name = f"--{opt.long}"
        else:
            name = f"-{opt.short}"
        lines.append(f"option {name}: {describe_pattern(opt.pattern)}")
    body = "; ".join(lines) if lines else "(no constraints)"
    return f"tier={rule.tier}: {body}"
