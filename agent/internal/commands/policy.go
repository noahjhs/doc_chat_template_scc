package commands

import (
	"fmt"
	"os/exec"
	"regexp"
	"sort"
	"strings"
)

// Pattern is one whitelist/blacklist pair for a single positional or option
// argument value -- see auth_service/models.py's Pattern for the full
// reasoning (RE2). Compiled once at fetch/decode time (see
// config/policy.go's FetchPolicyLayers), never per-match. A missing value (a
// position beyond what was supplied, or an option present with no value) is
// matched as "" -- so a blank pattern (Whitelist/Blacklist both nil) means
// "value not required" (an empty-string RE2 pattern already matches
// everything, including ""), a Whitelist of "^$" means "value not allowed"
// (only "" satisfies it), and a real pattern means "value required" (since
// "" won't satisfy most real patterns). Fully symmetric between positional
// and option constraints -- neither needs its own sentinel.
//
// A rule wanting to confine a specific argument value to the workspace
// (rather than just the command's own cwd, which runRunShellCommand already
// confines structurally via req.Path/resolvePath, independent of any
// pattern) has no dedicated sentinel for that -- author a plain regex (e.g.
// a blacklist on "^/" and "\\.\\." to reject absolute paths and traversal)
// the same way every other constraint in this schema works, optionally with
// PathResolution set to have that regex checked against the RESOLVED value
// (see resolveForMatch) rather than the raw argument string -- closing the
// symlink/lookup blind spot a raw-string regex alone can't see. An earlier
// version had a "{roots}" whitelist sentinel for a related but narrower
// purpose; removed for simplicity -- the daemon's own path confinement was
// always the real security boundary regardless (a "{roots}" rule could
// never be tier "allow" for that reason), so losing it was a narrower
// rule-authoring convenience, not a security regression.
type Pattern struct {
	Whitelist *regexp.Regexp // nil if absent
	Blacklist *regexp.Regexp // nil if absent
	// PathResolution names how to resolve this value BEFORE matching it
	// against Whitelist/Blacklist -- one of the PathResolution* constants
	// below, or "" (PathResolutionNone) for today's raw-string behavior.
	// Meaningless on Rule.Cwd (see its own doc comment) -- cwd is already a
	// daemon-resolved absolute path, not a command argument, so no
	// resolution mechanism applies to it; a Cwd pattern's PathResolution
	// field (if ever set) is simply never consulted.
	PathResolution string
}

// The PathResolution modes a positional/option constraint's Pattern can
// request -- see resolveForMatch for what each one actually does.
// Deliberately NOT including CDPATH: run_shell_command execs real binaries
// directly (never through a shell), and cd/type/hash/command are
// universally shell builtins, never real standalone executables -- so
// CDPATH-style resolution has no reachable target under this architecture.
const (
	PathResolutionNone    = ""
	PathResolutionDot     = "."
	PathResolutionPATH    = "$PATH"
	PathResolutionManPath = "MANPATH"
)

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
// the argv position (index 0 = the binary, always matched against its
// $PATH-resolved absolute path -- see runRunShellCommand -- so a rule
// constraining position 0 should match a real absolute path, not a bare
// command name); a supplied value beyond this slice's length is
// unconstrained. Cwd optionally constrains the directory the call runs in
// (already a daemon-resolved absolute path by the time it's checked -- see
// runRunShellCommand -- so it's matched raw, via valueMatchesPattern, never
// valueMatchesPatternResolved); a blank Cwd (the zero Pattern) means "any
// directory", same "unconstrained" convention every other blank Pattern in
// this schema uses.
type Rule struct {
	ID                    int
	PositionalConstraints []Pattern
	OptionConstraints     []OptionConstraint
	Cwd                   Pattern
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
// until then, and always nil in tests). Kept as an injected callback rather
// than importing agent/internal/config directly here, since this package
// has no business knowing about auth domains/device tokens.
func (h *Handler) SetRefreshPolicyLayersFunc(fn func()) {
	h.mu.Lock()
	h.refreshPolicyLayersFn = fn
	h.mu.Unlock()
}

// runRefreshPolicyLayers is deliberately not model-visible -- only the web
// app's policy-authoring UI triggers it. Runs the fetch in a goroutine
// rather than blocking the request/response;
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
// model-visible -- so the model can see what's actually available right now
// rather than working off a stale schema. The rich, per-rule prose
// description lives in the web app's tool schema (built from the same data
// fetched via GET /hosts); this is a lighter "what does the daemon think it
// has right now" summary.
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

// valueMatchesPattern reports whether value satisfies p -- a plain RE2
// whitelist/blacklist check against the raw value, no path resolution.
// Used for Rule.Cwd (see its own doc comment for why resolution never
// applies there).
func (h *Handler) valueMatchesPattern(value string, p Pattern) bool {
	if p.Whitelist != nil && !p.Whitelist.MatchString(value) {
		return false
	}
	if p.Blacklist != nil && p.Blacklist.MatchString(value) {
		return false
	}
	return true
}

// valueMatchesPatternResolved is valueMatchesPattern's counterpart for a
// positional/option constraint, which may carry a PathResolution mode (see
// Pattern's own doc comment): resolves value under that mode (relative to
// cwd, for PathResolutionDot) before checking it against p's
// whitelist/blacklist. Skips resolution entirely -- so a resolution failure
// can never reject an otherwise-unconstrained rule -- when p has neither a
// whitelist nor a blacklist (the "value not required" blank-pattern filler
// used throughout this schema); PathResolutionNone likewise resolves to a
// no-op (see resolveForMatch), preserving today's raw-string behavior for
// every pre-existing rule with no PathResolution set at all.
func (h *Handler) valueMatchesPatternResolved(value string, p Pattern, cwd string) bool {
	if p.Whitelist == nil && p.Blacklist == nil {
		return true
	}
	resolved, ok := resolveForMatch(value, p.PathResolution, cwd)
	if !ok {
		return false // fail closed: an unresolvable value never satisfies any pattern
	}
	if p.Whitelist != nil && !p.Whitelist.MatchString(resolved) {
		return false
	}
	if p.Blacklist != nil && p.Blacklist.MatchString(resolved) {
		return false
	}
	return true
}

// resolveForMatch applies mode to value -- PathResolutionDot joins it
// against cwd and runs it through realpath() (real symlink resolution, via
// the same machinery resolvePath itself uses -- see resolveJoinedPath),
// PathResolutionPATH looks it up on $PATH, PathResolutionManPath shells out
// to `man -w` (deferring to the system's own real MANPATH/section search
// rather than reimplementing it), and PathResolutionNone (or any other,
// unrecognized value) passes value through unchanged. Returns ok=false when
// the value can't be resolved under the requested mode.
func resolveForMatch(value, mode, cwd string) (string, bool) {
	switch mode {
	case PathResolutionDot:
		resolved, err := resolveJoinedPath(cwd, value)
		if err != nil {
			return "", false
		}
		return resolved, true
	case PathResolutionPATH:
		resolved, err := exec.LookPath(value)
		if err != nil {
			return "", false
		}
		return resolved, true
	case PathResolutionManPath:
		out, err := exec.Command("man", "-w", value).Output()
		if err != nil {
			return "", false
		}
		resolved := strings.TrimSpace(string(out))
		if resolved == "" {
			return "", false
		}
		return resolved, true
	default:
		return value, true
	}
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
// given structured call plus the effective directory it would run in --
// first-match-wins order is matchPolicy's job, not this function's.
// Positions/options the model supplies but r doesn't mention (i.e. beyond
// PositionalConstraints' length, or with no OptionConstraint entry at all)
// are simply not checked (don't-care), matching the same "unconstrained"
// philosophy applied throughout this schema. A position within
// PositionalConstraints' length that the model didn't actually supply is
// matched as "" -- same coercion the option loop below already uses -- so
// a blank pattern there means "value not required" without needing its own
// sentinel. cwd is checked first (cheapest, and independent of the call's
// own arguments) against r.Cwd, raw (see Rule.Cwd's own doc comment).
func (h *Handler) ruleMatches(r Rule, positionalArgs []string, options []RequestOption, cwd string) bool {
	if !h.valueMatchesPattern(cwd, r.Cwd) {
		return false
	}
	for i, pc := range r.PositionalConstraints {
		value := ""
		if i < len(positionalArgs) {
			value = positionalArgs[i]
		}
		if !h.valueMatchesPatternResolved(value, pc, cwd) {
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
		if !h.valueMatchesPatternResolved(value, oc.Pattern, cwd) {
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
// is empty or composes to zero rules). cwd is the effective directory the
// call would run in (see runRunShellCommand) -- checked against each rule's
// own Cwd pattern, and used as the join base for any PathResolutionDot
// constraint.
func (h *Handler) matchPolicy(layers []PolicyLayer, positionalArgs []string, options []RequestOption, cwd string) *Rule {
	rules := composePolicy(layers)
	for i := range rules {
		if h.ruleMatches(rules[i], positionalArgs, options, cwd) {
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
// this file already behaves; otherwise this just uses homeRoot, the fixed
// directory every path-taking action is confined to.
//
// The effective directory is resolved FIRST, before matching -- so a rule's
// Cwd pattern and any PathResolutionDot constraint see the real directory
// the call would actually run in, never a raw/unresolved req.Path. The
// binary (position 0) is likewise always resolved via $PATH before
// matching (see Rule's own doc comment) -- what gets pattern-matched and
// what actually execs are thereby guaranteed to be the exact same file, no
// gap between the two.
func (h *Handler) runRunShellCommand(req *Request) (Result, error) {
	if len(req.PositionalArgs) == 0 {
		return Result{}, &ActionError{Detail: "'positional_args' must include at least the binary."}
	}

	dir := h.HomeRoot()
	if dir == "" {
		return h.fail("No directory configured on this installation."), nil
	}
	if req.Path != "" {
		target, err := h.resolvePath(req.Path, true, true)
		if err != nil {
			return Result{}, err
		}
		dir = target
	}

	resolvedBinary, err := exec.LookPath(req.PositionalArgs[0])
	if err != nil {
		return Result{}, &ActionError{Detail: fmt.Sprintf("Binary not found on $PATH: %s", req.PositionalArgs[0])}
	}
	matchArgs := append([]string{resolvedBinary}, req.PositionalArgs[1:]...)

	rule := h.matchPolicy(h.PolicyLayers(), matchArgs, req.Options, dir)
	if rule == nil {
		return Result{}, &ActionError{Detail: "Denied: no matching rule for this call."}
	}
	// Tier is NOT enforced here -- same posture as before: the web app
	// decides ask/allow/deny before ever calling this action; command_key
	// possession remains the real security boundary.

	argv := buildArgv(req.PositionalArgs[1:], req.Options)

	// sandboxedCommand (seatbelt_darwin.go/seatbelt_other.go) wraps this in
	// a macOS Seatbelt profile confined to homeRoot (the WHOLE confined
	// tree, not just dir -- Seatbelt's own scope is deliberately as broad
	// as the daemon's own confinement gets, regardless of which
	// subdirectory a given call happens to run in) when available --
	// defense-in-depth on top of the rule match just above, not a
	// replacement for it.
	cmd, cleanup := sandboxedCommand(resolvedBinary, argv, []string{h.HomeRoot()})
	defer cleanup()
	cmd.Dir = dir
	var stdout, stderr strings.Builder
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	err = cmd.Run()
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
