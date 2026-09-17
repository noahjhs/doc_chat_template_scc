package commands

import (
	"encoding/json"
	"os"
	"path/filepath"
	"regexp"
	"testing"
)

// Cross-implementation parity: this package's own matchPolicy vs.
// auth_service/policy.py's match_policy are two independent
// implementations of the same rule-matching semantics -- nothing else
// proves they actually agree. Both this file and tests/test_policy_parity.py
// load the exact same tests/fixtures/policy_parity.json and must reach the
// same verdict on every case. Deliberately excludes any "{roots}" rule --
// see the fixture's own top-level comment for why those two
// implementations are not comparable there.
//
// Tests only the core match step (an already-composed, flat rule list, put
// into a single synthetic PolicyLayer so composePolicy's own layer-sorting
// never reorders it), not composition itself (each implementation's own
// suite already covers composing layers in id order).

type parityFixture struct {
	Cases []parityCase `json:"cases"`
}

type parityCase struct {
	Name                     string          `json:"name"`
	Rules                    []parityRule    `json:"rules"`
	PositionalArgs           []string        `json:"positional_args"`
	Options                  []RequestOption `json:"options"`
	ExpectedTier             string          `json:"expected_tier"`
	ExpectedMatchedRuleIndex *int            `json:"expected_matched_rule_index"`
}

type parityRule struct {
	PositionalConstraints []parityPattern          `json:"positional_constraints"`
	OptionConstraints     []parityOptionConstraint `json:"option_constraints"`
	Tier                  string                   `json:"tier"`
}

type parityPattern struct {
	Whitelist *string `json:"whitelist"`
	Blacklist *string `json:"blacklist"`
}

type parityOptionConstraint struct {
	Short   string        `json:"short"`
	Long    string        `json:"long"`
	Pattern parityPattern `json:"pattern"`
}

func (p parityPattern) compile(t *testing.T) Pattern {
	t.Helper()
	out := Pattern{}
	if p.Whitelist != nil && *p.Whitelist != "" {
		re, err := regexp.Compile(*p.Whitelist)
		if err != nil {
			t.Fatalf("compiling whitelist %q: %v", *p.Whitelist, err)
		}
		out.Whitelist = re
	}
	if p.Blacklist != nil && *p.Blacklist != "" {
		re, err := regexp.Compile(*p.Blacklist)
		if err != nil {
			t.Fatalf("compiling blacklist %q: %v", *p.Blacklist, err)
		}
		out.Blacklist = re
	}
	return out
}

func loadParityFixture(t *testing.T) parityFixture {
	t.Helper()
	// Repo root is three levels up from agent/internal/commands.
	path := filepath.Join("..", "..", "..", "tests", "fixtures", "policy_parity.json")
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("reading fixture: %v", err)
	}
	var fixture parityFixture
	if err := json.Unmarshal(data, &fixture); err != nil {
		t.Fatalf("parsing fixture: %v", err)
	}
	return fixture
}

func TestPolicyParityFixture(t *testing.T) {
	fixture := loadParityFixture(t)
	h, _ := newTestHandler(t)

	for _, c := range fixture.Cases {
		t.Run(c.Name, func(t *testing.T) {
			rules := make([]Rule, len(c.Rules))
			for i, rw := range c.Rules {
				positional := make([]Pattern, len(rw.PositionalConstraints))
				for j, pw := range rw.PositionalConstraints {
					positional[j] = pw.compile(t)
				}
				options := make([]OptionConstraint, len(rw.OptionConstraints))
				for j, ow := range rw.OptionConstraints {
					options[j] = OptionConstraint{Short: ow.Short, Long: ow.Long, Pattern: ow.Pattern.compile(t)}
				}
				rules[i] = Rule{ID: i, PositionalConstraints: positional, OptionConstraints: options, Tier: rw.Tier}
			}
			layers := []PolicyLayer{{ID: 0, Rules: rules}}

			matched := h.matchPolicy(layers, c.PositionalArgs, c.Options)

			tier := "deny"
			if matched != nil {
				tier = matched.Tier
			}
			if tier != c.ExpectedTier {
				t.Errorf("expected tier %q, got %q", c.ExpectedTier, tier)
			}

			if c.ExpectedMatchedRuleIndex == nil {
				if matched != nil {
					t.Errorf("expected no match, but rule %d matched", matched.ID)
				}
			} else {
				if matched == nil {
					t.Errorf("expected rule %d to match, but nothing matched", *c.ExpectedMatchedRuleIndex)
				} else if matched.ID != *c.ExpectedMatchedRuleIndex {
					t.Errorf("expected matched rule index %d, got %d", *c.ExpectedMatchedRuleIndex, matched.ID)
				}
			}
		})
	}
}
