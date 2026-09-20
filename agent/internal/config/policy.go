package config

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"time"

	"casper-agent/internal/commands"
)

// FetchPolicyLayers GETs this installation's own enabled policy layers from
// the auth service, authenticated with device_token -- see
// casper_service/main.py's GET /hosts/policy-layers. Mirrors ReportPresence's
// HTTP-call shape. The daemon fetches and caches this itself (see
// cmd/casper/daemon.go's handlePairURL/resumeSession, which call
// commands.Handler.SetPolicyLayers with the result) rather than trusting
// the browser/model to have applied a rule correctly. A layer's own rules
// arrive already ordered by position -- see main.py's _policy_layer_rules
// -- so no re-sorting happens here (composePolicy in commands/policy.go
// handles ordering ACROSS layers, separately, at match time). Decoding and
// compiling each rule (commands.PolicyLayerWire/commands.CompileRule) is
// shared with commands' own eval_policy action, which decodes the exact
// same wire shape inline on a request rather than fetching it -- see that
// package's policy_wire.go.
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
		PolicyLayers []commands.PolicyLayerWire `json:"policy_layers"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return nil, err
	}
	layers := make([]commands.PolicyLayer, 0, len(body.PolicyLayers))
	for _, lw := range body.PolicyLayers {
		rules := make([]commands.Rule, 0, len(lw.Rules))
		for _, rw := range lw.Rules {
			rule, err := commands.CompileRule(rw)
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
