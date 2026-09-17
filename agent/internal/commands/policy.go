package commands

import (
	"fmt"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
)

// Pattern is one whitelist/blacklist pair for a single positional or option
// argument value -- see auth_service/models.py's Pattern for the full
// reasoning (RE2, "{roots}"). Compiled once at fetch/decode time (see
// config/policy.go's FetchPolicyLayers), never per-match. A missing value (a
// position beyond what was supplied, or an option present with no value) is
// matched as "" -- so a blank pattern (Whitelist/Blacklist both nil) means
// "value not required" (an empty-string RE2 pattern already matches
// everything, including ""), a Whitelist of "^$" means "value not allowed"
// (only "" satisfies it), and a real pattern means "value required" (since
// "" won't satisfy most real patterns). Fully symmetric between positional
// and option constraints -- neither needs its own sentinel.
type Pattern struct {
	WhitelistRoots bool           // true iff whitelist was exactly "{roots}"
	Whitelist      *regexp.Regexp // nil if absent or WhitelistRoots
	Blacklist      *regexp.Regexp // nil if absent
}

// OptionConstraint is one option (not "flag" -- options can carry values) a
// rule constrains, identified by its short and/or long form. Including an
// OptionConstraint at all means that option must be PRESENT; Pattern is
// checked against its value, or against "" if it was present with no
// value -- so a blank Pattern (Whitelist/Blacklist both nil) accepts
// any/no value, and a Whitelist of "^$" requires no value specifically.
// See auth_service/models.py's OptionConstraint for the full reasoning.
type OptionConstraint struct {
	Short, Long string
	Pattern     Pattern
}

// Rule is one entry in a PolicyLayer's ordered list -- see db.py's own
// schema comment for the full shape. PositionalConstraints' slice index IS
// the argv position (index 0 = the binary); a supplied value beyond this
// slice's length is unconstrained.
type Rule struct {
	ID                    int
	PositionalConstraints []Pattern
	OptionConstraints     []OptionConstraint
	Tier                  string
}

// PolicyLayer is the daemon's own cached copy of a user-authored, ordered
// rule list -- fetched from auth_service (see config.FetchPolicyLayers) and
// cached here so enforcement never has to trust the browser/model to have
// applied a rule correctly. Rules is already ordered by position. A Policy
// (see composePolicy below) is the concatenation of every PolicyLayer
// currently attached to this host -- a layer is never evaluated standalone.
type PolicyLayer struct {
	ID    int
	Name  string
	Rules []Rule
}

// SetPolicyLayers replaces the entire cached set -- called after every fetch
// from auth_service (pairing, resume, or an explicit refresh triggered
// from the web app), never merged incrementally.
func (h *Handler) SetPolicyLayers(layers []PolicyLayer) {
	h.mu.Lock()
	h.policyLayers = layers
	h.mu.Unlock()
}

// PolicyLayers returns a snapshot of the currently cached policy layers.
func (h *Handler) PolicyLayers() []PolicyLayer {
	h.mu.Lock()
	defer h.mu.Unlock()
	return append([]PolicyLayer(nil), h.policyLayers...)
}

// SetRefreshPolicyLayersFunc injects the callback runRefreshPolicyLayers
// invokes -- set once at startup by cmd/casper/main.go (after daemonState
// exists, so the closure can reach its own getDeviceToken/authDomain; nil
// until then, and always nil in tests, same nil-tolerant posture as
// pickAndPersistDir). Kept as an injected callback rather than importing
// agent/internal/config directly here, since this package has no business
// knowing about auth domains/device tokens.
func (h *Handler) SetRefreshPolicyLayersFunc(fn func()) {
	h.mu.Lock()
	h.refreshPolicyLayersFn = fn
	h.mu.Unlock()
}

// runRefreshPolicyLayers is not part of run_local_command's model-facing
// action enum (see COMMAND_CATEGORIES in utils/sidebar.py) -- only the web
// app's policy-authoring UI triggers it, same posture as runAddDirectory.
// Runs the fetch in a goroutine rather than blocking the request/response;
// the caller is expected to poll (e.g. re-fetch GET /hosts) rather than
// wait on this response for the refreshed set.
func (h *Handler) runRefreshPolicyLayers(_ *Request) (Result, error) {
	h.mu.Lock()
	fn := h.refreshPolicyLayersFn
	h.mu.Unlock()
	if fn == nil {
		return Result{}, &ActionError{Detail: "Refreshing policy layers isn't supported on this installation."}
	}
	go fn()
	return h.ok("Refreshing policy layers -- check back shortly.", ""), nil
}

// runListPolicyLayers reports the daemon's own currently-cached set --
// model-visible, mirrors runListDirectories -- so the model can see what's
// actually available right now rather than working off a stale schema. The
// rich, per-rule prose description lives in the web app's tool schema
// (built from the same data fetched via GET /hosts); this is a lighter
// "what does the daemon think it has right now" summary, same modest role
// runListDirectories plays for addressable directories.
func (h *Handler) runListPolicyLayers(_ *Request) (Result, error) {
	layers := h.PolicyLayers()
	if len(layers) == 0 {
		return h.ok("No policy layers enabled on this host.", ""), nil
	}
	var lines []string
	for _, pl := range layers {
		var tiers []string
		for _, r := range pl.Rules {
			tiers = append(tiers, r.Tier)
		}
		lines = append(lines, fmt.Sprintf("#%d %s: %d rule(s) [%s]", pl.ID, pl.Name, len(pl.Rules), strings.Join(tiers, ", ")))
	}
	return h.ok(strings.Join(lines, "\n"), ""), nil
}

// valueInRoots mirrors resolvePath's resolve-and-confine logic exactly,
// returning a bool instead of raising -- used by valueMatchesPattern for a
// "{roots}" whitelist. Unconditionally false with zero roots (matches
// isConfined's own posture), so a "{roots}" constraint just never matches
// rather than erroring -- the rule falls through to the next one, or the
// terminal deny. Also unconditionally false for an empty value -- unlike
// resolvePath (where an empty req.Path deliberately means "use cwd" for
// path-taking actions), an empty value reaching here means a positional
// argument or option value was never actually supplied (see ruleMatches'
// "missing value matched as \"\"" coercion) and must never be treated as
// "so check the cwd instead", which would let a rule requiring a real
// path-in-roots value silently pass on a value that was never given.
func (h *Handler) valueInRoots(value string) bool {
	if value == "" {
		return false
	}
	base := h.getCwd()
	expanded := expandUser(value)
	var joined string
	if filepath.IsAbs(expanded) {
		joined = expanded
	} else {
		joined = filepath.Join(base, expanded)
	}
	resolved, err := realpath(joined)
	if err != nil {
		return false
	}
	return h.isConfined(resolved)
}

// valueMatchesPattern reports whether value satisfies p -- WhitelistRoots
// checks containment via valueInRoots against the RESOLVED path, while a
// blacklist, if also present, still checks the RAW value (a deliberate
// asymmetry, documented on Pattern in auth_service/models.py); otherwise a
// plain RE2 whitelist/blacklist check against the raw value.
func (h *Handler) valueMatchesPattern(value string, p Pattern) bool {
	if p.WhitelistRoots {
		if !h.valueInRoots(value) {
			return false
		}
	} else if p.Whitelist != nil && !p.Whitelist.MatchString(value) {
		return false
	}
	if p.Blacklist != nil && p.Blacklist.MatchString(value) {
		return false
	}
	return true
}

func findOption(options []RequestOption, short, long string) (RequestOption, bool) {
	for _, o := range options {
		if (short != "" && o.Short == short) || (long != "" && o.Long == long) {
			return o, true
		}
	}
	return RequestOption{}, false
}

// ruleMatches reports whether every constraint in r is satisfied by the
// given structured call -- first-match-wins order is matchPolicy's job, not
// this function's. Positions/options the model supplies but r doesn't
// mention (i.e. beyond PositionalConstraints' length, or with no
// OptionConstraint entry at all) are simply not checked (don't-care),
// matching the same "unconstrained" philosophy applied throughout this
// schema. A position within PositionalConstraints' length that the model
// didn't actually supply is matched as "" -- same coercion the option loop
// below already uses -- so a blank pattern there means "value not
// required" without needing its own sentinel.
func (h *Handler) ruleMatches(r Rule, positionalArgs []string, options []RequestOption) bool {
	for i, pc := range r.PositionalConstraints {
		value := ""
		if i < len(positionalArgs) {
			value = positionalArgs[i]
		}
		if !h.valueMatchesPattern(value, pc) {
			return false
		}
	}
	for _, oc := range r.OptionConstraints {
		supplied, ok := findOption(options, oc.Short, oc.Long)
		if !ok {
			return false
		}
		// A missing value is matched as "" -- see OptionConstraint's own
		// doc comment for why this makes "^$" the way to require no value,
		// with no separate nil/Wildcard special-casing needed here.
		value := ""
		if supplied.Value != nil {
			value = *supplied.Value
		}
		if !h.valueMatchesPattern(value, oc.Pattern) {
			return false
		}
	}
	return true
}

// composePolicy concatenates every given layer's Rules into one ordered
// list -- v1's whole composition rule (see the plan discussion this
// implements: "our v1 engine can simply concatenate them... re-evaluate
// after realistic usage"). Layers are sorted by ID ascending first, so
// composition order is stable and independent of whatever order the caller
// (or auth_service's own query, which currently orders by name) happened to
// supply them in.
func composePolicy(layers []PolicyLayer) []Rule {
	sorted := append([]PolicyLayer(nil), layers...)
	sort.Slice(sorted, func(i, j int) bool { return sorted[i].ID < sorted[j].ID })
	var rules []Rule
	for _, pl := range sorted {
		rules = append(rules, pl.Rules...)
	}
	return rules
}

// matchPolicy composes layers into one Policy (see composePolicy) and walks
// it in order, returning the first rule that matches -- nil means terminal
// deny (every rule exhausted, none matched, including the case where layers
// is empty or composes to zero rules).
func (h *Handler) matchPolicy(layers []PolicyLayer, positionalArgs []string, options []RequestOption) *Rule {
	rules := composePolicy(layers)
	for i := range rules {
		if h.ruleMatches(rules[i], positionalArgs, options) {
			return &rules[i]
		}
	}
	return nil
}

// buildArgv constructs the actual argv (excluding the binary itself, which
// the caller passes separately to sandboxedCommand) from the model's
// structured options plus any extra positional args beyond the binary --
// the ONE place raw argv gets built, only after a rule has already matched
// the structured data directly (never the other way around). Renders each
// option as --{long} {value} (preferring long when given, else -{short}
// {value}), the value token omitted entirely when nil, followed by
// extraPositionals -- exact relative ordering doesn't affect matching
// (which never re-parses this), only needs to be some deterministic,
// reproducible order.
func buildArgv(extraPositionals []string, options []RequestOption) []string {
	var argv []string
	for _, o := range options {
		if o.Long != "" {
			argv = append(argv, "--"+o.Long)
		} else {
			argv = append(argv, "-"+o.Short)
		}
		if o.Value != nil {
			argv = append(argv, *o.Value)
		}
	}
	return append(argv, extraPositionals...)
}

// runRunShellCommand is the daemon's own enforcement point -- purely
// structural, same as every other action in commands.go: composes every
// policy layer currently attached to this host into one Policy (see
// composePolicy) and matches the model-supplied structured call (never a
// raw argv string -- see Request's own doc comment) against it in order,
// first match wins, terminal deny. Host-scoped, not layer-scoped: there's
// no ID to look up here (GET /hosts/policy-layers already device-token-
// scopes h.PolicyLayers() to this host's own attached layers -- see
// config.FetchPolicyLayers) -- a rule is only ever evaluated as part of a
// policy, never a layer standalone, so every real call composes the whole
// set rather than the model picking one layer to check against. req.Path
// (reused from every path-taking action above) now ALWAYS picks which
// confined directory to run in when given -- a deliberate change from the
// old path_scoped boolean, matching how every other confined action in
// this file already behaves; otherwise this just uses whatever directory
// is currently tracked as cwd, itself always already inside h.roots.
func (h *Handler) runRunShellCommand(req *Request) (Result, error) {
	if len(req.PositionalArgs) == 0 {
		return Result{}, &ActionError{Detail: "'positional_args' must include at least the binary."}
	}
	rule := h.matchPolicy(h.PolicyLayers(), req.PositionalArgs, req.Options)
	if rule == nil {
		return Result{}, &ActionError{Detail: "Denied: no matching rule for this call."}
	}
	// Tier is NOT enforced here -- same posture as before: the web app
	// decides ask/allow/deny before ever calling this action; command_key
	// possession remains the real security boundary.

	if len(h.Roots()) == 0 {
		return h.fail("No directories added yet -- add one first (the \"+\" button in the workspace browser)."), nil
	}

	dir := h.getCwd()
	if req.Path != "" {
		target, err := h.resolvePath(req.Path, true, true)
		if err != nil {
			return Result{}, err
		}
		dir = target
	}

	binary := req.PositionalArgs[0]
	argv := buildArgv(req.PositionalArgs[1:], req.Options)

	// sandboxedCommand (seatbelt_darwin.go/seatbelt_other.go) wraps this in
	// a macOS Seatbelt profile confined to h.Roots() when available --
	// defense-in-depth on top of the rule match just above, not a
	// replacement for it.
	cmd, cleanup := sandboxedCommand(binary, argv, h.Roots())
	defer cleanup()
	cmd.Dir = dir
	var stdout, stderr strings.Builder
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	err := cmd.Run()
	if err != nil {
		exitCode := 1
		if exitErr, ok := err.(*exec.ExitError); ok {
			exitCode = exitErr.ExitCode()
		}
		return Result{
			Success:  false,
			Cwd:      dir,
			Stdout:   strings.TrimSpace(stdout.String()),
			Stderr:   strings.TrimSpace(stderr.String()),
			ExitCode: &exitCode,
		}, nil
	}
	return Result{
		Success: true, Cwd: dir, Stdout: strings.TrimSpace(stdout.String()), Stderr: strings.TrimSpace(stderr.String()),
	}, nil
}
