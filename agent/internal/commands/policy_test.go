package commands

import (
	"encoding/json"
	"os"
	"os/exec"
	"regexp"
	"testing"
)

func mustWhitelist(pattern string) Pattern {
	return Pattern{Whitelist: regexp.MustCompile(pattern)}
}

// mustResolvedBinaryWhitelist builds an exact-match whitelist against name's
// own $PATH-resolved absolute path -- what a position-0 constraint must
// match against now that runRunShellCommand always resolves the binary
// before matching (see Rule's own doc comment), rather than the bare name a
// pre-resolution rule could match directly.
func mustResolvedBinaryWhitelist(t *testing.T, name string) Pattern {
	t.Helper()
	resolved, err := exec.LookPath(name)
	if err != nil {
		t.Skipf("%s not found on $PATH -- skipping", name)
	}
	return mustWhitelist("^" + regexp.QuoteMeta(resolved) + "$")
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

func TestRuleMatches_Positional(t *testing.T) {
	h, _ := newTestHandler(t)
	rule := Rule{
		PositionalConstraints: []Pattern{mustWhitelist("^echo$"), mustWhitelist("^hello$")},
		Tier:                  "allow",
	}
	if !h.ruleMatches(rule, []string{"echo", "hello"}, nil, "") {
		t.Fatal("expected a match")
	}
	if h.ruleMatches(rule, []string{"echo", "goodbye"}, nil, "") {
		t.Fatal("expected no match -- position 1 doesn't satisfy the pattern")
	}
	if h.ruleMatches(rule, []string{"echo"}, nil, "") {
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
	if !h.ruleMatches(rule, []string{"echo", "hello"}, nil, "") {
		t.Fatal("expected a match -- no positional constraints at all")
	}
	if !h.ruleMatches(rule, []string{"rm", "-rf", "/"}, nil, "") {
		t.Fatal("expected a match -- still no positional constraints, regardless of the binary")
	}
}

func TestRuleMatches_UnconstrainedEarlyPositionDoesntBlockALaterOne(t *testing.T) {
	h, _ := newTestHandler(t)
	rule := Rule{
		PositionalConstraints: []Pattern{unconstrained(), unconstrained(), mustWhitelist("^world$")},
		Tier:                  "allow",
	}
	if !h.ruleMatches(rule, []string{"echo", "hello", "world"}, nil, "") {
		t.Fatal("expected a match -- positions 0/1 accept anything, only position 2 is real")
	}
	if h.ruleMatches(rule, []string{"echo", "hello", "there"}, nil, "") {
		t.Fatal("expected no match -- position 2 fails")
	}
	// Positions 0/1 don't even need to be SUPPLIED, not just any value --
	// same coercion-to-"" the option loop uses.
	if h.ruleMatches(rule, []string{"echo"}, nil, "") {
		t.Fatal("expected no match -- position 2 still has nothing to check against")
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
	if !h.ruleMatches(rule, []string{"rm"}, nil, "") {
		t.Fatal("expected a match -- position 1 wasn't supplied at all")
	}
	if !h.ruleMatches(rule, []string{"rm", ""}, nil, "") {
		t.Fatal("expected a match -- position 1 was explicitly supplied as an empty string")
	}
	if h.ruleMatches(rule, []string{"rm", "-rf"}, nil, "") {
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
	if !h.ruleMatches(rule, []string{"echo"}, []RequestOption{{Long: "force", Value: nil}}, "") {
		t.Fatal("expected a match -- option present with no value, matched as \"\"")
	}
	if h.ruleMatches(rule, []string{"echo"}, []RequestOption{{Long: "force", Value: &noValue}}, "") {
		t.Fatal("expected no match -- a value was supplied but the constraint requires none")
	}
	if h.ruleMatches(rule, []string{"echo"}, nil, "") {
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
	if !h.ruleMatches(rule, []string{"echo"}, []RequestOption{{Short: "v", Value: nil}}, "") {
		t.Fatal("expected a match with no value")
	}
	if !h.ruleMatches(rule, []string{"echo"}, []RequestOption{{Short: "v", Value: &anything}}, "") {
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
	if !h.ruleMatches(rule, []string{"echo"}, []RequestOption{{Long: "format", Value: &jsonValue}}, "") {
		t.Fatal("expected a match -- value satisfies the pattern")
	}
	if h.ruleMatches(rule, []string{"echo"}, []RequestOption{{Long: "format", Value: &xmlValue}}, "") {
		t.Fatal("expected no match -- value doesn't satisfy the pattern")
	}
	if h.ruleMatches(rule, []string{"echo"}, []RequestOption{{Long: "format", Value: nil}}, "") {
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
	got := []int{rules[0].Rule.ID, rules[1].Rule.ID, rules[2].Rule.ID}
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
	layerID, rule := h.matchPolicy(layers, []string{"echo"}, nil, "")
	if rule == nil || rule.ID != 1 || layerID != 1 {
		t.Fatalf("expected the first (more specific) rule, from layer 1, to win, got %+v (layer %d)", rule, layerID)
	}
	// A binary layer 1 doesn't recognize still matches, via layer 2's
	// unconstrained rule -- proof both layers are actually composed, not
	// just the first one checked.
	layerID, rule = h.matchPolicy(layers, []string{"git"}, nil, "")
	if rule == nil || rule.ID != 2 || layerID != 2 {
		t.Fatalf("expected layer 2's rule to win for a binary layer 1 doesn't match, got %+v (layer %d)", rule, layerID)
	}
}

func TestMatchPolicy_TerminalDenyWhenNothingMatches(t *testing.T) {
	h, _ := newTestHandler(t)
	layers := []PolicyLayer{
		{ID: 1, Rules: []Rule{{PositionalConstraints: []Pattern{mustWhitelist("^git$")}, Tier: "allow"}}},
	}
	if layerID, rule := h.matchPolicy(layers, []string{"echo"}, nil, ""); rule != nil || layerID != 0 {
		t.Fatal("expected no match (terminal deny)")
	}
}

func TestMatchPolicy_NoLayersMeansTerminalDeny(t *testing.T) {
	h, _ := newTestHandler(t)
	if layerID, rule := h.matchPolicy(nil, []string{"echo"}, nil, ""); rule != nil || layerID != 0 {
		t.Fatal("expected no match -- zero layers attached composes to zero rules")
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
			PositionalConstraints: []Pattern{mustResolvedBinaryWhitelist(t, "echo"), mustWhitelist("^hello$")},
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
		{ID: 1, Rules: []Rule{{PositionalConstraints: []Pattern{mustResolvedBinaryWhitelist(t, "git")}, Tier: "allow"}}},
		{ID: 2, Rules: []Rule{{PositionalConstraints: []Pattern{mustResolvedBinaryWhitelist(t, "echo")}, Tier: "allow"}}},
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
	// No rule matched at all is now a normal Tier:"deny" verdict (HTTP 200
	// via handleCommand), not an ActionError -- the daemon is the sole
	// decider of allow/ask/deny now, so the caller must only ever consult
	// Tier, never separately distinguish "nothing matched" from "a rule
	// matched with its own tier deny".
	h, _ := newTestHandler(t)
	h.SetPolicyLayers([]PolicyLayer{{ID: 1, Rules: []Rule{
		{PositionalConstraints: []Pattern{mustWhitelist("^git$")}, Tier: "allow"},
	}}})
	res, err := h.Dispatch(&Request{Action: "run_shell_command", PositionalArgs: []string{"rm", "-rf", "/"}})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if res.Tier != "deny" || res.Success || res.MatchedLayerID != 0 || res.MatchedRuleID != 0 {
		t.Fatalf("expected Tier:\"deny\" with no execution and no matched IDs, got %+v", res)
	}
}

func TestRunShellCommand_NoLayersAttachedMeansTerminalDeny(t *testing.T) {
	h, _ := newTestHandler(t)
	res, err := h.Dispatch(&Request{Action: "run_shell_command", PositionalArgs: []string{"echo"}})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if res.Tier != "deny" {
		t.Fatalf("expected Tier:\"deny\" -- zero policy layers attached composes to zero rules, got %+v", res)
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
	h := New("")
	h.SetPolicyLayers([]PolicyLayer{{ID: 1, Rules: []Rule{{PositionalConstraints: []Pattern{unconstrained()}, Tier: "allow"}}}})
	res, err := h.Dispatch(&Request{Action: "run_shell_command", PositionalArgs: []string{"echo"}})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if res.Success {
		t.Fatal("expected run_shell_command to fail with no homeRoot configured, same as every other confined action")
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

func TestRunShellCommand_BareBinaryNameNoLongerMatchesUnresolved(t *testing.T) {
	// The security property Phase 1 adds: a rule constraining position 0
	// must match the $PATH-RESOLVED absolute path, not the bare name --
	// confirms a bare "^echo$" whitelist (yesterday's correct way to author
	// this) now correctly fails to match, rather than silently matching
	// against the pre-resolution raw string.
	h, _ := newTestHandler(t)
	h.SetPolicyLayers([]PolicyLayer{{ID: 1, Rules: []Rule{
		{PositionalConstraints: []Pattern{mustWhitelist("^echo$")}, Tier: "allow"},
	}}})
	res, err := h.Dispatch(&Request{Action: "run_shell_command", PositionalArgs: []string{"echo", "hi"}})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if res.Tier != "deny" {
		t.Fatalf("expected Tier:\"deny\" -- the bare name no longer matches the resolved absolute path, got %+v", res)
	}
}

func TestRunShellCommand_UnknownBinaryIsRejected(t *testing.T) {
	h, _ := newTestHandler(t)
	h.SetPolicyLayers([]PolicyLayer{{ID: 1, Rules: []Rule{{PositionalConstraints: []Pattern{unconstrained()}, Tier: "allow"}}}})
	if _, err := h.Dispatch(&Request{Action: "run_shell_command", PositionalArgs: []string{"definitely-not-a-real-binary-xyz"}}); err == nil {
		t.Fatal("expected an ActionError -- the binary doesn't resolve on $PATH")
	}
}

func TestRunShellCommand_CwdConstraint(t *testing.T) {
	h, root := newTestHandler(t)
	subdir := root + "/subdir"
	if err := os.Mkdir(subdir, 0o755); err != nil {
		t.Fatal(err)
	}
	h.SetPolicyLayers([]PolicyLayer{{ID: 1, Rules: []Rule{{
		PositionalConstraints: []Pattern{unconstrained()},
		Cwd:                   mustWhitelist("^" + regexp.QuoteMeta(subdir) + "$"),
		Tier:                  "allow",
	}}}})

	// The rule requires cwd == subdir -- the default root doesn't satisfy
	// it, so this is denied even though the positional constraint (the
	// only other constraint) is wide open.
	res, err := h.Dispatch(&Request{Action: "run_shell_command", PositionalArgs: []string{"echo"}})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if res.Tier != "deny" {
		t.Fatalf("expected Tier:\"deny\" -- cwd is the root, not subdir, got %+v", res)
	}
	// Redirecting into subdir via Path satisfies the Cwd constraint.
	res, err = h.Dispatch(&Request{Action: "run_shell_command", PositionalArgs: []string{"echo"}, Path: subdir})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !res.Success {
		t.Fatalf("expected success once cwd matches the Cwd constraint, got %+v", res)
	}
}

func TestValueMatchesPatternResolved_DotResolvesSymlinks(t *testing.T) {
	h, root := newTestHandler(t)
	secret := root + "/secret"
	if err := os.Mkdir(secret, 0o755); err != nil {
		t.Fatal(err)
	}
	link := root + "/safe-looking-link"
	if err := os.Symlink(secret, link); err != nil {
		t.Fatal(err)
	}
	// A blacklist on the real (symlink-resolved) target, checked against
	// the SYMLINK's own (safe-looking) name -- would pass under a raw
	// string match, but PathResolutionDot resolves it to the real target
	// first, so the blacklist correctly catches it.
	rule := Rule{
		PositionalConstraints: []Pattern{
			unconstrained(),
			{Blacklist: mustWhitelist("^" + regexp.QuoteMeta(secret)).Whitelist, PathResolution: PathResolutionDot},
		},
		Tier: "deny",
	}
	if h.ruleMatches(rule, []string{"cat", "safe-looking-link"}, nil, root) {
		t.Fatal("expected no match -- the symlink resolves into the blacklisted target")
	}
}

func TestValueMatchesPatternResolved_DotFailsClosedOnUnresolvable(t *testing.T) {
	h, root := newTestHandler(t)
	// A symlink cycle makes EvalSymlinks itself fail ("too many links") --
	// realpath() otherwise happily resolves a not-yet-existing target, by
	// design (see its own doc comment), so a merely nonexistent path isn't
	// itself a resolution failure; a genuine cycle is one of the few ways
	// to force a real error out of it.
	a, b := root+"/a", root+"/b"
	if err := os.Symlink(b, a); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(a, b); err != nil {
		t.Fatal(err)
	}
	rule := Rule{
		PositionalConstraints: []Pattern{
			unconstrained(),
			{Whitelist: mustWhitelist(".*").Whitelist, PathResolution: PathResolutionDot},
		},
		Tier: "allow",
	}
	if h.ruleMatches(rule, []string{"cat", "a"}, nil, root) {
		t.Fatal("expected no match -- a symlink cycle must fail closed, not match anything")
	}
}

func TestResolveForMatch_PathMode(t *testing.T) {
	resolvedEcho, err := exec.LookPath("echo")
	if err != nil {
		t.Skip("echo not found on $PATH")
	}
	resolved, ok := resolveForMatch("echo", PathResolutionPATH, "")
	if !ok || resolved != resolvedEcho {
		t.Fatalf("expected %q, ok=true, got %q, ok=%v", resolvedEcho, resolved, ok)
	}
	if _, ok := resolveForMatch("definitely-not-a-real-binary-xyz", PathResolutionPATH, ""); ok {
		t.Fatal("expected ok=false for a binary that isn't on $PATH")
	}
}

func TestResolveForMatch_ManPathMode(t *testing.T) {
	if _, err := exec.LookPath("man"); err != nil {
		t.Skip("man not available")
	}
	resolved, ok := resolveForMatch("ls", PathResolutionManPath, "")
	if !ok || resolved == "" {
		t.Fatalf("expected ls's man page to resolve to a real path, got %q, ok=%v", resolved, ok)
	}
	if _, ok := resolveForMatch("definitely-not-a-real-manpage-xyz", PathResolutionManPath, ""); ok {
		t.Fatal("expected ok=false for a name with no man page")
	}
}

func TestResolveForMatch_NoneModeIsRawPassthrough(t *testing.T) {
	resolved, ok := resolveForMatch("../../etc/passwd", PathResolutionNone, "/some/cwd")
	if !ok || resolved != "../../etc/passwd" {
		t.Fatalf("expected the raw value unchanged, got %q, ok=%v", resolved, ok)
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

// --- Tier-aware run_shell_command (the daemon is now the sole decider of
// allow/ask/deny -- these confirm the verdict/execute branching in
// runRunShellCommand, not just "did a rule match at all"). ------------------

func TestRunShellCommand_AskTierPausesWithoutExecuting(t *testing.T) {
	h, _ := newTestHandler(t)
	h.SetPolicyLayers([]PolicyLayer{{ID: 1, Rules: []Rule{{
		ID:                    7,
		PositionalConstraints: []Pattern{mustResolvedBinaryWhitelist(t, "echo")},
		Tier:                  "ask",
	}}}})
	res, err := h.Dispatch(&Request{Action: "run_shell_command", PositionalArgs: []string{"echo", "hi"}})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if res.Tier != "ask" || res.Success || res.Stdout != "" || res.MatchedLayerID != 1 || res.MatchedRuleID != 7 {
		t.Fatalf("expected an unexecuted ask verdict naming the matched rule, got %+v", res)
	}
}

func TestRunShellCommand_DenyTierRuleIsDeniedWithoutExecuting(t *testing.T) {
	h, _ := newTestHandler(t)
	h.SetPolicyLayers([]PolicyLayer{{ID: 1, Rules: []Rule{{
		ID:                    9,
		PositionalConstraints: []Pattern{mustResolvedBinaryWhitelist(t, "echo")},
		Tier:                  "deny",
	}}}})
	res, err := h.Dispatch(&Request{Action: "run_shell_command", PositionalArgs: []string{"echo", "hi"}})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if res.Tier != "deny" || res.Success || res.MatchedLayerID != 1 || res.MatchedRuleID != 9 {
		t.Fatalf("expected a deny verdict naming the matched (deny-tier) rule, got %+v", res)
	}
}

func TestRunShellCommand_ApprovedResendExecutesAskTierRule(t *testing.T) {
	h, _ := newTestHandler(t)
	h.SetPolicyLayers([]PolicyLayer{{ID: 1, Rules: []Rule{{
		ID:                    7,
		PositionalConstraints: []Pattern{mustResolvedBinaryWhitelist(t, "echo"), mustWhitelist("^hi$")},
		Tier:                  "ask",
	}}}})
	res, err := h.Dispatch(&Request{Action: "run_shell_command", PositionalArgs: []string{"echo", "hi"}, Approved: true})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if res.Tier != "ask" || !res.Success || res.Stdout != "hi" || res.MatchedLayerID != 1 || res.MatchedRuleID != 7 {
		t.Fatalf("expected an approved ask-tier resend to execute, got %+v", res)
	}
}

func TestRunShellCommand_ApprovedFlagIgnoredWhenPolicyNowDenies(t *testing.T) {
	// The daemon keeps no memory of the earlier "ask" -- it re-matches fresh
	// every time. If the policy changed to deny between the ask and the
	// resend, Approved never overrides that.
	h, _ := newTestHandler(t)
	h.SetPolicyLayers([]PolicyLayer{{ID: 1, Rules: []Rule{{
		ID:                    9,
		PositionalConstraints: []Pattern{mustResolvedBinaryWhitelist(t, "echo")},
		Tier:                  "deny",
	}}}})
	res, err := h.Dispatch(&Request{Action: "run_shell_command", PositionalArgs: []string{"echo", "hi"}, Approved: true})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if res.Tier != "deny" || res.Success {
		t.Fatalf("expected Approved to never override a since-changed deny verdict, got %+v", res)
	}
}

func TestRunShellCommand_ApprovedFlagIgnoredForAllowTier(t *testing.T) {
	h, _ := newTestHandler(t)
	h.SetPolicyLayers([]PolicyLayer{{ID: 1, Rules: []Rule{{
		ID:                    1,
		PositionalConstraints: []Pattern{mustResolvedBinaryWhitelist(t, "echo"), mustWhitelist("^hi$")},
		Tier:                  "allow",
	}}}})
	for _, approved := range []bool{false, true} {
		res, err := h.Dispatch(&Request{Action: "run_shell_command", PositionalArgs: []string{"echo", "hi"}, Approved: approved})
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if res.Tier != "allow" || !res.Success || res.Stdout != "hi" {
			t.Fatalf("expected an allow-tier rule to execute regardless of Approved=%v, got %+v", approved, res)
		}
	}
}

// --- eval_policy: evaluates an AD HOC, inline set of rules, no execution,
// never consulting h.PolicyLayers() (that's run_shell_command's own cached,
// host-attached set). --------------------------------------------------------

func TestEvalPolicy_MatchesInlineLayersNotTheCachedSet(t *testing.T) {
	h, _ := newTestHandler(t)
	// A completely different set is attached for real dispatch -- proves
	// eval_policy never consults it.
	h.SetPolicyLayers([]PolicyLayer{{ID: 99, Rules: []Rule{{ID: 99, PositionalConstraints: []Pattern{unconstrained()}, Tier: "deny"}}}})

	res, err := h.Dispatch(&Request{
		Action:         "eval_policy",
		PositionalArgs: []string{"echo", "hi"},
		PolicyLayers: []PolicyLayerWire{{
			ID:   1,
			Name: "ad hoc",
			Rules: []RuleWire{{
				ID:                    5,
				PositionalConstraints: []PatternWire{},
				Tier:                  "ask",
			}},
		}},
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if res.Tier != "ask" || res.Success || res.MatchedLayerID != 1 || res.MatchedRuleID != 5 {
		t.Fatalf("expected the inline layer's rule to match (never the cached, attached set), got %+v", res)
	}
}

func TestEvalPolicy_NoMatchIsTerminalDeny(t *testing.T) {
	h, _ := newTestHandler(t)
	res, err := h.Dispatch(&Request{Action: "eval_policy", PositionalArgs: []string{"echo"}})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if res.Tier != "deny" || res.MatchedLayerID != 0 || res.MatchedRuleID != 0 {
		t.Fatalf("expected a terminal deny with no matched IDs for an empty inline layer set, got %+v", res)
	}
}

func TestEvalPolicy_RequiresAtLeastTheBinary(t *testing.T) {
	h, _ := newTestHandler(t)
	if _, err := h.Dispatch(&Request{Action: "eval_policy", PositionalArgs: []string{}}); err == nil {
		t.Fatal("expected an ActionError -- positional_args must include at least the binary")
	}
}

func TestEvalPolicy_MalformedRegexIsRejected(t *testing.T) {
	h, _ := newTestHandler(t)
	badWhitelist := "["
	_, err := h.Dispatch(&Request{
		Action:         "eval_policy",
		PositionalArgs: []string{"echo"},
		PolicyLayers: []PolicyLayerWire{{
			ID:   1,
			Name: "bad",
			Rules: []RuleWire{{
				ID:                    1,
				PositionalConstraints: []PatternWire{mustPatternWire(t, &badWhitelist, nil)},
				Tier:                  "allow",
			}},
		}},
	})
	if _, ok := err.(*ActionError); !ok {
		t.Fatalf("expected an ActionError for an uncompilable inline pattern, got %v", err)
	}
}

func TestEvalPolicy_DegradesGracefullyWhenBinaryNotOnPath(t *testing.T) {
	// Unlike run_shell_command, a lookup failure never fails the whole
	// eval -- eval never executes anything, so a hypothetical binary that
	// doesn't exist on this machine is still a legitimate thing to ask
	// "what would happen if..." about (matched against the raw, unresolved
	// name it degrades to).
	h, _ := newTestHandler(t)
	res, err := h.Dispatch(&Request{
		Action:         "eval_policy",
		PositionalArgs: []string{"definitely-not-a-real-binary-xyz"},
		PolicyLayers: []PolicyLayerWire{{
			ID:   1,
			Name: "raw-name",
			Rules: []RuleWire{{
				ID:                    1,
				PositionalConstraints: []PatternWire{mustPatternWire(t, strPtr("^definitely-not-a-real-binary-xyz$"), nil)},
				Tier:                  "allow",
			}},
		}},
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if res.Tier != "allow" {
		t.Fatalf("expected the raw (unresolved) name to still match, got %+v", res)
	}
}

func TestEvalPolicy_CwdConstraint(t *testing.T) {
	h, _ := newTestHandler(t)
	res, err := h.Dispatch(&Request{
		Action:         "eval_policy",
		PositionalArgs: []string{"definitely-not-a-real-binary-xyz"},
		Cwd:            "/home/alice",
		PolicyLayers: []PolicyLayerWire{{
			ID:   1,
			Name: "cwd-scoped",
			Rules: []RuleWire{{
				ID:  1,
				Cwd: mustPatternWire(t, strPtr("^/home/.+"), nil),
				PositionalConstraints: []PatternWire{
					mustPatternWire(t, strPtr("^definitely-not-a-real-binary-xyz$"), nil),
				},
				Tier: "allow",
			}},
		}},
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if res.Tier != "allow" {
		t.Fatalf("expected the cwd constraint to be satisfied by req.Cwd, got %+v", res)
	}
}

func strPtr(s string) *string { return &s }

// mustPatternWire builds a PatternWire the same way JSON decoding would --
// via UnmarshalJSON -- since its fields are unexported and there's no
// exported constructor (mirrors how a real eval_policy request arrives).
func mustPatternWire(t *testing.T, whitelist, blacklist *string) PatternWire {
	t.Helper()
	obj := map[string]any{}
	if whitelist != nil {
		obj["whitelist"] = *whitelist
	}
	if blacklist != nil {
		obj["blacklist"] = *blacklist
	}
	data, err := json.Marshal(obj)
	if err != nil {
		t.Fatalf("json.Marshal: %v", err)
	}
	var pw PatternWire
	if err := json.Unmarshal(data, &pw); err != nil {
		// Not a Fatal -- some tests deliberately construct an uncompilable
		// pattern this way and expect Compile (not UnmarshalJSON) to be
		// where it fails; a raw regex string is always valid JSON/decode
		// input regardless of whether it later compiles.
	}
	return pw
}
