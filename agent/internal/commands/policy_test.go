package commands

import (
	"os"
	"regexp"
	"testing"
)

func mustWhitelist(pattern string) Pattern {
	return Pattern{Whitelist: regexp.MustCompile(pattern)}
}

// unconstrained is a blank pattern -- "value not required" -- used
// throughout these tests as a convenient "match anything at this
// position" filler.
func unconstrained() Pattern {
	return Pattern{}
}

// valueNotAllowed matches only an empty string -- see Pattern's own doc
// comment for why this is how a rule requires an argument or option to
// have NO value.
func valueNotAllowed() Pattern {
	return mustWhitelist("^$")
}

func rootsWhitelist() Pattern {
	return Pattern{WhitelistRoots: true}
}

func TestRuleMatches_Positional(t *testing.T) {
	h, _ := newTestHandler(t)
	rule := Rule{
		PositionalConstraints: []Pattern{mustWhitelist("^echo$"), mustWhitelist("^hello$")},
		Tier:                  "allow",
	}
	if !h.ruleMatches(rule, []string{"echo", "hello"}, nil) {
		t.Fatal("expected a match")
	}
	if h.ruleMatches(rule, []string{"echo", "goodbye"}, nil) {
		t.Fatal("expected no match -- position 1 doesn't satisfy the pattern")
	}
	if h.ruleMatches(rule, []string{"echo"}, nil) {
		t.Fatal("expected no match -- position 1 has a real constraint but nothing was supplied")
	}
}

func TestRuleMatches_EmptyPositionalConstraintsMatchAnyBinary(t *testing.T) {
	// Position 0 (the binary) is optional, same as every other position --
	// an empty list is a fully legitimate "unconstrained" rule, not a
	// degenerate/invalid one; it simply never enters the loop in
	// ruleMatches, so any positional_args (including any binary) satisfy
	// it, same as any position beyond a shorter list already does.
	h, _ := newTestHandler(t)
	rule := Rule{Tier: "allow"}
	if !h.ruleMatches(rule, []string{"echo", "hello"}, nil) {
		t.Fatal("expected a match -- no positional constraints at all")
	}
	if !h.ruleMatches(rule, []string{"rm", "-rf", "/"}, nil) {
		t.Fatal("expected a match -- still no positional constraints, regardless of the binary")
	}
}

func TestRuleMatches_UnconstrainedEarlyPositionDoesntBlockALaterOne(t *testing.T) {
	h, _ := newTestHandler(t)
	rule := Rule{
		PositionalConstraints: []Pattern{unconstrained(), unconstrained(), mustWhitelist("^world$")},
		Tier:                  "allow",
	}
	if !h.ruleMatches(rule, []string{"echo", "hello", "world"}, nil) {
		t.Fatal("expected a match -- positions 0/1 accept anything, only position 2 is real")
	}
	if h.ruleMatches(rule, []string{"echo", "hello", "there"}, nil) {
		t.Fatal("expected no match -- position 2 fails")
	}
	// Positions 0/1 don't even need to be SUPPLIED, not just any value --
	// same coercion-to-"" the option loop uses.
	if h.ruleMatches(rule, []string{"echo"}, nil) {
		t.Fatal("expected no match -- position 2 still has nothing to check against")
	}
}

func TestRuleMatches_MissingPositionNeverSatisfiesRoots(t *testing.T) {
	// End-to-end version of TestValueMatchesPattern_RootsEmptyValueNeverMatchesEvenWithRealRoots,
	// through the actual ruleMatches loop that does the "missing position
	// coerced to \"\"" step.
	h, _ := newTestHandler(t)
	rule := Rule{PositionalConstraints: []Pattern{mustWhitelist("^cat$"), rootsWhitelist()}, Tier: "ask"}
	if h.ruleMatches(rule, []string{"cat"}, nil) {
		t.Fatal("expected no match -- position 1 (a real {roots} constraint) was never supplied")
	}
}

func TestRuleMatches_PositionalValueNotAllowed(t *testing.T) {
	// There's no separate "must not be present" concept for positional
	// args distinct from "no value" the way there is for options -- a
	// positional value that's absent is matched as "" exactly like a
	// valueless option, so "^$" works the same way here too: it requires
	// the position be either absent or an explicit empty string.
	h, _ := newTestHandler(t)
	rule := Rule{
		PositionalConstraints: []Pattern{mustWhitelist("^rm$"), valueNotAllowed()},
		Tier:                  "deny",
	}
	if !h.ruleMatches(rule, []string{"rm"}, nil) {
		t.Fatal("expected a match -- position 1 wasn't supplied at all")
	}
	if !h.ruleMatches(rule, []string{"rm", ""}, nil) {
		t.Fatal("expected a match -- position 1 was explicitly supplied as an empty string")
	}
	if h.ruleMatches(rule, []string{"rm", "-rf"}, nil) {
		t.Fatal("expected no match -- position 1 has a real value, which \"^$\" forbids")
	}
}

func TestRuleMatches_OptionEmptyStringWhitelistRequiresNoValue(t *testing.T) {
	// There's no separate nil/no-value state anymore -- a missing value is
	// matched as "", so a whitelist of "^$" (matches only the empty
	// string) is how a rule requires an option be present with NO value.
	h, _ := newTestHandler(t)
	rule := Rule{
		PositionalConstraints: []Pattern{unconstrained()},
		OptionConstraints:     []OptionConstraint{{Long: "force", Pattern: mustWhitelist("^$")}},
	}
	noValue := "no"
	if !h.ruleMatches(rule, []string{"echo"}, []RequestOption{{Long: "force", Value: nil}}) {
		t.Fatal("expected a match -- option present with no value, matched as \"\"")
	}
	if h.ruleMatches(rule, []string{"echo"}, []RequestOption{{Long: "force", Value: &noValue}}) {
		t.Fatal("expected no match -- a value was supplied but the constraint requires none")
	}
	if h.ruleMatches(rule, []string{"echo"}, nil) {
		t.Fatal("expected no match -- option not present at all")
	}
}

func TestRuleMatches_OptionBlankPatternAcceptsAnyOrNoValue(t *testing.T) {
	h, _ := newTestHandler(t)
	rule := Rule{
		PositionalConstraints: []Pattern{unconstrained()},
		OptionConstraints:     []OptionConstraint{{Short: "v", Pattern: Pattern{}}},
	}
	anything := "anything"
	if !h.ruleMatches(rule, []string{"echo"}, []RequestOption{{Short: "v", Value: nil}}) {
		t.Fatal("expected a match with no value")
	}
	if !h.ruleMatches(rule, []string{"echo"}, []RequestOption{{Short: "v", Value: &anything}}) {
		t.Fatal("expected a match with any value")
	}
}

func TestRuleMatches_OptionValuePattern(t *testing.T) {
	h, _ := newTestHandler(t)
	rule := Rule{
		PositionalConstraints: []Pattern{unconstrained()},
		OptionConstraints:     []OptionConstraint{{Long: "format", Pattern: mustWhitelist("^json$")}},
	}
	jsonValue := "json"
	xmlValue := "xml"
	if !h.ruleMatches(rule, []string{"echo"}, []RequestOption{{Long: "format", Value: &jsonValue}}) {
		t.Fatal("expected a match -- value satisfies the pattern")
	}
	if h.ruleMatches(rule, []string{"echo"}, []RequestOption{{Long: "format", Value: &xmlValue}}) {
		t.Fatal("expected no match -- value doesn't satisfy the pattern")
	}
	if h.ruleMatches(rule, []string{"echo"}, []RequestOption{{Long: "format", Value: nil}}) {
		t.Fatal("expected no match -- a missing value is matched as \"\", which doesn't satisfy \"^json$\" either")
	}
}

func TestComposePolicy_ConcatenatesInLayerIDOrder(t *testing.T) {
	// Deliberately supplied out of ID order, to confirm composePolicy sorts
	// by ID itself rather than trusting caller order (see its own doc
	// comment -- v1's whole composition rule is "just concatenate them").
	layers := []PolicyLayer{
		{ID: 2, Rules: []Rule{{ID: 20, Tier: "ask"}}},
		{ID: 1, Rules: []Rule{{ID: 10, Tier: "allow"}, {ID: 11, Tier: "deny"}}},
	}
	rules := composePolicy(layers)
	if len(rules) != 3 {
		t.Fatalf("expected 3 rules, got %d", len(rules))
	}
	got := []int{rules[0].ID, rules[1].ID, rules[2].ID}
	want := []int{10, 11, 20}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("expected rule order %v (layer 1's rules, then layer 2's), got %v", want, got)
		}
	}
}

func TestMatchPolicy_FirstMatchWinsAcrossLayers(t *testing.T) {
	// A rule is only ever evaluated as part of a policy, never a layer
	// standalone -- confirms a call is checked against every attached
	// layer's rules composed together, not just one layer the caller
	// happens to pick.
	h, _ := newTestHandler(t)
	layers := []PolicyLayer{
		{ID: 1, Rules: []Rule{{ID: 1, PositionalConstraints: []Pattern{mustWhitelist("^echo$")}, Tier: "ask"}}},
		{ID: 2, Rules: []Rule{{ID: 2, PositionalConstraints: []Pattern{unconstrained()}, Tier: "allow"}}},
	}
	rule := h.matchPolicy(layers, []string{"echo"}, nil)
	if rule == nil || rule.ID != 1 {
		t.Fatalf("expected the first (more specific) rule, from layer 1, to win, got %+v", rule)
	}
	// A binary layer 1 doesn't recognize still matches, via layer 2's
	// unconstrained rule -- proof both layers are actually composed, not
	// just the first one checked.
	rule = h.matchPolicy(layers, []string{"git"}, nil)
	if rule == nil || rule.ID != 2 {
		t.Fatalf("expected layer 2's rule to win for a binary layer 1 doesn't match, got %+v", rule)
	}
}

func TestMatchPolicy_TerminalDenyWhenNothingMatches(t *testing.T) {
	h, _ := newTestHandler(t)
	layers := []PolicyLayer{
		{ID: 1, Rules: []Rule{{PositionalConstraints: []Pattern{mustWhitelist("^git$")}, Tier: "allow"}}},
	}
	if h.matchPolicy(layers, []string{"echo"}, nil) != nil {
		t.Fatal("expected no match (terminal deny)")
	}
}

func TestMatchPolicy_NoLayersMeansTerminalDeny(t *testing.T) {
	h, _ := newTestHandler(t)
	if h.matchPolicy(nil, []string{"echo"}, nil) != nil {
		t.Fatal("expected no match -- zero layers attached composes to zero rules")
	}
}

func TestValueMatchesPattern_Roots(t *testing.T) {
	h, root := newTestHandler(t)
	inside := root + "/inside.txt"
	if err := os.WriteFile(inside, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	if !h.valueMatchesPattern(inside, rootsWhitelist()) {
		t.Fatal("expected a path inside a root to satisfy {roots}")
	}
	if h.valueMatchesPattern("/etc/passwd", rootsWhitelist()) {
		t.Fatal("expected a path outside every root to fail {roots}")
	}
}

func TestValueMatchesPattern_RootsEmptyNeverMatches(t *testing.T) {
	h := New(nil, nil) // zero roots
	if h.valueMatchesPattern("/anything", rootsWhitelist()) {
		t.Fatal("expected {roots} to never match with zero roots")
	}
}

func TestValueMatchesPattern_RootsEmptyValueNeverMatchesEvenWithRealRoots(t *testing.T) {
	// Regression test: valueInRoots used to treat an empty value as "use
	// cwd instead" (mirroring how an empty req.Path means "use cwd" for
	// other path-taking actions) -- harmless before ruleMatches always
	// required real presence for a non-wildcard positional constraint, but
	// once a missing positional value started being coerced to "" (so a
	// blank pattern can mean "value not required" without its own
	// sentinel), that same coercion reaching a REAL "{roots}" pattern
	// would silently resolve to the cwd and match -- letting a rule
	// requiring "position N must be a real path inside roots" pass when
	// position N was never supplied at all. cwd is always inside roots by
	// the daemon's own invariant, so this needs an actual root configured
	// to catch a regression back to the old defaulting behavior.
	h, _ := newTestHandler(t)
	if h.valueMatchesPattern("", rootsWhitelist()) {
		t.Fatal("expected an empty (missing) value to never satisfy {roots}, even with a real root configured")
	}
}

func TestValueMatchesPattern_RootsWithBlacklistChecksRawValue(t *testing.T) {
	h, root := newTestHandler(t)
	blocked := root + "/blocked.txt"
	if err := os.WriteFile(blocked, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	p := rootsWhitelist()
	p.Blacklist = regexp.MustCompile("blocked")
	if h.valueMatchesPattern(blocked, p) {
		t.Fatal("expected the blacklist (checked against the raw value) to reject this even though it's inside roots")
	}
}

func TestBuildArgv(t *testing.T) {
	value := "output.txt"
	argv := buildArgv([]string{"extra"}, []RequestOption{
		{Long: "output", Value: &value},
		{Short: "f", Value: nil},
	})
	expected := []string{"--output", "output.txt", "-f", "extra"}
	if len(argv) != len(expected) {
		t.Fatalf("expected %v, got %v", expected, argv)
	}
	for i := range expected {
		if argv[i] != expected[i] {
			t.Fatalf("expected %v, got %v", expected, argv)
		}
	}
}

func TestRunShellCommand_Succeeds(t *testing.T) {
	h, _ := newTestHandler(t)
	h.SetPolicyLayers([]PolicyLayer{{
		ID: 1,
		Rules: []Rule{{
			ID:                    1,
			PositionalConstraints: []Pattern{mustWhitelist("^echo$"), mustWhitelist("^hello$")},
			Tier:                  "allow",
		}},
	}})

	res, err := h.Dispatch(&Request{Action: "run_shell_command", PositionalArgs: []string{"echo", "hello"}})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !res.Success || res.Stdout != "hello" {
		t.Fatalf("expected success with stdout %q, got %+v", "hello", res)
	}
}

func TestRunShellCommand_ComposesEveryAttachedLayer(t *testing.T) {
	// End-to-end version of TestMatchPolicy_FirstMatchWinsAcrossLayers,
	// through Dispatch -- there's no layer ID in the request at all, so
	// this also confirms the action genuinely doesn't need one.
	h, _ := newTestHandler(t)
	h.SetPolicyLayers([]PolicyLayer{
		{ID: 1, Rules: []Rule{{PositionalConstraints: []Pattern{mustWhitelist("^git$")}, Tier: "allow"}}},
		{ID: 2, Rules: []Rule{{PositionalConstraints: []Pattern{mustWhitelist("^echo$")}, Tier: "allow"}}},
	})
	res, err := h.Dispatch(&Request{Action: "run_shell_command", PositionalArgs: []string{"echo", "hi"}})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !res.Success {
		t.Fatalf("expected success -- layer 2's rule should have matched, got %+v", res)
	}
}

func TestRunShellCommand_RejectsWhenNoRuleMatches(t *testing.T) {
	h, _ := newTestHandler(t)
	h.SetPolicyLayers([]PolicyLayer{{ID: 1, Rules: []Rule{
		{PositionalConstraints: []Pattern{mustWhitelist("^git$")}, Tier: "allow"},
	}}})
	if _, err := h.Dispatch(&Request{Action: "run_shell_command", PositionalArgs: []string{"rm", "-rf", "/"}}); err == nil {
		t.Fatal("expected an ActionError -- no rule matches, terminal deny")
	}
}

func TestRunShellCommand_NoLayersAttachedMeansTerminalDeny(t *testing.T) {
	h, _ := newTestHandler(t)
	if _, err := h.Dispatch(&Request{Action: "run_shell_command", PositionalArgs: []string{"echo"}}); err == nil {
		t.Fatal("expected an ActionError -- zero policy layers attached composes to zero rules, terminal deny")
	}
}

func TestRunShellCommand_RequiresAtLeastTheBinary(t *testing.T) {
	h, _ := newTestHandler(t)
	h.SetPolicyLayers([]PolicyLayer{{ID: 1, Rules: []Rule{{PositionalConstraints: []Pattern{unconstrained()}, Tier: "allow"}}}})
	if _, err := h.Dispatch(&Request{Action: "run_shell_command", PositionalArgs: []string{}}); err == nil {
		t.Fatal("expected an ActionError -- positional_args must include at least the binary")
	}
}

func TestRunShellCommand_NoRootsMeansFail(t *testing.T) {
	h := New(nil, nil)
	h.SetPolicyLayers([]PolicyLayer{{ID: 1, Rules: []Rule{{PositionalConstraints: []Pattern{unconstrained()}, Tier: "allow"}}}})
	res, err := h.Dispatch(&Request{Action: "run_shell_command", PositionalArgs: []string{"echo"}})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if res.Success {
		t.Fatal("expected run_shell_command to fail with zero roots, same as every other confined action")
	}
}

func TestRunShellCommand_PathRedirectsDirectoryUnconditionally(t *testing.T) {
	h, root := newTestHandler(t)
	subdir := root + "/subdir"
	if err := os.Mkdir(subdir, 0o755); err != nil {
		t.Fatal(err)
	}
	h.SetPolicyLayers([]PolicyLayer{{ID: 1, Rules: []Rule{{PositionalConstraints: []Pattern{unconstrained()}, Tier: "allow"}}}})

	res, err := h.Dispatch(&Request{
		Action: "run_shell_command", PositionalArgs: []string{"echo", "hi"}, Path: subdir,
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if res.Cwd != subdir {
		t.Fatalf("expected the command to run in %q, got %q", subdir, res.Cwd)
	}
}

func TestListPolicyLayers(t *testing.T) {
	h, _ := newTestHandler(t)
	empty, err := h.Dispatch(&Request{Action: "list_policy_layers"})
	if err != nil || empty.Stdout != "No policy layers enabled on this host." {
		t.Fatalf("expected the empty message, got %q (err=%v)", empty.Stdout, err)
	}

	h.SetPolicyLayers([]PolicyLayer{
		{ID: 1, Name: "test", Rules: []Rule{{PositionalConstraints: []Pattern{unconstrained()}, Tier: "allow"}}},
	})
	res, err := h.Dispatch(&Request{Action: "list_policy_layers"})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if res.Stdout == "" || res.Stdout == "No policy layers enabled on this host." {
		t.Fatalf("expected a non-empty listing, got %q", res.Stdout)
	}
}
