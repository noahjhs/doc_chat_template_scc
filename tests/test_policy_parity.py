"""Cross-implementation parity: auth_service/policy.py's match_policy vs.
agent/internal/commands/policy.go's matchPolicy are two independent
implementations of the same rule-matching semantics -- nothing else proves
they actually agree. Both this file and agent/internal/commands/
parity_test.go load the exact same tests/fixtures/policy_parity.json and
must reach the same verdict on every case. Deliberately excludes any
"{roots}" rule -- see the fixture's own top-level comment for why those
two implementations are not comparable there.

Tests only the core match step (an already-composed, flat rule list),
not composition itself (each implementation's own suite already covers
composing layers in id order) -- see policy.compose_policy/match_policy's
own docstrings."""

import json
import os
import sys

import pytest

AUTH_SERVICE_DIR = os.path.join(os.path.dirname(__file__), "..", "auth_service")
FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "policy_parity.json")


@pytest.fixture(scope="module")
def policy_module():
    sys.path.insert(0, os.path.abspath(AUTH_SERVICE_DIR))
    for mod in ("models", "policy"):
        sys.modules.pop(mod, None)
    import policy as policy_module

    yield policy_module
    sys.path.remove(os.path.abspath(AUTH_SERVICE_DIR))
    for mod in ("models", "policy"):
        sys.modules.pop(mod, None)


def _load_cases():
    with open(FIXTURE_PATH) as f:
        return json.load(f)["cases"]


@pytest.mark.parametrize("case", _load_cases(), ids=lambda c: c["name"])
def test_parity_case(policy_module, case):
    from models import PolicyLayerRuleInfo

    composed = [
        (
            i,
            PolicyLayerRuleInfo(
                id=i,
                position=i,
                positional_constraints=rule["positional_constraints"],
                option_constraints=rule["option_constraints"],
                tier=rule["tier"],
            ),
        )
        for i, rule in enumerate(case["rules"])
    ]
    matched_rule_index, matched_rule = policy_module.match_policy(
        composed, case["positional_args"], case["options"], roots=[]
    )
    tier = matched_rule.tier if matched_rule else "deny"
    assert tier == case["expected_tier"], f"{case['name']}: expected tier {case['expected_tier']!r}, got {tier!r}"
    assert matched_rule_index == case["expected_matched_rule_index"], (
        f"{case['name']}: expected matched rule index {case['expected_matched_rule_index']!r}, "
        f"got {matched_rule_index!r}"
    )
