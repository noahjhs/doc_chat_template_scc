package config

import (
	"encoding/json"
	"fmt"
	"net/http"
	"time"

	"casper-agent/internal/commands"
)

// argPatternWire mirrors one entry of auth_service's allowed_args JSON
// (models.CommandTemplateArgPattern) -- v1 only ever enforces zero-slot,
// exact-match patterns (see commands.CommandTemplate), so Slots is decoded
// but not used yet; kept here rather than dropped so a future parameterized
// pass has somewhere to read it from without touching the wire shape.
type argPatternWire struct {
	Pattern string         `json:"pattern"`
	Slots   map[string]any `json:"slots"`
}

type hostCommandTemplateWire struct {
	ID          int              `json:"id"`
	Name        string           `json:"name"`
	Binary      string           `json:"binary"`
	AllowedArgs []argPatternWire `json:"allowed_args"`
	Tier        string           `json:"tier"`
	PathScoped  bool             `json:"path_scoped"`
}

// FetchCommandTemplates GETs this installation's own enabled command
// templates from the auth service, authenticated with device_token -- see
// auth_service/main.py's GET /hosts/command-templates. Mirrors
// ReportPresence's HTTP-call shape. The daemon fetches and caches this
// itself (see cmd/casper/daemon.go's handlePairURL/resumeSession, which
// call commands.Handler.SetCommandTemplates with the result) rather than
// trusting the browser/model to have applied a template correctly -- see
// the "Resources: command templates" plan.
func FetchCommandTemplates(authDomain, deviceToken string) ([]commands.CommandTemplate, error) {
	req, err := http.NewRequest(http.MethodGet, BaseURL(authDomain)+"/hosts/command-templates", nil)
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
		return nil, fmt.Errorf("unexpected status %d fetching command templates", resp.StatusCode)
	}
	var body struct {
		CommandTemplates []hostCommandTemplateWire `json:"command_templates"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return nil, err
	}
	templates := make([]commands.CommandTemplate, 0, len(body.CommandTemplates))
	for _, w := range body.CommandTemplates {
		allowedArgs := make([]string, 0, len(w.AllowedArgs))
		for _, p := range w.AllowedArgs {
			allowedArgs = append(allowedArgs, p.Pattern)
		}
		templates = append(templates, commands.CommandTemplate{
			ID:          w.ID,
			Name:        w.Name,
			Binary:      w.Binary,
			AllowedArgs: allowedArgs,
			Tier:        w.Tier,
			PathScoped:  w.PathScoped,
		})
	}
	return templates, nil
}
