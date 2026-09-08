// Package config handles domain configuration, session persistence, and the
// workspace-selection/decoupling logic -- a port of casper_tool.py's
// load_app_domain()/load_auth_domain()/session/workspace handling.
//
// Unlike the Python version, there's no "frozen vs. running from source"
// distinction here: a Go build is always a compiled artifact, there's no
// interpreted-from-source mode to special-case. So app_server.txt/
// auth_server.txt are always read via go:embed (baked in at compile time,
// staged into embedded/ by the build script from the repo-root source-of-
// truth files) -- the env var override already covers "I want to change
// this without rebuilding" for local development, exactly as it did for the
// packaged Python build.
package config

import (
	_ "embed"
	"fmt"
	"os"
	"strings"
)

//go:embed embedded/app_server.txt
var embeddedAppServer string

//go:embed embedded/auth_server.txt
var embeddedAuthServer string

//go:embed embedded/relay_server.txt
var embeddedRelayServer string

// BaseURL turns a bare host[:port] (no scheme, no path) into a scheme://host
// base. Localhost gets plain http; anything else (a real deployed domain)
// gets https.
func BaseURL(domain string) string {
	domain = strings.Trim(strings.TrimSpace(domain), "/")
	scheme := "https"
	if strings.HasPrefix(domain, "localhost") || strings.HasPrefix(domain, "127.0.0.1") {
		scheme = "http"
	}
	return scheme + "://" + domain
}

func BuildAppURL(domain string) string {
	return BaseURL(domain) + "/chat"
}

// LoadAppDomain returns the deployed web app's domain, or "" if unresolvable
// -- not fatal, the agent just won't auto-open a browser tab.
func LoadAppDomain() string {
	if v := os.Getenv("CONTROL_TOOL_APP_DOMAIN"); v != "" {
		return v
	}
	return strings.TrimSpace(embeddedAppServer)
}

// LoadAuthDomain returns the auth service's domain. Unlike LoadAppDomain,
// this is fatal if unresolvable -- there's no graceful fallback when the
// whole sign-in flow depends on it.
func LoadAuthDomain() (string, error) {
	if v := os.Getenv("CONTROL_TOOL_AUTH_DOMAIN"); v != "" {
		return v, nil
	}
	domain := strings.TrimSpace(embeddedAuthServer)
	if domain == "" {
		return "", fmt.Errorf(
			"no auth service configured: rebuild with auth_server.txt present, or set CONTROL_TOOL_AUTH_DOMAIN",
		)
	}
	return domain, nil
}

// LoadRelayDomain returns the relay service's domain (an exact mirror of
// LoadAuthDomain's pattern) -- fatal if unresolvable, since the agent can't
// expose itself to the deployed web app at all without it.
func LoadRelayDomain() (string, error) {
	if v := os.Getenv("CONTROL_TOOL_RELAY_DOMAIN"); v != "" {
		return v, nil
	}
	domain := strings.TrimSpace(embeddedRelayServer)
	if domain == "" {
		return "", fmt.Errorf(
			"no relay service configured: rebuild with relay_server.txt present, or set CONTROL_TOOL_RELAY_DOMAIN",
		)
	}
	return domain, nil
}
