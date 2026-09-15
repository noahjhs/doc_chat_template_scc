package commands

import (
	"fmt"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
)

// RuleChainPattern is one whitelist/blacklist pair, or a special sentinel,
// for a single positional or option argument value -- see
// auth_service/models.py's RuleChainPattern for the full reasoning (RE2,
// "{roots}"). Compiled once at fetch/decode time (see
// config/rulechains.go's FetchRuleChains), never per-match.
type RuleChainPattern struct {
	Wildcard       bool           // true iff this entry was the literal "*" -- always satisfied
	WhitelistRoots bool           // true iff whitelist was exactly "{roots}" (mutually exclusive with Wildcard)
	Whitelist      *regexp.Regexp // nil if absent, Wildcard, or WhitelistRoots
	Blacklist      *regexp.Regexp // nil if absent
}

// OptionConstraint is one option (not "flag" -- options can carry values) a
// rule constrains, identified by its short and/or long form. Pattern nil
// means the option must be present with NO value; Pattern.Wildcard means
// present with any/no value; otherwise present WITH a value satisfying it.
type OptionConstraint struct {
	Short, Long string
	Pattern     *RuleChainPattern
}

// Rule is one entry in a RuleChain's ordered list -- see db.py's own schema
// comment for the full shape. PositionalConstraints' slice index IS the
// argv position (index 0 = the binary); a supplied value beyond this
// slice's length is unconstrained.
type Rule struct {
	ID                    int
	PositionalConstraints []RuleChainPattern
	OptionConstraints     []OptionConstraint
	Tier                  string
}

// RuleChain is the daemon's own cached copy of a user-authored, ordered
// rule list -- fetched from auth_service (see config.FetchRuleChains) and
// cached here so enforcement never has to trust the browser/model to have
// applied a rule correctly. Rules is already ordered by position.
type RuleChain struct {
	ID    int
	Name  string
	Rules []Rule
}

// SetRuleChains replaces the entire cached set -- called after every fetch
// from auth_service (pairing, resume, or an explicit refresh triggered
// from the web app), never merged incrementally.
func (h *Handler) SetRuleChains(ruleChains []RuleChain) {
	h.mu.Lock()
	h.ruleChains = ruleChains
	h.mu.Unlock()
}

// RuleChains returns a snapshot of the currently cached rule chains.
func (h *Handler) RuleChains() []RuleChain {
	h.mu.Lock()
	defer h.mu.Unlock()
	return append([]RuleChain(nil), h.ruleChains...)
}

// SetRefreshRuleChainsFunc injects the callback runRefreshRuleChains
// invokes -- set once at startup by cmd/casper/main.go (after daemonState
// exists, so the closure can reach its own getDeviceToken/authDomain; nil
// until then, and always nil in tests, same nil-tolerant posture as
// pickAndPersistDir). Kept as an injected callback rather than importing
// agent/internal/config directly here, since this package has no business
// knowing about auth domains/device tokens.
func (h *Handler) SetRefreshRuleChainsFunc(fn func()) {
	h.mu.Lock()
	h.refreshRuleChainsFn = fn
	h.mu.Unlock()
}

// runRefreshRuleChains is not part of run_local_command's model-facing
// action enum (see COMMAND_CATEGORIES in utils/sidebar.py) -- only the web
// app's Resources page triggers it, same posture as runAddDirectory. Runs
// the fetch in a goroutine rather than blocking the request/response; the
// caller is expected to poll (e.g. re-fetch GET /hosts) rather than wait on
// this response for the refreshed set.
func (h *Handler) runRefreshRuleChains(_ *Request) (Result, error) {
	h.mu.Lock()
	fn := h.refreshRuleChainsFn
	h.mu.Unlock()
	if fn == nil {
		return Result{}, &ActionError{Detail: "Refreshing rule chains isn't supported on this installation."}
	}
	go fn()
	return h.ok("Refreshing rule chains -- check back shortly.", ""), nil
}

func (h *Handler) findRuleChain(id int) (RuleChain, bool) {
	h.mu.Lock()
	defer h.mu.Unlock()
	for _, rc := range h.ruleChains {
		if rc.ID == id {
			return rc, true
		}
	}
	return RuleChain{}, false
}

// runListRuleChains reports the daemon's own currently-cached set --
// model-visible, mirrors runListDirectories -- so the model can see what's
// actually available right now rather than working off a stale schema. The
// rich, per-rule prose description lives in pages/chat.py's tool schema
// (built from the same data fetched via GET /hosts); this is a lighter
// "what does the daemon think it has right now" summary, same modest role
// runListDirectories plays for addressable directories.
func (h *Handler) runListRuleChains(_ *Request) (Result, error) {
	ruleChains := h.RuleChains()
	if len(ruleChains) == 0 {
		return h.ok("No rule chains enabled on this host.", ""), nil
	}
	var lines []string
	for _, rc := range ruleChains {
		var tiers []string
		for _, r := range rc.Rules {
			tiers = append(tiers, r.Tier)
		}
		lines = append(lines, fmt.Sprintf("#%d %s: %d rule(s) [%s]", rc.ID, rc.Name, len(rc.Rules), strings.Join(tiers, ", ")))
	}
	return h.ok(strings.Join(lines, "\n"), ""), nil
}

// valueInRoots mirrors resolvePath's resolve-and-confine logic exactly,
// returning a bool instead of raising -- used by valueMatchesPattern for a
// "{roots}" whitelist. Unconditionally false with zero roots (matches
// isConfined's own posture), so a "{roots}" constraint just never matches
// rather than erroring -- the rule falls through to the next one, or the
// terminal deny.
func (h *Handler) valueInRoots(value string) bool {
	base := h.getCwd()
	expanded := expandUser(value)
	if expanded == "" {
		expanded = "."
	}
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

// valueMatchesPattern reports whether value satisfies p -- Wildcard always
// matches; WhitelistRoots checks containment via valueInRoots against the
// RESOLVED path, while a blacklist, if also present, still checks the RAW
// value (a deliberate asymmetry, documented on RuleChainPattern in
// auth_service/models.py); otherwise a plain RE2 whitelist/blacklist check
// against the raw value.
func (h *Handler) valueMatchesPattern(value string, p RuleChainPattern) bool {
	if p.Wildcard {
		return true
	}
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
// given structured call -- first-match-wins order is matchRule's job, not
// this function's. Positions/options the model supplies but r doesn't
// mention are simply not checked (don't-care), matching the same
// "unconstrained" philosophy applied throughout this schema.
func (h *Handler) ruleMatches(r Rule, positionalArgs []string, options []RequestOption) bool {
	for i, pc := range r.PositionalConstraints {
		if pc.Wildcard {
			continue
		}
		if i >= len(positionalArgs) {
			return false // a real constraint with nothing supplied to check against
		}
		if !h.valueMatchesPattern(positionalArgs[i], pc) {
			return false
		}
	}
	for _, oc := range r.OptionConstraints {
		supplied, ok := findOption(options, oc.Short, oc.Long)
		if !ok {
			return false
		}
		switch {
		case oc.Pattern == nil:
			if supplied.Value != nil {
				return false
			}
		case oc.Pattern.Wildcard:
			// present with any/no value -- always satisfied from here
		default:
			if supplied.Value == nil || !h.valueMatchesPattern(*supplied.Value, *oc.Pattern) {
				return false
			}
		}
	}
	return true
}

// matchRule walks chain.Rules in order, returning the first one that
// matches -- nil means terminal deny (every rule exhausted, none matched).
func (h *Handler) matchRule(chain RuleChain, positionalArgs []string, options []RequestOption) *Rule {
	for i := range chain.Rules {
		if h.ruleMatches(chain.Rules[i], positionalArgs, options) {
			return &chain.Rules[i]
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

// runRunRuleChainCall is the daemon's own enforcement point -- purely
// structural, same as every other action in commands.go: matches the
// model-supplied structured call (never a raw argv string -- see Request's
// own doc comment) against chain.Rules in order, first match wins,
// terminal deny. req.Path (reused from every path-taking action above) now
// ALWAYS picks which confined directory to run in when given -- a
// deliberate change from the old path_scoped boolean, matching how every
// other confined action in this file already behaves; otherwise this just
// uses whatever directory is currently tracked as cwd, itself always
// already inside h.roots.
func (h *Handler) runRunRuleChainCall(req *Request) (Result, error) {
	chain, ok := h.findRuleChain(req.RuleChainID)
	if !ok {
		return Result{}, &ActionError{Detail: "Unknown or no longer enabled rule chain."}
	}
	if len(req.PositionalArgs) == 0 {
		return Result{}, &ActionError{Detail: "'positional_args' must include at least the binary."}
	}
	rule := h.matchRule(chain, req.PositionalArgs, req.Options)
	if rule == nil {
		return Result{}, &ActionError{Detail: "Denied: no matching rule for this call."}
	}
	// Tier is NOT enforced here -- same posture as before: pages/chat.py
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
