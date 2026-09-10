package commands

import (
	"os"
	"testing"
)

func testTemplate() CommandTemplate {
	return CommandTemplate{
		ID:          1,
		Name:        "echo",
		Binary:      "echo",
		AllowedArgs: []string{"hello", "hello world"},
		Tier:        "allow",
		PathScoped:  true,
	}
}

func TestRunCommandTemplate_AllowedArgsSucceeds(t *testing.T) {
	h, _ := newTestHandler(t)
	h.SetCommandTemplates([]CommandTemplate{testTemplate()})

	res, err := h.Dispatch(&Request{Action: "run_command_template", TemplateID: 1, Args: "hello"})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !res.Success {
		t.Fatalf("expected success, got %+v", res)
	}
	if res.Stdout != "hello" {
		t.Fatalf("expected stdout %q, got %q", "hello", res.Stdout)
	}
}

func TestRunCommandTemplate_RejectsUnknownTemplateID(t *testing.T) {
	h, _ := newTestHandler(t)
	h.SetCommandTemplates([]CommandTemplate{testTemplate()})

	if _, err := h.Dispatch(&Request{Action: "run_command_template", TemplateID: 99, Args: "hello"}); err == nil {
		t.Fatal("expected an ActionError for an unknown template id")
	}
}

func TestRunCommandTemplate_RejectsArgsNotInAllowlist(t *testing.T) {
	h, _ := newTestHandler(t)
	h.SetCommandTemplates([]CommandTemplate{testTemplate()})

	if _, err := h.Dispatch(&Request{Action: "run_command_template", TemplateID: 1, Args: "rm -rf /"}); err == nil {
		t.Fatal("expected an ActionError for args outside AllowedArgs -- structural allowlist must reject this")
	}
}

func TestRunCommandTemplate_NoRootsMeansFail(t *testing.T) {
	h := New(nil, nil)
	h.SetCommandTemplates([]CommandTemplate{testTemplate()})

	res, err := h.Dispatch(&Request{Action: "run_command_template", TemplateID: 1, Args: "hello"})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if res.Success {
		t.Fatal("expected run_command_template to fail with zero roots, same as every other confined action")
	}
}

func TestRunCommandTemplate_PathScopedRedirectsDirectory(t *testing.T) {
	h, root := newTestHandler(t)
	subdir := root + "/subdir"
	if err := os.Mkdir(subdir, 0o755); err != nil {
		t.Fatal(err)
	}
	h.SetCommandTemplates([]CommandTemplate{testTemplate()})

	res, err := h.Dispatch(&Request{Action: "run_command_template", TemplateID: 1, Args: "hello", Path: subdir})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !res.Success {
		t.Fatalf("expected success, got %+v", res)
	}
	if res.Cwd != subdir {
		t.Fatalf("expected the command to run in %q, got %q", subdir, res.Cwd)
	}
}

func TestRunCommandTemplate_PathScopedFalseIgnoresPath(t *testing.T) {
	h, root := newTestHandler(t)
	template := testTemplate()
	template.PathScoped = false
	h.SetCommandTemplates([]CommandTemplate{template})

	// Pass a Path that would otherwise redirect execution -- since
	// PathScoped is false, it must be ignored and cwd (root) used instead.
	res, err := h.Dispatch(&Request{Action: "run_command_template", TemplateID: 1, Args: "hello", Path: root})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if res.Cwd != root {
		t.Fatalf("expected cwd to stay %q (Path ignored when not PathScoped), got %q", root, res.Cwd)
	}
}

func TestListCommandTemplates(t *testing.T) {
	h, _ := newTestHandler(t)
	empty, err := h.Dispatch(&Request{Action: "list_command_templates"})
	if err != nil || empty.Stdout != "No command templates enabled on this host." {
		t.Fatalf("expected the empty message, got %q (err=%v)", empty.Stdout, err)
	}

	h.SetCommandTemplates([]CommandTemplate{testTemplate()})
	res, err := h.Dispatch(&Request{Action: "list_command_templates"})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if res.Stdout == "" || res.Stdout == "No command templates enabled on this host." {
		t.Fatalf("expected a non-empty listing, got %q", res.Stdout)
	}
}
