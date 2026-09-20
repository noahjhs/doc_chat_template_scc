package commands

import (
	"encoding/json"
	"fmt"
	"regexp"
)

// PatternWire decodes one JSON {"whitelist":.., "blacklist":..} object --
// mirrors casper_service/models.py's Pattern, used for both
// positional_constraints list entries and OptionConstraint.pattern (fully
// symmetric -- see Pattern's own doc comment for why neither needs a
// "*"/wildcard sentinel). Shared by config.FetchPolicyLayers (the daemon's
// own cached-layers fetch, via GET /hosts/policy-layers) and this package's
// own eval_policy action (an AD HOC, possibly-unattached set supplied
// inline on the request -- see Request.PolicyLayers) -- moved here (was
// unexported in package config, which imports this package, never the
// reverse) so both decode-then-compile paths share one pipeline instead of
// two copies silently drifting.
type PatternWire struct {
	whitelist      *string
	blacklist      *string
	pathResolution string
}

func (p *PatternWire) UnmarshalJSON(data []byte) error {
	// A stale/legacy row could still have a literal JSON null (the old
	// nilable OptionConstraint.pattern) or the bare string "*" (the old
	// positional wildcard sentinel) here -- both decode to the zero value
	// (no whitelist/blacklist), which compiles to "value not required",
	// the same safe fallback a blank {} object gets under the current
	// representation, and behaviorally equivalent to what both old forms
	// meant anyway.
	if string(data) == "null" || string(data) == `"*"` {
		return nil
	}
	var obj struct {
		Whitelist      *string `json:"whitelist"`
		Blacklist      *string `json:"blacklist"`
		PathResolution string  `json:"path_resolution"`
	}
	if err := json.Unmarshal(data, &obj); err != nil {
		return err
	}
	p.whitelist = obj.Whitelist
	p.blacklist = obj.Blacklist
	p.pathResolution = obj.PathResolution
	return nil
}

// Compile turns a decoded PatternWire into a Pattern, compiling any real
// regex via Go's stdlib regexp (RE2) -- returns an error if either
// whitelist or blacklist fails to compile, letting the caller (CompileRule)
// skip the whole rule rather than crash the daemon. casper_service already
// validates with google-re2 (a different, if syntax-compatible, RE2
// binding) at authoring time, so this should be rare -- a small parity gap
// is still possible, and this is the deliberate defense against it. An
// unrecognized path_resolution value (e.g. a future casper_service adding a
// mode this daemon build doesn't know yet) is carried through as-is --
// resolveForMatch's own default case already treats any unrecognized mode
// as a no-op passthrough, same as PathResolutionNone, so an older daemon
// build degrades to raw-string matching for that constraint rather than
// failing to compile the rule.
func (p PatternWire) Compile() (Pattern, error) {
	out := Pattern{PathResolution: p.pathResolution}
	if p.whitelist != nil {
		re, err := regexp.Compile(*p.whitelist)
		if err != nil {
			return out, fmt.Errorf("whitelist %q: %w", *p.whitelist, err)
		}
		out.Whitelist = re
	}
	if p.blacklist != nil {
		re, err := regexp.Compile(*p.blacklist)
		if err != nil {
			return out, fmt.Errorf("blacklist %q: %w", *p.blacklist, err)
		}
		out.Blacklist = re
	}
	return out, nil
}

type OptionConstraintWire struct {
	Short   string      `json:"short"`
	Long    string      `json:"long"`
	Pattern PatternWire `json:"pattern"` // always a real (possibly blank) pattern -- see OptionConstraint's own doc comment
}

type RuleWire struct {
	ID                    int                    `json:"id"`
	PositionalConstraints []PatternWire          `json:"positional_constraints"`
	OptionConstraints     []OptionConstraintWire `json:"option_constraints"`
	Cwd                   PatternWire            `json:"cwd"`
	Tier                  string                 `json:"tier"`
}

type PolicyLayerWire struct {
	ID    int        `json:"id"`
	Name  string     `json:"name"`
	Rules []RuleWire `json:"rules"`
}

// CompileRule turns one decoded RuleWire into a Rule, compiling every
// pattern it references. Returns an error (never partial output) if any
// single pattern fails to compile -- the caller skips the whole rule rather
// than silently enforcing a partially-constrained (more permissive than
// authored) version of it.
func CompileRule(w RuleWire) (Rule, error) {
	positional := make([]Pattern, 0, len(w.PositionalConstraints))
	for i, pw := range w.PositionalConstraints {
		compiled, err := pw.Compile()
		if err != nil {
			return Rule{}, fmt.Errorf("position %d: %w", i, err)
		}
		positional = append(positional, compiled)
	}
	options := make([]OptionConstraint, 0, len(w.OptionConstraints))
	for _, ow := range w.OptionConstraints {
		compiled, err := ow.Pattern.Compile()
		if err != nil {
			return Rule{}, fmt.Errorf("option %s/%s: %w", ow.Short, ow.Long, err)
		}
		options = append(options, OptionConstraint{Short: ow.Short, Long: ow.Long, Pattern: compiled})
	}
	cwd, err := w.Cwd.Compile()
	if err != nil {
		return Rule{}, fmt.Errorf("cwd: %w", err)
	}
	return Rule{ID: w.ID, PositionalConstraints: positional, OptionConstraints: options, Cwd: cwd, Tier: w.Tier}, nil
}
