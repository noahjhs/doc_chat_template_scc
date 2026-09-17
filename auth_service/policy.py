"""The canonical Policy/Policy Layer rule-matching engine -- the server-side
source of truth for what a given (positional_args, options) call resolves
to under a composed Policy (concatenated Policy Layers). Mirrors the Go
daemon's own authoritative matcher (agent/internal/commands/policy.go)
exactly -- that daemon-side copy remains the one that's truly authoritative
for a real dispatched call (it alone has real filesystem access for
{roots} containment), but this is the canonical *server-side* copy: POST
/policies/eval evaluates purely against this, and the tool-dispatch tier
decision (once /conversations/step lands) will too. pages/chat.py still
carries its own client-side port of this same logic for now (used to
approximate the tier before a real call is dispatched) -- that copy is
slated for deletion once its callers move to /conversations/step."""

import os.path

import re2

from models import Pattern, PolicyLayerRuleInfo


def _value_in_roots(value: str, roots: list[str]) -> bool:
    """A plain string-prefix test against the supplied workspace roots -- no
    symlink/".."/case-sensitivity resolution (only the daemon, with real
    filesystem access to the target host, can check containment
    authoritatively -- see Pattern's own docstring on why a "{roots}" rule
    can never be tier "allow"). A relative value can't be resolved against a
    remote cwd from here either, so it's treated as possibly in-bounds
    whenever there's at least one root -- this can only make eval report
    "ask" where the daemon would actually allow, never the reverse."""
    if not value:
        return False
    if not os.path.isabs(value):
        return bool(roots)
    return any(value == root or value.startswith(root.rstrip("/") + "/") for root in roots)


def _pattern_matches(value: str, pattern: Pattern, roots: list[str]) -> bool:
    """Matches if (value matches whitelist) AND (value does NOT match
    blacklist) -- see Pattern's own docstring for the full three-state
    semantics this implements. Uses google-re2, not stdlib re, to preserve
    the no-catastrophic-backtracking property the whole schema is built
    around, since these patterns evaluate agent-influenced input."""
    if pattern.whitelist == "{roots}":
        if not _value_in_roots(value, roots):
            return False
    elif pattern.whitelist and not re2.search(pattern.whitelist, value):
        return False
    if pattern.blacklist and re2.search(pattern.blacklist, value):
        return False
    return True


def _find_option(options: list[dict], short: str | None, long: str | None) -> dict | None:
    for opt in options:
        if (short and opt.get("short") == short) or (long and opt.get("long") == long):
            return opt
    return None


def _rule_matches(rule: PolicyLayerRuleInfo, positional_args: list[str], options: list[dict], roots: list[str]) -> bool:
    """A positional_constraints entry beyond what was actually supplied is
    matched as "" -- same coercion the option loop below uses for a missing
    value -- so a blank pattern there means "value not required" with no
    separate sentinel needed."""
    for i, pattern in enumerate(rule.positional_constraints):
        value = positional_args[i] if i < len(positional_args) else ""
        if not _pattern_matches(value, pattern, roots):
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
        if not _pattern_matches(value, constraint.pattern, roots):
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
    roots: list[str],
) -> tuple[int | None, PolicyLayerRuleInfo | None]:
    """First-match-wins over an already-composed policy (see compose_policy
    above). Returns (None, None) -- terminal deny -- when nothing matches,
    including when the policy has no rules at all (e.g. zero layers
    supplied)."""
    for layer_id, rule in composed:
        if _rule_matches(rule, positional_args, options, roots):
            return layer_id, rule
    return None, None
