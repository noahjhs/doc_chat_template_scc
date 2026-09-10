package commands

import (
	"fmt"
	"os/exec"
	"strings"
)

// CommandTemplate is the daemon's own cached copy of a user-authored rule
// for invoking one CLI command -- fetched from auth_service (see
// agent/internal/config's FetchCommandTemplates) and cached here so
// enforcement never has to trust the browser/model to have applied a
// template correctly; see the "Resources: command templates" plan for the
// full reasoning. Tier is carried through purely for runListCommandTemplates'
// own reporting -- it is *not* enforced anywhere in this package. Deciding
// whether a call needs human approval first is pages/chat.py's job: the
// real security boundary for any /api/command call is command_key
// possession, not a tier this package has no independent way to verify was
// actually honored upstream anyway.
type CommandTemplate struct {
	ID     int
	Name   string
	Binary string
	// v1 only ever holds zero-slot, exact-match patterns (e.g. "run
	// build") -- see auth_service/models.py's CommandTemplateArgPattern
	// for why the stored shape allows for future parameterized slots even
	// though nothing here reads them yet.
	AllowedArgs []string
	Tier        string
	PathScoped  bool
}

// SetCommandTemplates replaces the entire cached set -- called after every
// fetch from auth_service (pairing, resume, or an explicit refresh
// triggered from the web app), never merged incrementally.
func (h *Handler) SetCommandTemplates(templates []CommandTemplate) {
	h.mu.Lock()
	h.templates = templates
	h.mu.Unlock()
}

// CommandTemplates returns a snapshot of the currently cached templates.
func (h *Handler) CommandTemplates() []CommandTemplate {
	h.mu.Lock()
	defer h.mu.Unlock()
	return append([]CommandTemplate(nil), h.templates...)
}

// SetRefreshCommandTemplatesFunc injects the callback runRefreshCommandTemplates
// invokes -- set once at startup by cmd/casper/main.go (after daemonState
// exists, so the closure can reach its own getDeviceToken/authDomain; nil
// until then, and always nil in tests, same nil-tolerant posture as
// pickAndPersistDir above) to actually fetch+apply a fresh set via
// config.FetchCommandTemplates/SetCommandTemplates. Kept as an injected
// callback rather than importing agent/internal/config directly here,
// since this package has no business knowing about auth domains/device
// tokens -- those are daemon-orchestration concerns, same reasoning
// pickAndPersistDir/onRootAdded already established.
func (h *Handler) SetRefreshCommandTemplatesFunc(fn func()) {
	h.mu.Lock()
	h.refreshCommandTemplatesFn = fn
	h.mu.Unlock()
}

// runRefreshCommandTemplates is not part of run_local_command's model-facing
// action enum (see COMMAND_CATEGORIES in utils/sidebar.py) -- only the web
// app's Resources page triggers it, same posture as runAddDirectory. Runs
// the fetch in a goroutine rather than blocking the request/response --
// mirrors runAddDirectory's own reasoning (the relay's /api/command timeout
// is a hard 15s ceiling); the caller is expected to poll (e.g. re-fetch
// GET /hosts, which carries the same command_templates data) rather than
// wait on this response for the refreshed set.
func (h *Handler) runRefreshCommandTemplates(_ *Request) (Result, error) {
	h.mu.Lock()
	fn := h.refreshCommandTemplatesFn
	h.mu.Unlock()
	if fn == nil {
		return Result{}, &ActionError{Detail: "Refreshing command templates isn't supported on this installation."}
	}
	go fn()
	return h.ok("Refreshing command templates -- check back shortly.", ""), nil
}

func (h *Handler) findCommandTemplate(id int) (CommandTemplate, bool) {
	h.mu.Lock()
	defer h.mu.Unlock()
	for _, t := range h.templates {
		if t.ID == id {
			return t, true
		}
	}
	return CommandTemplate{}, false
}

// runListCommandTemplates reports the daemon's own currently-cached set --
// model-visible, mirrors runListDirectories -- so the model can see what's
// actually available right now rather than working off a stale schema.
func (h *Handler) runListCommandTemplates(_ *Request) (Result, error) {
	templates := h.CommandTemplates()
	if len(templates) == 0 {
		return h.ok("No command templates enabled on this host.", ""), nil
	}
	var lines []string
	for _, t := range templates {
		lines = append(
			lines,
			fmt.Sprintf("#%d %s: %s %s (%s)", t.ID, t.Name, t.Binary, strings.Join(t.AllowedArgs, " | "), t.Tier),
		)
	}
	return h.ok(strings.Join(lines, "\n"), ""), nil
}

// runRunCommandTemplate is the daemon's own enforcement point -- purely
// structural, same as every other action in commands.go: this binary,
// exactly this argv option, optionally confined to a directory inside
// h.roots. req.Path (reused from every path-taking action above) picks
// which confined directory to run in when the template is PathScoped;
// otherwise this just uses whatever directory is currently tracked as cwd
// -- which is itself always already inside h.roots, an invariant
// maintained by AddRoot/runCd, so execution is never actually unconfined
// either way.
func (h *Handler) runRunCommandTemplate(req *Request) (Result, error) {
	template, ok := h.findCommandTemplate(req.TemplateID)
	if !ok {
		return Result{}, &ActionError{Detail: "Unknown or no longer enabled command template."}
	}

	matched := false
	for _, allowed := range template.AllowedArgs {
		if allowed == req.Args {
			matched = true
			break
		}
	}
	if !matched {
		return Result{}, &ActionError{Detail: "Those arguments aren't allowed for this command template."}
	}

	if len(h.Roots()) == 0 {
		return h.fail("No directories added yet -- add one first (the \"+\" button in the workspace browser)."), nil
	}

	dir := h.getCwd()
	if template.PathScoped && req.Path != "" {
		target, err := h.resolvePath(req.Path, true, true)
		if err != nil {
			return Result{}, err
		}
		dir = target
	}

	var argv []string
	if req.Args != "" {
		argv = strings.Fields(req.Args)
	}

	// sandboxedCommand (seatbelt_darwin.go/seatbelt_other.go) wraps this in
	// a macOS Seatbelt profile confined to h.Roots() when available --
	// defense-in-depth on top of the argv-allowlist check just above, not
	// a replacement for it.
	cmd, cleanup := sandboxedCommand(template.Binary, argv, h.Roots())
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
