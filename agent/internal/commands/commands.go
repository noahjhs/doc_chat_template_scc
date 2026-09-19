// Package commands implements the confined set of filesystem/shell
// operations the remote assistant can invoke. The actual security boundary
// is resolvePath(): every path-taking action funnels through it, and it
// rejects anything that would resolve (after following symlinks) outside
// the Handler's own homeRoot -- a single fixed directory (the user's home
// directory by default, computed once at startup -- see cmd/casper/main.go)
// rather than a dynamic, user-managed set. Everything else -- which
// directory a shell command runs in, whether a given argument value is
// safe -- is the policy schema's job (see policy.go's Rule.Cwd and
// Pattern.PathResolution), not this package's.
package commands

import (
	"encoding/base64"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
)

// Request mirrors casper_tool.py's CommandRequest Pydantic model -- trimmed
// to just what the daemon's remaining actions (read_file/write_file/
// add_directory/policy-layer actions/run_shell_command) actually use, now
// that run_local_command's fixed allowlist (which needed Destination/
// Pattern/Lines/Limit for cp/mv/grep/find/head/tail) is retired.
type Request struct {
	Action string `json:"action"`
	Path   string `json:"path,omitempty"`
	// Content is base64-encoded file bytes -- only used by write_file (see
	// runReadFile/runWriteFile below), which the web app's cross-host and
	// host<->server-storage transfer tools drive. Base64 rather than raw
	// text, so transferred files survive round-tripping arbitrary binary
	// content intact through JSON, which requires valid UTF-8.
	Content string `json:"content,omitempty"`
	// PositionalArgs/Options are only used by run_shell_command (see
	// policy.go). No argv string, ever -- the model supplies STRUCTURED
	// arguments (PositionalArgs index 0 is the binary itself, mapping 1:1
	// onto a rule's positional constraint list; Options is a list of named
	// entries, not raw tokens), and the daemon is what constructs the
	// actual argv for exec.Command, once, only after a rule has matched --
	// see policy.go's buildArgv. No policy/layer ID here: the call is
	// host-scoped, not layer-scoped -- the daemon composes every currently
	// cached PolicyLayer (already scoped to this host, since
	// GET /hosts/policy-layers is device-token-gated) into this host's
	// Policy and evaluates against the whole thing (see runRunShellCommand)
	// rather than the model picking one layer to check against. Path
	// (above) is reused by run_shell_command too, to pick which confined
	// directory to run in (unconditional whenever given -- see
	// runRunShellCommand).
	PositionalArgs []string        `json:"positional_args,omitempty"`
	Options        []RequestOption `json:"options,omitempty"`
}

// RequestOption is one model-supplied option entry for run_shell_command --
// Value is nil for a valueless option (e.g. "--force"), matching
// OptionConstraint.Pattern's own nil-means-"no value" convention.
type RequestOption struct {
	Short string  `json:"short,omitempty"`
	Long  string  `json:"long,omitempty"`
	Value *string `json:"value,omitempty"`
}

// Result mirrors the plain dicts _ok()/_fail()/run_git() return -- FastAPI
// serializes those dicts as-is (no Pydantic response_model), so most
// responses have no "exit_code" key at all; ExitCode is a pointer
// specifically so omitempty can tell "absent" apart from "zero" (a real,
// meaningful git exit code).
type Result struct {
	Success  bool   `json:"success"`
	Cwd      string `json:"cwd"`
	Stdout   string `json:"stdout"`
	Stderr   string `json:"stderr"`
	ExitCode *int   `json:"exit_code,omitempty"`
}

// ActionError is a client-facing failure (HTTP 400 in the Python version's
// HTTPException) -- distinct from a Result{Success:false,...}, which is a
// well-formed "the command ran and failed" response.
type ActionError struct {
	Detail string
}

func (e *ActionError) Error() string { return e.Detail }

// Handler holds the confined-execution state -- one instance per running
// agent, guarded by a mutex since the HTTP server dispatches concurrently.
// homeRoot is set once at construction (see cmd/casper/main.go) and never
// mutated afterward in v1 -- there's no more "cd" action, and no more
// dynamic "+"-button workspace management, so a separate mutable "current
// directory" no longer earns its keep. Kept as ordinary state (not a
// package const) purely so a future version can make it settable again
// without another structural change; nothing in this package currently
// changes it after New(). Every command just fails with a clear "no
// directory configured" message if homeRoot is ever empty (e.g. a
// misconfigured test Handler), same posture the old "no directories added
// yet" empty-roots case had.
type Handler struct {
	mu                    sync.Mutex
	homeRoot              string
	policyLayers          []PolicyLayer // see policy.go -- the daemon's own cached copy of policy layers attached to this host, fetched from auth_service
	refreshPolicyLayersFn func()        // see policy.go's SetRefreshPolicyLayersFunc
}

func New(homeRoot string) *Handler {
	return &Handler{homeRoot: homeRoot}
}

// HomeRoot returns the single directory every path-taking action is
// confined to -- also what cmd/casper/daemon.go reports via presence (see
// config.ReportPresence) so auth_service can cache it for its own
// best-effort "." path_resolution preview (see policy.py's module
// docstring).
func (h *Handler) HomeRoot() string {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.homeRoot
}

// isConfined reports whether resolved is inside (or is exactly) homeRoot.
// With an empty homeRoot this is unconditionally false -- that case gets
// its own clearer error message in resolvePath.
func (h *Handler) isConfined(resolved string) bool {
	root := h.HomeRoot()
	if root == "" {
		return false
	}
	return resolved == root || strings.HasPrefix(resolved, root+string(os.PathSeparator))
}

func expandUser(path string) string {
	if path == "~" || strings.HasPrefix(path, "~/") {
		home, err := os.UserHomeDir()
		if err != nil {
			return path
		}
		if path == "~" {
			return home
		}
		return filepath.Join(home, path[2:])
	}
	return path
}

// realpath mimics Python's os.path.realpath: resolves symlinks in whatever
// prefix of the path actually exists, and appends the rest literally.
// filepath.EvalSymlinks (Go's closest stdlib equivalent) requires the whole
// path to exist, which would break resolving e.g. a "mkdir"/"touch"/"cp"
// destination's parent for the confinement check below before the target
// itself exists -- getting this right matters, since it's the actual
// symlink-escape defense, not just a cosmetic path-cleanup step.
func realpath(path string) (string, error) {
	if !filepath.IsAbs(path) {
		abs, err := filepath.Abs(path)
		if err != nil {
			return "", err
		}
		path = abs
	}
	path = filepath.Clean(path)

	existing := path
	var suffix []string
	for {
		if _, err := os.Lstat(existing); err == nil {
			break
		}
		parent := filepath.Dir(existing)
		if parent == existing {
			break // reached the filesystem root; nothing more to strip
		}
		suffix = append([]string{filepath.Base(existing)}, suffix...)
		existing = parent
	}

	resolvedExisting, err := filepath.EvalSymlinks(existing)
	if err != nil {
		return "", err
	}
	if len(suffix) == 0 {
		return resolvedExisting, nil
	}
	return filepath.Join(append([]string{resolvedExisting}, suffix...)...), nil
}

// resolveJoinedPath joins value against base (unless value is already
// absolute, or "~"/"~/..."-prefixed, in which case it's expanded/used as-is
// -- see expandUser) and resolves the result via realpath() (real symlink
// resolution). Factored out of resolvePath so policy.go's own per-argument
// PathResolutionDot resolution (see resolveForMatch) can reuse the exact
// same join+realpath step without duplicating it, and without pulling in
// resolvePath's own isConfined/mustExist/mustBeDir checks -- those are
// specific to resolvePath's own callers (req.Path, read_file/write_file),
// not to matching a rule against a hypothetical resolved value.
func resolveJoinedPath(base, value string) (string, error) {
	expanded := expandUser(value)
	if expanded == "" {
		expanded = "."
	}
	if filepath.IsAbs(expanded) {
		return realpath(expanded)
	}
	return realpath(filepath.Join(base, expanded))
}

// resolvePath is the security boundary every handler below funnels through.
// Resolves path (absolute, or relative to homeRoot) and confirms the result
// stays inside homeRoot.
func (h *Handler) resolvePath(path string, mustExist, mustBeDir bool) (string, error) {
	root := h.HomeRoot()
	resolved, err := resolveJoinedPath(root, path)
	if err != nil {
		return "", &ActionError{Detail: fmt.Sprintf("No such file or directory: %s", path)}
	}

	if !h.isConfined(resolved) {
		if root == "" {
			return "", &ActionError{Detail: "No directory configured on this installation."}
		}
		return "", &ActionError{Detail: fmt.Sprintf("Path is outside the allowed directory: %s", root)}
	}
	if mustExist {
		if _, err := os.Stat(resolved); err != nil {
			return "", &ActionError{Detail: fmt.Sprintf("No such file or directory: %s", resolved)}
		}
	}
	if mustBeDir {
		info, err := os.Stat(resolved)
		if err != nil || !info.IsDir() {
			return "", &ActionError{Detail: fmt.Sprintf("Not a directory: %s", resolved)}
		}
	}
	return resolved, nil
}

func require(value, fieldName, action string) (string, error) {
	if value == "" {
		return "", &ActionError{Detail: fmt.Sprintf("'%s' is required for the '%s' action.", fieldName, action)}
	}
	return value, nil
}

func cap(text string, maxChars int) string {
	r := []rune(text)
	if len(r) <= maxChars {
		return text
	}
	return string(r[:maxChars]) + fmt.Sprintf("\n... [truncated, %d more characters]", len(r)-maxChars)
}

func (h *Handler) ok(stdout, stderr string) Result {
	return Result{Success: true, Cwd: h.HomeRoot(), Stdout: stdout, Stderr: stderr}
}

func (h *Handler) fail(stderr string) Result {
	return Result{Success: false, Cwd: h.HomeRoot(), Stdout: "", Stderr: stderr}
}

// --- File transfer: binary-safe. Backs the web app's cross-host and
// host<->server-storage transfer tools (see pages/chat.py's TRANSFER_TOOL).
// A different (and larger-blast-radius) operation from run_shell_command --
// these move whole files directly, so they get their own dedicated action
// rather than being expressible as a shell invocation.

const maxTransferFileBytes = 10 * 1024 * 1024 // 10MB -- base64+JSON+relay overhead considered; configs/scripts/small datasets, not media

func (h *Handler) runReadFile(req *Request) (Result, error) {
	path, err := require(req.Path, "path", "read_file")
	if err != nil {
		return Result{}, err
	}
	target, err := h.resolvePath(path, true, false)
	if err != nil {
		return Result{}, err
	}
	info, err := os.Stat(target)
	if err != nil {
		return Result{}, err
	}
	if info.IsDir() {
		return h.fail(fmt.Sprintf("Is a directory: %s", target)), nil
	}
	if info.Size() > maxTransferFileBytes {
		return h.fail(fmt.Sprintf("File too large to transfer (%d bytes, max %d).", info.Size(), maxTransferFileBytes)), nil
	}
	data, err := os.ReadFile(target)
	if err != nil {
		return Result{}, err
	}
	return h.ok(base64.StdEncoding.EncodeToString(data), ""), nil
}

func (h *Handler) runWriteFile(req *Request) (Result, error) {
	path, err := require(req.Path, "path", "write_file")
	if err != nil {
		return Result{}, err
	}
	if req.Content == "" {
		return Result{}, &ActionError{Detail: "'content' is required for the 'write_file' action."}
	}
	data, err := base64.StdEncoding.DecodeString(req.Content)
	if err != nil {
		return Result{}, &ActionError{Detail: "Invalid base64 content."}
	}
	if len(data) > maxTransferFileBytes {
		return Result{}, &ActionError{Detail: fmt.Sprintf("File too large to transfer (%d bytes, max %d).", len(data), maxTransferFileBytes)}
	}
	// mustExist=false, mustBeDir=false -- like touch/cp's destination,
	// write_file creates the file if it doesn't exist yet (and overwrites
	// it if it does).
	target, err := h.resolvePath(path, false, false)
	if err != nil {
		return Result{}, err
	}
	if err := os.WriteFile(target, data, 0o644); err != nil {
		return h.fail(err.Error()), nil
	}
	return h.ok(fmt.Sprintf("Wrote %d bytes to %s", len(data), target), ""), nil
}

// Dispatch mirrors ACTION_HANDLERS -- returns (nil, ActionError) for an
// unrecognized action so the HTTP layer can turn that into the same 400 the
// Python version returns.
func (h *Handler) Dispatch(req *Request) (Result, error) {
	switch req.Action {
	case "read_file":
		return h.runReadFile(req)
	case "write_file":
		return h.runWriteFile(req)
	case "list_policy_layers":
		return h.runListPolicyLayers(req)
	case "run_shell_command":
		return h.runRunShellCommand(req)
	case "refresh_policy_layers":
		return h.runRefreshPolicyLayers(req)
	default:
		return Result{}, &ActionError{Detail: "Action not authorized."}
	}
}
