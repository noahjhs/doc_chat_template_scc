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

// patternWire decodes one JSON value that's either the bare string "*" or
// an object {"whitelist":.., "blacklist":..} -- mirrors
// auth_service/models.py's RuleChainPattern / the Literal["*"] union used
// for positional_constraints list entries and OptionConstraint.pattern.
type patternWire struct {
	isWildcard bool
	whitelist  *string
	blacklist  *string
}

func (p *patternWire) UnmarshalJSON(data []byte) error {
	// A stale/legacy row could still have a literal JSON null here (the
	// old tri-state OptionConstraint.pattern representation) -- decodes to
	// the zero value (no wildcard, no whitelist/blacklist), which compiles
	// to "matches any/no value", the same safe fallback a blank {} object
	// gets under the current representation.
	if string(data) == "null" {
		return nil
	}
	var asString string
	if err := json.Unmarshal(data, &asString); err == nil {
		p.isWildcard = asString == "*"
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

// compile turns a decoded patternWire into a commands.RuleChainPattern,
// compiling any real regex via Go's stdlib regexp (RE2) -- returns an
// error if either whitelist or blacklist fails to compile, letting the
// caller (compileRule) skip the whole rule rather than crash the daemon.
// auth_service already validates with google-re2 (a different, if
// syntax-compatible, RE2 binding) at authoring time, so this should be
// rare -- a small parity gap is still possible, and this is the
// deliberate defense against it.
func (p patternWire) compile() (commands.RuleChainPattern, error) {
	if p.isWildcard {
		return commands.RuleChainPattern{Wildcard: true}, nil
	}
	out := commands.RuleChainPattern{}
	if p.whitelist != nil {
		if *p.whitelist == "{roots}" {
			out.WhitelistRoots = true
		} else {
			re, err := regexp.Compile(*p.whitelist)
			if err != nil {
				return out, fmt.Errorf("whitelist %q: %w", *p.whitelist, err)
			}
			out.Whitelist = re
		}
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

type hostRuleChainWire struct {
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
	positional := make([]commands.RuleChainPattern, 0, len(w.PositionalConstraints))
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

// FetchRuleChains GETs this installation's own enabled rule chains from the
// auth service, authenticated with device_token -- see
// auth_service/main.py's GET /hosts/rule-chains. Mirrors ReportPresence's
// HTTP-call shape. The daemon fetches and caches this itself (see
// cmd/casper/daemon.go's handlePairURL/resumeSession, which call
// commands.Handler.SetRuleChains with the result) rather than trusting the
// browser/model to have applied a rule correctly. A rule chain's own rules
// arrive already ordered by position -- see main.py's _rule_chain_rules --
// so no re-sorting happens here.
func FetchRuleChains(authDomain, deviceToken string) ([]commands.RuleChain, error) {
	req, err := http.NewRequest(http.MethodGet, BaseURL(authDomain)+"/hosts/rule-chains", nil)
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
		return nil, fmt.Errorf("unexpected status %d fetching rule chains", resp.StatusCode)
	}
	var body struct {
		RuleChains []hostRuleChainWire `json:"rule_chains"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return nil, err
	}
	ruleChains := make([]commands.RuleChain, 0, len(body.RuleChains))
	for _, cw := range body.RuleChains {
		rules := make([]commands.Rule, 0, len(cw.Rules))
		for _, rw := range cw.Rules {
			rule, err := compileRule(rw)
			if err != nil {
				log.Printf("rule chain %d (%s): skipping rule %d, failed to compile: %s", cw.ID, cw.Name, rw.ID, err)
				continue
			}
			rules = append(rules, rule)
		}
		ruleChains = append(ruleChains, commands.RuleChain{ID: cw.ID, Name: cw.Name, Rules: rules})
	}
	return ruleChains, nil
}
