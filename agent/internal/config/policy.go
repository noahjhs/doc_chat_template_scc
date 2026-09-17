package config

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"regexp"
	"time"

	"casper-agent/internal/commands"
)

// patternWire decodes one JSON {"whitelist":.., "blacklist":..} object --
// mirrors auth_service/models.py's Pattern, used for both
// positional_constraints list entries and OptionConstraint.pattern (fully
// symmetric -- see commands.Pattern's own doc comment for why neither
// needs a "*"/wildcard sentinel).
type patternWire struct {
	whitelist *string
	blacklist *string
}

func (p *patternWire) UnmarshalJSON(data []byte) error {
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
		Whitelist *string `json:"whitelist"`
		Blacklist *string `json:"blacklist"`
	}
	if err := json.Unmarshal(data, &obj); err != nil {
		return err
	}
	p.whitelist = obj.Whitelist
	p.blacklist = obj.Blacklist
	return nil
}

// compile turns a decoded patternWire into a commands.Pattern, compiling
// any real regex via Go's stdlib regexp (RE2) -- returns an error if
// either whitelist or blacklist fails to compile, letting the caller
// (compileRule) skip the whole rule rather than crash the daemon.
// auth_service already validates with google-re2 (a different, if
// syntax-compatible, RE2 binding) at authoring time, so this should be
// rare -- a small parity gap is still possible, and this is the
// deliberate defense against it.
func (p patternWire) compile() (commands.Pattern, error) {
	out := commands.Pattern{}
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

type optionConstraintWire struct {
	Short   string      `json:"short"`
	Long    string      `json:"long"`
	Pattern patternWire `json:"pattern"` // always a real (possibly blank) pattern -- see OptionConstraint's own doc comment
}

type ruleWire struct {
	ID                    int                    `json:"id"`
	PositionalConstraints []patternWire          `json:"positional_constraints"`
	OptionConstraints     []optionConstraintWire `json:"option_constraints"`
	Tier                  string                 `json:"tier"`
}

type hostPolicyLayerWire struct {
	ID    int        `json:"id"`
	Name  string     `json:"name"`
	Rules []ruleWire `json:"rules"`
}

// compileRule turns one decoded ruleWire into a commands.Rule, compiling
// every pattern it references. Returns an error (never partial output) if
// any single pattern fails to compile -- the caller skips the whole rule
// rather than silently enforcing a partially-constrained (more permissive
// than authored) version of it.
func compileRule(w ruleWire) (commands.Rule, error) {
	positional := make([]commands.Pattern, 0, len(w.PositionalConstraints))
	for i, pw := range w.PositionalConstraints {
		compiled, err := pw.compile()
		if err != nil {
			return commands.Rule{}, fmt.Errorf("position %d: %w", i, err)
		}
		positional = append(positional, compiled)
	}
	options := make([]commands.OptionConstraint, 0, len(w.OptionConstraints))
	for _, ow := range w.OptionConstraints {
		compiled, err := ow.Pattern.compile()
		if err != nil {
			return commands.Rule{}, fmt.Errorf("option %s/%s: %w", ow.Short, ow.Long, err)
		}
		options = append(options, commands.OptionConstraint{Short: ow.Short, Long: ow.Long, Pattern: compiled})
	}
	return commands.Rule{ID: w.ID, PositionalConstraints: positional, OptionConstraints: options, Tier: w.Tier}, nil
}

// FetchPolicyLayers GETs this installation's own enabled policy layers from
// the auth service, authenticated with device_token -- see
// auth_service/main.py's GET /hosts/policy-layers. Mirrors ReportPresence's
// HTTP-call shape. The daemon fetches and caches this itself (see
// cmd/casper/daemon.go's handlePairURL/resumeSession, which call
// commands.Handler.SetPolicyLayers with the result) rather than trusting
// the browser/model to have applied a rule correctly. A layer's own rules
// arrive already ordered by position -- see main.py's _policy_layer_rules
// -- so no re-sorting happens here (composePolicy in commands/policy.go
// handles ordering ACROSS layers, separately, at match time).
func FetchPolicyLayers(authDomain, deviceToken string) ([]commands.PolicyLayer, error) {
	req, err := http.NewRequest(http.MethodGet, BaseURL(authDomain)+"/hosts/policy-layers", nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+deviceToken)
	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("unexpected status %d fetching policy layers", resp.StatusCode)
	}
	var body struct {
		PolicyLayers []hostPolicyLayerWire `json:"policy_layers"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return nil, err
	}
	layers := make([]commands.PolicyLayer, 0, len(body.PolicyLayers))
	for _, lw := range body.PolicyLayers {
		rules := make([]commands.Rule, 0, len(lw.Rules))
		for _, rw := range lw.Rules {
			rule, err := compileRule(rw)
			if err != nil {
				log.Printf("policy layer %d (%s): skipping rule %d, failed to compile: %s", lw.ID, lw.Name, rw.ID, err)
				continue
			}
			rules = append(rules, rule)
		}
		layers = append(layers, commands.PolicyLayer{ID: lw.ID, Name: lw.Name, Rules: rules})
	}
	return layers, nil
}
