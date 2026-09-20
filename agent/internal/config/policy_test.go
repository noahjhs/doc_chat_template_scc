package config

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func testAuthDomain(server *httptest.Server) string {
	return strings.TrimPrefix(server.URL, "http://")
}

func TestFetchPolicyLayersDecodesPositionalAndOptionShapes(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"policy_layers": []map[string]any{
				{
					"id":   1,
					"name": "npm scripts",
					"rules": []map[string]any{
						{
							"id": 1,
							"positional_constraints": []any{
								"*",
								map[string]any{"whitelist": "^run$"},
							},
							"option_constraints": []map[string]any{
								{"short": "f", "long": "force", "pattern": map[string]any{"whitelist": "^$"}},
								{"long": "verbose", "pattern": map[string]any{}},
								{"long": "output", "pattern": map[string]any{"whitelist": ".+"}},
								{"long": "legacy", "pattern": nil}, // stale row from before this was always a real object
							},
							"tier": "ask",
						},
					},
				},
			},
		})
	}))
	defer server.Close()

	layers, err := FetchPolicyLayers(testAuthDomain(server), "dev-token")
	if err != nil {
		t.Fatalf("unexpected error: %s", err)
	}
	if len(layers) != 1 || len(layers[0].Rules) != 1 {
		t.Fatalf("unexpected shape: %+v", layers)
	}
	rule := layers[0].Rules[0]

	pc := rule.PositionalConstraints
	// Position 0 is the bare string "*" -- a stale pre-this-change wire
	// value (the old positional wildcard sentinel) -- which must still
	// decode safely as a blank ("value not required") pattern, never a
	// crash or a compile error.
	if len(pc) != 2 || pc[0].Whitelist != nil || pc[0].Blacklist != nil {
		t.Fatalf("expected a legacy \"*\" at position 0 to decode as blank (value not required), got %+v", pc)
	}
	if pc[1].Whitelist == nil || !pc[1].Whitelist.MatchString("run") {
		t.Fatalf("expected position 1's whitelist to compile and match \"run\", got %+v", pc[1])
	}

	oc := rule.OptionConstraints
	if len(oc) != 4 {
		t.Fatalf("expected 4 option constraints, got %d", len(oc))
	}
	if oc[0].Pattern.Whitelist == nil || !oc[0].Pattern.Whitelist.MatchString("") || oc[0].Pattern.Whitelist.MatchString("x") {
		t.Fatalf("expected --force's pattern (\"^$\") to require an empty value, got %+v", oc[0].Pattern)
	}
	if oc[1].Pattern.Whitelist != nil || oc[1].Pattern.Blacklist != nil {
		t.Fatalf("expected a blank (any/no value) pattern for --verbose, got %+v", oc[1].Pattern)
	}
	if oc[2].Pattern.Whitelist == nil || !oc[2].Pattern.Whitelist.MatchString("x") {
		t.Fatalf("expected --output's pattern to compile and match, got %+v", oc[2].Pattern)
	}
	// A literal JSON null (a stale pre-this-change row) decodes to the same
	// blank/matches-anything pattern a {} object would -- never a crash.
	if oc[3].Pattern.Whitelist != nil || oc[3].Pattern.Blacklist != nil {
		t.Fatalf("expected a legacy null pattern to decode as blank (matches any/no value), got %+v", oc[3].Pattern)
	}
}

func TestFetchPolicyLayersDecodesCurrentBlacklistDefault(t *testing.T) {
	// casper_service now always sends a real blacklist string, never null --
	// a "not specified" one defaults to BLACKLIST_MATCHES_NOTHING
	// ("[^\s\S]", a character class that can never match anything). This
	// compiles to a real, non-nil regexp (unlike the legacy nil-blacklist
	// case covered above), so confirm it actually behaves as "never
	// matches, so never rejects" rather than assuming it from the string
	// alone.
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"policy_layers": []map[string]any{
				{
					"id":   1,
					"name": "test",
					"rules": []map[string]any{
						{
							"id":                     1,
							"positional_constraints": []any{map[string]any{"whitelist": "", "blacklist": `[^\s\S]`}},
							"option_constraints":     []map[string]any{},
							"tier":                   "allow",
						},
					},
				},
			},
		})
	}))
	defer server.Close()

	layers, err := FetchPolicyLayers(testAuthDomain(server), "dev-token")
	if err != nil {
		t.Fatalf("unexpected error: %s", err)
	}
	pc := layers[0].Rules[0].PositionalConstraints[0]
	if pc.Whitelist == nil || !pc.Whitelist.MatchString("anything") {
		t.Fatalf("expected the empty whitelist to compile and match everything, got %+v", pc)
	}
	if pc.Blacklist == nil {
		t.Fatalf("expected the blacklist default to compile to a real (non-nil) regexp, got %+v", pc)
	}
	if pc.Blacklist.MatchString("") || pc.Blacklist.MatchString("anything") {
		t.Fatalf("expected the blacklist default to never match anything, got %+v", pc)
	}
}

func TestFetchPolicyLayersSkipsRuleWithUncompilablePattern(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"policy_layers": []map[string]any{
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

	layers, err := FetchPolicyLayers(testAuthDomain(server), "dev-token")
	if err != nil {
		t.Fatalf("unexpected error: %s", err)
	}
	if len(layers) != 1 {
		t.Fatalf("expected the policy layer itself to still come through, got %+v", layers)
	}
	// Only rule 2 should have survived -- rule 1's bad regex means it's
	// dropped (logged, not crashed) rather than silently under-enforced.
	if len(layers[0].Rules) != 1 || layers[0].Rules[0].ID != 2 {
		t.Fatalf("expected only rule 2 to survive, got %+v", layers[0].Rules)
	}
}

func TestFetchPolicyLayersDecodesCwdAndPathResolution(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"policy_layers": []map[string]any{
				{
					"id":   1,
					"name": "cwd-scoped",
					"rules": []map[string]any{
						{
							"id": 1,
							"positional_constraints": []any{
								map[string]any{},
								map[string]any{"whitelist": "^safe.*", "path_resolution": "."},
							},
							"option_constraints": []map[string]any{},
							"cwd":                map[string]any{"whitelist": "^/home/.+"},
							"tier":               "allow",
						},
					},
				},
			},
		})
	}))
	defer server.Close()

	layers, err := FetchPolicyLayers(testAuthDomain(server), "dev-token")
	if err != nil {
		t.Fatalf("unexpected error: %s", err)
	}
	rule := layers[0].Rules[0]
	if rule.Cwd.Whitelist == nil || !rule.Cwd.Whitelist.MatchString("/home/alice") {
		t.Fatalf("expected cwd's whitelist to compile and match, got %+v", rule.Cwd)
	}
	pc := rule.PositionalConstraints
	if len(pc) != 2 {
		t.Fatalf("expected 2 positional constraints, got %d", len(pc))
	}
	if pc[0].PathResolution != "" {
		t.Fatalf("expected an absent path_resolution to decode as \"\" (none), got %q", pc[0].PathResolution)
	}
	if pc[1].PathResolution != "." {
		t.Fatalf("expected position 1's path_resolution to decode as \".\", got %q", pc[1].PathResolution)
	}
}

func TestFetchPolicyLayersUnexpectedStatusIsAnError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer server.Close()

	if _, err := FetchPolicyLayers(testAuthDomain(server), "dev-token"); err == nil {
		t.Fatal("expected an error for a non-200 response")
	}
}
