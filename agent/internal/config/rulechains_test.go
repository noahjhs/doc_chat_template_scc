package config

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestFetchRuleChainsDecodesPositionalAndOptionShapes(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"rule_chains": []map[string]any{
				{
					"id":   1,
					"name": "npm scripts",
					"rules": []map[string]any{
						{
							"id": 1,
							"positional_constraints": []any{
								"*",
								map[string]any{"whitelist": "^run$"},
								map[string]any{"whitelist": "{roots}"},
							},
							"option_constraints": []map[string]any{
								{"short": "f", "long": "force", "pattern": nil},
								{"long": "verbose", "pattern": "*"},
								{"long": "output", "pattern": map[string]any{"whitelist": ".+"}},
							},
							"tier": "ask",
						},
					},
				},
			},
		})
	}))
	defer server.Close()

	ruleChains, err := FetchRuleChains(testAuthDomain(server), "dev-token")
	if err != nil {
		t.Fatalf("unexpected error: %s", err)
	}
	if len(ruleChains) != 1 || len(ruleChains[0].Rules) != 1 {
		t.Fatalf("unexpected shape: %+v", ruleChains)
	}
	rule := ruleChains[0].Rules[0]

	pc := rule.PositionalConstraints
	if len(pc) != 3 || !pc[0].Wildcard {
		t.Fatalf("expected position 0 to be a wildcard, got %+v", pc)
	}
	if pc[1].Whitelist == nil || !pc[1].Whitelist.MatchString("run") {
		t.Fatalf("expected position 1's whitelist to compile and match \"run\", got %+v", pc[1])
	}
	if !pc[2].WhitelistRoots {
		t.Fatalf("expected position 2 to be WhitelistRoots, got %+v", pc[2])
	}

	oc := rule.OptionConstraints
	if len(oc) != 3 {
		t.Fatalf("expected 3 option constraints, got %d", len(oc))
	}
	if oc[0].Pattern != nil {
		t.Fatalf("expected a nil pattern (no value allowed) for --force, got %+v", oc[0].Pattern)
	}
	if oc[1].Pattern == nil || !oc[1].Pattern.Wildcard {
		t.Fatalf("expected a wildcard pattern for --verbose, got %+v", oc[1].Pattern)
	}
	if oc[2].Pattern == nil || oc[2].Pattern.Whitelist == nil || !oc[2].Pattern.Whitelist.MatchString("x") {
		t.Fatalf("expected --output's pattern to compile and match, got %+v", oc[2].Pattern)
	}
}

func TestFetchRuleChainsSkipsRuleWithUncompilablePattern(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"rule_chains": []map[string]any{
				{
					"id":   1,
					"name": "mixed",
					"rules": []map[string]any{
						{
							"id":                     1,
							"positional_constraints": []any{map[string]any{"whitelist": "["}}, // invalid regex
							"option_constraints":     []map[string]any{},
							"tier":                   "allow",
						},
						{
							"id":                     2,
							"positional_constraints": []any{"*"},
							"option_constraints":     []map[string]any{},
							"tier":                   "ask",
						},
					},
				},
			},
		})
	}))
	defer server.Close()

	ruleChains, err := FetchRuleChains(testAuthDomain(server), "dev-token")
	if err != nil {
		t.Fatalf("unexpected error: %s", err)
	}
	if len(ruleChains) != 1 {
		t.Fatalf("expected the rule chain itself to still come through, got %+v", ruleChains)
	}
	// Only rule 2 should have survived -- rule 1's bad regex means it's
	// dropped (logged, not crashed) rather than silently under-enforced.
	if len(ruleChains[0].Rules) != 1 || ruleChains[0].Rules[0].ID != 2 {
		t.Fatalf("expected only rule 2 to survive, got %+v", ruleChains[0].Rules)
	}
}

func TestFetchRuleChainsUnexpectedStatusIsAnError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer server.Close()

	if _, err := FetchRuleChains(testAuthDomain(server), "dev-token"); err == nil {
		t.Fatal("expected an error for a non-200 response")
	}
}
