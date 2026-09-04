import logging
import os
import re
import subprocess
import sys
import threading
import time
import tomllib

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

# Run this on your own machine, e.g.:
#   uvicorn control_api:app --port 8000
# (or just run the packaged app directly). Enter the tunnel URL printed at
# startup, plus the API key below, into the web app's sidebar.
if getattr(sys, "frozen", False):
    # Packaged build: keep runtime files next to the actual executable, not
    # the temp dir PyInstaller unpacks into.
    _app_dir = os.path.dirname(sys.executable)
else:
    _app_dir = os.path.dirname(__file__)

# Every command the remote assistant asks for gets logged here, in addition
# to the console, so there's a record even for a packaged app run without a
# visible terminal.
COMMAND_LOG_FILE = os.environ.get(
    "CONTROL_API_LOG_FILE", os.path.join(_app_dir, "command_log.txt")
)
logger = logging.getLogger("control_api")
logger.setLevel(logging.INFO)
_log_format = logging.Formatter("%(asctime)s %(message)s")
for _handler in (logging.StreamHandler(sys.stdout), logging.FileHandler(COMMAND_LOG_FILE)):
    _handler.setFormatter(_log_format)
    logger.addHandler(_handler)


def load_api_key():
    """The same LOCAL_AGENT_API_KEY the web app reads from its own
    secrets.toml — baked into packaged builds at build time (see
    build/build_macos.sh / build_windows.ps1), or read live from
    .streamlit/secrets.toml when running from source."""
    env_override = os.environ.get("CONTROL_API_KEY")
    if env_override:
        return env_override

    if getattr(sys, "frozen", False):
        bundled = os.path.join(sys._MEIPASS, "baked_api_key.txt")
        if os.path.exists(bundled):
            with open(bundled) as f:
                key = f.read().strip()
            if key:
                return key
        raise RuntimeError(
            "No API key was baked into this build. Rebuild with "
            "LOCAL_AGENT_API_KEY set in .streamlit/secrets.toml."
        )

    secrets_path = os.path.join(_app_dir, ".streamlit", "secrets.toml")
    try:
        with open(secrets_path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise RuntimeError(
            f"No {secrets_path} found. Set LOCAL_AGENT_API_KEY there (or set "
            "the CONTROL_API_KEY environment variable)."
        ) from None

    key = data.get("LOCAL_AGENT_API_KEY")
    if not key:
        raise RuntimeError(f"LOCAL_AGENT_API_KEY is not set in {secrets_path}.")
    return key


API_KEY = load_api_key()


def load_app_url():
    """The deployed web app's URL, opened in a new browser tab on launch.
    Baked into packaged builds at build time from app_url.txt (see
    build/build_macos.sh / build_windows.ps1), or read live from that same
    file when running from source. Not fatal if missing — the server just
    won't auto-open a tab."""
    env_override = os.environ.get("CONTROL_API_APP_URL")
    if env_override:
        return env_override

    if getattr(sys, "frozen", False):
        bundled = os.path.join(sys._MEIPASS, "baked_app_url.txt")
        if os.path.exists(bundled):
            with open(bundled) as f:
                url = f.read().strip()
            if url:
                return url
        return None

    app_url_path = os.path.join(_app_dir, "app_url.txt")
    if os.path.exists(app_url_path):
        with open(app_url_path) as f:
            url = f.read().strip()
        if url:
            return url
    return None


app = FastAPI()


def require_api_key(x_api_key: str = Header(default=None)) -> None:
    if x_api_key != API_KEY:
        logger.warning("REJECTED request with invalid/missing API key")
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key.")


@app.get("/api/health")
def health(_=Depends(require_api_key)):
    return {"ok": True}


# 1. Define structured input using Pydantic
class GitCommandRequest(BaseModel):
    action: str  # e.g., "status", "log", "branch", "ls", "cd"
    limit: int = 5
    path: str | None = None  # only used by the "cd" action


# 2. Strict allowlist mapping to map inputs to safe local commands
ALLOWED_ACTIONS = {
    "status": ["git", "status", "--porcelain"],
    "branch": ["git", "branch", "-a"],
    "log": ["git", "log", "--oneline", "-n"],
    "ls": ["ls", "-la"],
}

# Tracks the directory git commands run in. "cd" moves it; it starts wherever
# this server was launched from. Guarded by a lock since FastAPI runs sync
# endpoints like these in a threadpool.
_state_lock = threading.Lock()
current_dir = os.getcwd()


def change_directory(path):
    global current_dir
    if not path:
        logger.warning("REJECTED cd with no path given")
        raise HTTPException(status_code=400, detail="path is required for the 'cd' action.")

    with _state_lock:
        base = current_dir
    expanded = os.path.expanduser(path)
    target = expanded if os.path.isabs(expanded) else os.path.normpath(os.path.join(base, expanded))

    if not os.path.isdir(target):
        logger.warning("REJECTED cd to %r (not a directory)", target)
        raise HTTPException(status_code=400, detail=f"Not a directory: {target}")

    with _state_lock:
        current_dir = target
    logger.info("CD -> %s", target)
    return {"success": True, "cwd": target}


@app.post("/api/git")
def execute_git_command(request: GitCommandRequest, _=Depends(require_api_key)):
    if request.action == "cd":
        return change_directory(request.path)

    if request.action not in ALLOWED_ACTIONS:
        logger.warning("REJECTED action=%r (not authorized)", request.action)
        raise HTTPException(status_code=400, detail="Action not authorized.")

    # Construct the command safely without shell=True
    base_cmd = ALLOWED_ACTIONS[request.action]
    if request.action == "log":
        base_cmd = base_cmd + [str(request.limit)]

    with _state_lock:
        cwd = current_dir

    logger.info("RUN %s (in %s)", " ".join(base_cmd), cwd)

    try:
        # 3. Execute locally and capture output
        result = subprocess.run(base_cmd, capture_output=True, text=True, check=True, cwd=cwd)
        logger.info("OK   %s (exit 0)", " ".join(base_cmd))
        # 4. Return clean, structured JSON back to the AI agent
        return {
            "success": True,
            "cwd": cwd,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
    except subprocess.CalledProcessError as e:
        logger.warning("FAIL %s (exit %s)", " ".join(base_cmd), e.returncode)
        return {
            "success": False,
            "cwd": cwd,
            "stdout": e.stdout.strip(),
            "stderr": e.stderr.strip(),
            "exit_code": e.returncode,
        }


# Real quick-tunnel subdomains are always several hyphen-joined words (e.g.
# "lat-actually-quotes-browsers") — require at least 3 segments so this
# doesn't match some other short/simple *.trycloudflare.com reference that
# shows up elsewhere in cloudflared's own log output.
CLOUDFLARED_URL_RE = re.compile(r"https://[a-z0-9]+(?:-[a-z0-9]+){2,}\.trycloudflare\.com")


def find_cloudflared():
    """Prefer a binary bundled alongside a PyInstaller-packaged build, else
    fall back to one already on the user's PATH."""
    if getattr(sys, "frozen", False):
        bundled = os.path.join(
            sys._MEIPASS, "cloudflared.exe" if sys.platform == "win32" else "cloudflared"
        )
        if os.path.exists(bundled):
            return bundled
    return "cloudflared"


def start_tunnel(port, url_timeout=30):
    """Launch a Cloudflare quick tunnel pointing at the local port; print and
    return its public URL once Cloudflare assigns one (blocks up to
    `url_timeout` seconds for that — worth the wait, since callers use the
    URL to link the browser tab they open back to this server). Returns
    (None, None) if cloudflared isn't available; (proc, None) if it started
    but no URL showed up in time."""
    cloudflared_path = find_cloudflared()
    try:
        proc = subprocess.Popen(
            [cloudflared_path, "tunnel", "--url", f"http://localhost:{port}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        print(
            "cloudflared not found — install it (e.g. `brew install cloudflared` "
            "on Mac, or download it from github.com/cloudflare/cloudflared) to "
            f"expose this server, or run it manually:\n"
            f"  cloudflared tunnel --url http://localhost:{port}",
            flush=True,
        )
        return None, None

    found_url = {}

    def watch_output():
        # flush=True matters here: when this process isn't attached to a
        # terminal (e.g. launched by a packaged app's GUI wrapper), stdout is
        # block-buffered by default and the URL could sit unflushed for a
        # long time otherwise. Keeps draining stdout for the process's whole
        # life (not just until the URL is found) so its pipe never fills up
        # and blocks cloudflared.
        for line in proc.stdout:
            if "url" not in found_url:
                match = CLOUDFLARED_URL_RE.search(line)
                if match:
                    found_url["url"] = match.group(0)
                    print(f"\n🌐 Public URL: {found_url['url']}", flush=True)

    threading.Thread(target=watch_output, daemon=True).start()

    deadline = time.time() + url_timeout
    while "url" not in found_url and proc.poll() is None and time.time() < deadline:
        time.sleep(0.1)

    return proc, found_url.get("url")


if __name__ == "__main__":
    # Lets `python3 control_api.py` work directly, not just `uvicorn control_api:app`.
    import webbrowser
    from urllib.parse import quote, urlsplit, urlunsplit

    import uvicorn

    PORT = int(os.environ.get("CONTROL_API_PORT", "8000"))
    tunnel_proc, tunnel_url = start_tunnel(PORT)

    app_url = load_app_url()
    if app_url:
        open_url = app_url
        if tunnel_url:
            # Pass our own URL along so the chat app can pre-fill its "Local
            # agent URL" field instead of the user copy-pasting it in.
            parts = urlsplit(app_url)
            query = f"{parts.query}&" if parts.query else ""
            query += f"local_agent_url={quote(tunnel_url, safe='')}"
            open_url = urlunsplit(parts._replace(query=query))
        try:
            webbrowser.open(open_url, new=2)
            print(f"🌍 Opened {open_url} in your browser.", flush=True)
        except Exception as e:  # noqa: BLE001 - opening a tab is a nicety, not essential
            print(f"Couldn't open a browser tab for {open_url}: {e}", flush=True)
    else:
        logger.info("No app URL configured — not opening a browser tab.")

    try:
        uvicorn.run(app, host="0.0.0.0", port=PORT)
    finally:
        if tunnel_proc:
            tunnel_proc.terminate()
