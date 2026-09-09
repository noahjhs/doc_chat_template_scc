// Package commands implements the confined, allowlisted set of filesystem
// commands the remote assistant can invoke -- a direct port of
// casper_tool.py's resolve_path()/ACTION_HANDLERS/run_*() functions. The
// actual security boundary is resolvePath(): every command funnels through
// it, and it rejects anything that would resolve (after following symlinks)
// outside RootDir.
package commands

import (
	"encoding/base64"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
)

// Request mirrors casper_tool.py's CommandRequest Pydantic model. Lines/Limit
// default to 10/5 respectively when the field is absent from the request
// JSON -- unlike Pydantic, Go's json package leaves an absent int field at
// its zero value, so ApplyDefaults() below does that substitution by hand.
type Request struct {
	Action      string `json:"action"`
	Path        string `json:"path,omitempty"`
	Destination string `json:"destination,omitempty"`
	Pattern     string `json:"pattern,omitempty"`
	Lines       int    `json:"lines,omitempty"`
	Limit       int    `json:"limit,omitempty"`
	// Content is base64-encoded file bytes -- only used by write_file (see
	// runReadFile/runWriteFile below), which the web app's cross-host and
	// host<->server-storage transfer tools drive. Base64 rather than raw
	// text: unlike cat/head/tail (which assume and display text),
	// transferred files need to survive round-tripping arbitrary binary
	// content intact through JSON, which requires valid UTF-8.
	Content string `json:"content,omitempty"`
}

// ApplyDefaults matches CommandRequest's Pydantic field defaults (lines=10,
// limit=5) -- called once, right after JSON-decoding a request.
func (r *Request) ApplyDefaults() {
	if r.Lines == 0 {
		r.Lines = 10
	}
	if r.Limit == 0 {
		r.Limit = 5
	}
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

// Handler holds the confined-workspace state (the tracked "current
// directory" commands like cd/ls resolve relative paths against) -- one
// instance per running agent, guarded by a mutex since the HTTP server
// dispatches concurrently.
type Handler struct {
	RootDir string

	mu         sync.Mutex
	currentDir string
}

func New(rootDir string) *Handler {
	return &Handler{RootDir: rootDir, currentDir: rootDir}
}

func (h *Handler) getCwd() string {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.currentDir
}

func (h *Handler) setCwd(dir string) {
	h.mu.Lock()
	h.currentDir = dir
	h.mu.Unlock()
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

// resolvePath is the security boundary every handler below funnels through.
// Resolves path (absolute, or relative to the tracked current directory) and
// confirms the result stays inside h.RootDir.
func (h *Handler) resolvePath(path string, mustExist, mustBeDir bool) (string, error) {
	base := h.getCwd()
	expanded := expandUser(path)
	if expanded == "" {
		expanded = "."
	}
	var joined string
	if filepath.IsAbs(expanded) {
		joined = expanded
	} else {
		joined = filepath.Join(base, expanded)
	}
	resolved, err := realpath(joined)
	if err != nil {
		return "", &ActionError{Detail: fmt.Sprintf("No such file or directory: %s", joined)}
	}

	if resolved != h.RootDir && !strings.HasPrefix(resolved, h.RootDir+string(os.PathSeparator)) {
		return "", &ActionError{Detail: fmt.Sprintf("Path is outside the allowed directory: %s", h.RootDir)}
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
	return Result{Success: true, Cwd: h.getCwd(), Stdout: stdout, Stderr: stderr}
}

func (h *Handler) fail(stderr string) Result {
	return Result{Success: false, Cwd: h.getCwd(), Stdout: "", Stderr: stderr}
}

// --- Git: still shells out to the git binary, same as the Python version. -

var gitCommands = map[string][]string{
	"status": {"git", "status", "--porcelain"},
	"branch": {"git", "branch", "-a"},
	// "log" gets its limit argument appended at call time.
	"log": {"git", "log", "--oneline", "-n"},
}

func (h *Handler) runGit(req *Request) (Result, error) {
	baseCmd := append([]string{}, gitCommands[req.Action]...)
	if req.Action == "log" {
		baseCmd = append(baseCmd, strconv.Itoa(req.Limit))
	}
	cwd := h.getCwd()
	cmd := exec.Command(baseCmd[0], baseCmd[1:]...)
	cmd.Dir = cwd
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
			Cwd:      cwd,
			Stdout:   strings.TrimSpace(stdout.String()),
			Stderr:   strings.TrimSpace(stderr.String()),
			ExitCode: &exitCode,
		}, nil
	}
	return Result{Success: true, Cwd: cwd, Stdout: strings.TrimSpace(stdout.String()), Stderr: strings.TrimSpace(stderr.String())}, nil
}

// --- Navigation ------------------------------------------------------------

func (h *Handler) runPwd(_ *Request) (Result, error) {
	return h.ok(h.getCwd(), ""), nil
}

func (h *Handler) runCd(req *Request) (Result, error) {
	path, err := require(req.Path, "path", "cd")
	if err != nil {
		return Result{}, err
	}
	target, err := h.resolvePath(path, true, true)
	if err != nil {
		return Result{}, err
	}
	h.setCwd(target)
	return Result{Success: true, Cwd: target, Stdout: target, Stderr: ""}, nil
}

func (h *Handler) runLs(req *Request) (Result, error) {
	path := req.Path
	if path == "" {
		path = "."
	}
	target, err := h.resolvePath(path, true, true)
	if err != nil {
		return Result{}, err
	}
	entries, err := os.ReadDir(target)
	if err != nil {
		return Result{}, err
	}
	sort.Slice(entries, func(i, j int) bool { return entries[i].Name() < entries[j].Name() })
	var lines []string
	for _, entry := range entries {
		kind := "f"
		var size int64
		info, err := entry.Info() // Lstat-equivalent: does not follow symlinks, matching os.scandir(follow_symlinks=False)
		if err == nil {
			size = info.Size()
			if info.IsDir() {
				kind = "d"
			}
		}
		lines = append(lines, fmt.Sprintf("%s %10d %s", kind, size, entry.Name()))
	}
	return h.ok(strings.Join(lines, "\n"), ""), nil
}

func (h *Handler) runTree(req *Request) (Result, error) {
	const maxDepth = 4
	path := req.Path
	if path == "" {
		path = "."
	}
	target, err := h.resolvePath(path, true, true)
	if err != nil {
		return Result{}, err
	}
	var lines []string
	var walk func(dir, prefix string, depth int)
	walk = func(dir, prefix string, depth int) {
		if depth > maxDepth {
			return
		}
		entries, err := os.ReadDir(dir)
		if err != nil {
			lines = append(lines, fmt.Sprintf("%s[error: %s]", prefix, err))
			return
		}
		sort.Slice(entries, func(i, j int) bool { return entries[i].Name() < entries[j].Name() })
		for _, entry := range entries {
			isDir := entry.IsDir()
			suffix := ""
			if isDir {
				suffix = "/"
			}
			lines = append(lines, fmt.Sprintf("%s%s%s", prefix, entry.Name(), suffix))
			if isDir {
				walk(filepath.Join(dir, entry.Name()), prefix+"  ", depth+1)
			}
		}
	}
	walk(target, "", 1)
	return h.ok(cap(strings.Join(lines, "\n"), 20000), ""), nil
}

// --- Management: intentionally no recursive delete -- "rm" only removes a
// file, "rmdir" only an already-empty directory, same as the real shell
// builtins. Combined with RootDir confinement, that caps the worst case to
// "delete one file/empty dir inside the allowed tree", never a recursive
// wipe. ----------------------------------------------------------------------

func (h *Handler) runMkdir(req *Request) (Result, error) {
	path, err := require(req.Path, "path", "mkdir")
	if err != nil {
		return Result{}, err
	}
	target, err := h.resolvePath(path, false, false)
	if err != nil {
		return Result{}, err
	}
	if _, statErr := os.Stat(target); statErr == nil {
		return h.fail(fmt.Sprintf("Already exists: %s", target)), nil
	}
	if err := os.MkdirAll(target, 0o755); err != nil {
		return Result{}, err
	}
	return h.ok(fmt.Sprintf("Created %s", target), ""), nil
}

func (h *Handler) runTouch(req *Request) (Result, error) {
	path, err := require(req.Path, "path", "touch")
	if err != nil {
		return Result{}, err
	}
	target, err := h.resolvePath(path, false, false)
	if err != nil {
		return Result{}, err
	}
	if info, statErr := os.Stat(target); statErr == nil && info.IsDir() {
		return h.fail(fmt.Sprintf("Is a directory: %s", target)), nil
	}
	now := timeNow()
	if _, statErr := os.Stat(target); statErr != nil {
		f, err := os.Create(target)
		if err != nil {
			return Result{}, err
		}
		f.Close()
	} else if err := os.Chtimes(target, now, now); err != nil {
		return Result{}, err
	}
	return h.ok(fmt.Sprintf("Touched %s", target), ""), nil
}

func (h *Handler) runCp(req *Request) (Result, error) {
	path, err := require(req.Path, "path", "cp")
	if err != nil {
		return Result{}, err
	}
	dest, err := require(req.Destination, "destination", "cp")
	if err != nil {
		return Result{}, err
	}
	src, err := h.resolvePath(path, true, false)
	if err != nil {
		return Result{}, err
	}
	dst, err := h.resolvePath(dest, false, false)
	if err != nil {
		return Result{}, err
	}
	info, err := os.Stat(src)
	if err != nil {
		return Result{}, err
	}
	if info.IsDir() {
		if err := copyTree(src, dst); err != nil {
			return Result{}, err
		}
	} else {
		if err := copyFile(src, dst); err != nil {
			return Result{}, err
		}
	}
	return h.ok(fmt.Sprintf("Copied %s -> %s", src, dst), ""), nil
}

func (h *Handler) runMv(req *Request) (Result, error) {
	path, err := require(req.Path, "path", "mv")
	if err != nil {
		return Result{}, err
	}
	dest, err := require(req.Destination, "destination", "mv")
	if err != nil {
		return Result{}, err
	}
	src, err := h.resolvePath(path, true, false)
	if err != nil {
		return Result{}, err
	}
	dst, err := h.resolvePath(dest, false, false)
	if err != nil {
		return Result{}, err
	}
	if err := os.Rename(src, dst); err != nil {
		return Result{}, err
	}
	return h.ok(fmt.Sprintf("Moved %s -> %s", src, dst), ""), nil
}

func (h *Handler) runRm(req *Request) (Result, error) {
	path, err := require(req.Path, "path", "rm")
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
		return h.fail(fmt.Sprintf("Is a directory (use 'rmdir' for an empty directory): %s", target)), nil
	}
	if err := os.Remove(target); err != nil {
		return Result{}, err
	}
	return h.ok(fmt.Sprintf("Removed %s", target), ""), nil
}

func (h *Handler) runRmdir(req *Request) (Result, error) {
	path, err := require(req.Path, "path", "rmdir")
	if err != nil {
		return Result{}, err
	}
	target, err := h.resolvePath(path, true, true)
	if err != nil {
		return Result{}, err
	}
	if err := os.Remove(target); err != nil {
		return h.fail(err.Error()), nil
	}
	return h.ok(fmt.Sprintf("Removed %s", target), ""), nil
}

// --- Viewing & Searching -----------------------------------------------------

func readText(path string) (string, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	return cap(string(data), 20000), nil
}

// --- File transfer: binary-safe, unlike cat/head/tail's text-only, ---------
// truncated-for-display reads. Backs the web app's cross-host and
// host<->server-storage transfer tools (see pages/chat.py's TRANSFER_TOOL).
// Not reachable from the general run_local_command schema -- these move
// whole files, a different (and larger-blast-radius) operation from every
// other allowlisted command, so they get their own dedicated tool rather
// than folding into run_local_command's action enum.

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

func (h *Handler) runCat(req *Request) (Result, error) {
	path, err := require(req.Path, "path", "cat")
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
	text, err := readText(target)
	if err != nil {
		return Result{}, err
	}
	return h.ok(text, ""), nil
}

func (h *Handler) runLess(req *Request) (Result, error) {
	// No interactive paging over HTTP -- same as 'cat', just capped output.
	return h.runCat(req)
}

func (h *Handler) runHead(req *Request) (Result, error) {
	path, err := require(req.Path, "path", "head")
	if err != nil {
		return Result{}, err
	}
	target, err := h.resolvePath(path, true, false)
	if err != nil {
		return Result{}, err
	}
	lines, err := readLines(target)
	if err != nil {
		return Result{}, err
	}
	n := req.Lines
	if n < 0 {
		n = 0
	}
	if n > len(lines) {
		n = len(lines)
	}
	return h.ok(cap(strings.TrimRight(strings.Join(lines[:n], ""), "\n"), 20000), ""), nil
}

func (h *Handler) runTail(req *Request) (Result, error) {
	path, err := require(req.Path, "path", "tail")
	if err != nil {
		return Result{}, err
	}
	target, err := h.resolvePath(path, true, false)
	if err != nil {
		return Result{}, err
	}
	lines, err := readLines(target)
	if err != nil {
		return Result{}, err
	}
	var tail []string
	if req.Lines > 0 {
		start := len(lines) - req.Lines
		if start < 0 {
			start = 0
		}
		tail = lines[start:]
	}
	return h.ok(cap(strings.TrimRight(strings.Join(tail, ""), "\n"), 20000), ""), nil
}

func (h *Handler) runGrep(req *Request) (Result, error) {
	pattern, err := require(req.Pattern, "pattern", "grep")
	if err != nil {
		return Result{}, err
	}
	path := req.Path
	if path == "" {
		path = "."
	}
	target, err := h.resolvePath(path, true, false)
	if err != nil {
		return Result{}, err
	}
	regex, err := regexp.Compile(pattern)
	if err != nil {
		return Result{}, &ActionError{Detail: fmt.Sprintf("Invalid pattern: %s", err)}
	}

	var files []string
	if info, statErr := os.Stat(target); statErr == nil && !info.IsDir() {
		files = []string{target}
	} else {
		_ = filepath.Walk(target, func(p string, info os.FileInfo, err error) error {
			if err != nil || info.IsDir() {
				return nil
			}
			files = append(files, p)
			return nil
		})
	}

	var matches []string
	for _, filePath := range files {
		if len(matches) >= req.Limit {
			break
		}
		lines, err := readLines(filePath)
		if err != nil {
			continue
		}
		for lineno, line := range lines {
			if regex.MatchString(line) {
				rel, _ := filepath.Rel(h.RootDir, filePath)
				matches = append(matches, fmt.Sprintf("%s:%d: %s", rel, lineno+1, strings.TrimRight(line, "\n")))
				if len(matches) >= req.Limit {
					break
				}
			}
		}
	}
	return h.ok(strings.Join(matches, "\n"), ""), nil
}

func (h *Handler) runFind(req *Request) (Result, error) {
	path := req.Path
	if path == "" {
		path = "."
	}
	target, err := h.resolvePath(path, true, true)
	if err != nil {
		return Result{}, err
	}
	pattern := req.Pattern
	if pattern == "" {
		pattern = "*"
	}

	var matches []string
	_ = filepath.Walk(target, func(p string, info os.FileInfo, err error) error {
		if err != nil || p == target {
			return nil
		}
		if len(matches) >= req.Limit {
			return filepath.SkipAll
		}
		if ok, _ := filepath.Match(pattern, info.Name()); ok {
			rel, _ := filepath.Rel(h.RootDir, p)
			matches = append(matches, rel)
		}
		return nil
	})
	if len(matches) > req.Limit {
		matches = matches[:req.Limit]
	}
	return h.ok(strings.Join(matches, "\n"), ""), nil
}

// Dispatch mirrors ACTION_HANDLERS -- returns (nil, ActionError) for an
// unrecognized action so the HTTP layer can turn that into the same 400 the
// Python version returns.
func (h *Handler) Dispatch(req *Request) (Result, error) {
	switch req.Action {
	case "status", "branch", "log":
		return h.runGit(req)
	case "pwd":
		return h.runPwd(req)
	case "cd":
		return h.runCd(req)
	case "ls":
		return h.runLs(req)
	case "tree":
		return h.runTree(req)
	case "mkdir":
		return h.runMkdir(req)
	case "touch":
		return h.runTouch(req)
	case "cp":
		return h.runCp(req)
	case "mv":
		return h.runMv(req)
	case "rm":
		return h.runRm(req)
	case "rmdir":
		return h.runRmdir(req)
	case "cat":
		return h.runCat(req)
	case "less":
		return h.runLess(req)
	case "head":
		return h.runHead(req)
	case "tail":
		return h.runTail(req)
	case "grep":
		return h.runGrep(req)
	case "find":
		return h.runFind(req)
	case "read_file":
		return h.runReadFile(req)
	case "write_file":
		return h.runWriteFile(req)
	default:
		return Result{}, &ActionError{Detail: "Action not authorized."}
	}
}
