import fnmatch
import http.server
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit, urlunsplit

import requests
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Run this on your own machine, e.g. via the packaged app, or from source
# with `python3 casper_tool.py`. First run opens a browser tab to sign up
# or log in; after that it's silent. For a non-interactive run (dev/CI),
# set CONTROL_TOOL_KEY directly instead (also required if launching via
# bare `uvicorn casper_tool:app`, which skips the __main__ auth flow
# entirely).
if getattr(sys, "frozen", False):
    # Packaged build: keep runtime files next to the actual executable, not
    # the temp dir PyInstaller unpacks into.
    _app_dir = os.path.dirname(sys.executable)
else:
    _app_dir = os.path.dirname(__file__)

# Every filesystem-touching action is confined to this directory tree — the
# tool's own location. Put/run it inside the repo (or other directory) you
# want the assistant to work in; it can never read, write, or navigate
# outside that tree, no matter what path a request asks for.
ROOT_DIR = os.path.realpath(_app_dir)

# Every command the remote assistant asks for gets logged here, in addition
# to the console, so there's a record even for a packaged app run without a
# visible terminal.
COMMAND_LOG_FILE = os.environ.get(
    "CONTROL_TOOL_LOG_FILE", os.path.join(_app_dir, "command_log.txt")
)
logger = logging.getLogger("casper_tool")
logger.setLevel(logging.INFO)
_log_format = logging.Formatter("%(asctime)s %(message)s")
for _handler in (logging.StreamHandler(sys.stdout), logging.FileHandler(COMMAND_LOG_FILE)):
    _handler.setFormatter(_log_format)
    logger.addHandler(_handler)


def _base_url(domain):
    """Turn a bare host[:port] (no scheme, no path) into a scheme://host
    base. Localhost gets plain http; anything else (a real deployed domain)
    gets https."""
    domain = domain.strip().strip("/")
    scheme = "http" if domain.startswith("localhost") or domain.startswith("127.0.0.1") else "https"
    return f"{scheme}://{domain}"


def build_app_url(domain):
    return f"{_base_url(domain)}/chat"


def load_app_domain():
    """The deployed web app's domain (just the host[:port], no scheme or
    path — build_app_url() fills those in). Baked into packaged builds at
    build time from app_server.txt (see build/build_macos.sh /
    build_windows.ps1), or read live from that same file when running from
    source. Not fatal if missing — the server just won't auto-open a tab."""
    env_override = os.environ.get("CONTROL_TOOL_APP_DOMAIN")
    if env_override:
        return env_override

    if getattr(sys, "frozen", False):
        bundled = os.path.join(sys._MEIPASS, "baked_app_server.txt")
        if os.path.exists(bundled):
            with open(bundled) as f:
                domain = f.read().strip()
            if domain:
                return domain
        return None

    app_server_path = os.path.join(_app_dir, "app_server.txt")
    if os.path.exists(app_server_path):
        with open(app_server_path) as f:
            domain = f.read().strip()
        if domain:
            return domain
    return None


def load_auth_domain():
    """The auth service's domain (same baked/live/env pattern as
    load_app_domain()). Unlike the app domain, this one is fatal if
    unresolvable — there's no graceful fallback when the whole sign-in flow
    depends on it."""
    env_override = os.environ.get("CONTROL_TOOL_AUTH_DOMAIN")
    if env_override:
        return env_override

    if getattr(sys, "frozen", False):
        bundled = os.path.join(sys._MEIPASS, "baked_auth_server.txt")
        if os.path.exists(bundled):
            with open(bundled) as f:
                domain = f.read().strip()
            if domain:
                return domain
    else:
        auth_server_path = os.path.join(_app_dir, "auth_server.txt")
        if os.path.exists(auth_server_path):
            with open(auth_server_path) as f:
                domain = f.read().strip()
            if domain:
                return domain

    raise RuntimeError(
        "No auth service configured. Set auth_server.txt (source) or rebuild "
        "with it present (packaged), or set CONTROL_TOOL_AUTH_DOMAIN."
    )


# --- Sign-in: a session.json next to the executable holds the credential
# obtained from the auth service, so a run after the first is silent. ------
SESSION_FILE = os.path.join(_app_dir, "session.json")


def load_session():
    try:
        with open(SESSION_FILE) as f:
            data = json.load(f)
        if data.get("username") and data.get("token"):
            return data
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        pass
    return None


def clear_session():
    """Removes session.json -- called on sign-out (see /api/shutdown), since
    that's also the moment pages/chat.py revokes this same token against the
    auth service: keeping the file around after that would only make the
    *next* launch discover (via verify_session) that it's dead, print a
    confusing "no longer valid" message, and fall through to a fresh
    sign-in anyway -- exactly what not having a saved session at all
    already does, just slower and with an alarming-looking log line along
    the way.

    Best-effort: this used to only catch FileNotFoundError, and anything
    else (e.g. a permissions problem) would propagate straight out of the
    /api/shutdown handler -- since this ran *before* that handler started
    the farewell-dialog/exit background thread, a failure here silently
    skipped the dialog and the exit entirely, not just the cleanup, which
    is a real, confirmed way both could intermittently not happen."""
    try:
        os.remove(SESSION_FILE)
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001 -- best-effort only, but still log why
        logger.warning("clear_session: couldn't remove %s: %s", SESSION_FILE, e)


def save_session(username, token, browser_name=None):
    """`browser_name` is the macOS app name detected from the sign-in
    request's User-Agent (see detect_browser_name) -- remembered here so a
    later run's bring_tab_into_view() has a real answer without needing a
    fresh detection (the silent-reuse path never sees another HTTP request
    to sniff a User-Agent from). Preserves whatever was already saved if
    not given this time, rather than blanking it out."""
    if browser_name is None:
        existing = load_session()
        browser_name = existing.get("browser") if existing else None
    with open(SESSION_FILE, "w") as f:
        json.dump({"username": username, "token": token, "browser": browser_name}, f)
    try:
        os.chmod(SESSION_FILE, 0o600)
    except OSError:
        pass  # best-effort -- not fatal if the platform/filesystem doesn't support it


def verify_session(auth_domain, token):
    """Returns True/False for a completed check, or None if the auth
    service couldn't be reached -- callers need to tell "invalid" apart
    from "couldn't check right now"."""
    try:
        response = requests.post(
            f"{_base_url(auth_domain)}/verify",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        response.raise_for_status()
        return response.json().get("valid", False)
    except requests.RequestException:
        return None


def detect_browser_name(user_agent):
    """Best-effort mapping from a browser's own User-Agent string (sniffed
    from an actual HTTP request it made -- see open_pairing_page_and_wait)
    to its macOS application name, for bring_tab_into_view()'s AppleScript.
    Reliable for the major engines that identify themselves truthfully;
    many Chromium-based browsers (Brave, Vivaldi, Arc, ...) deliberately
    present as plain "Chrome" in their UA specifically to avoid this kind
    of sniffing, so those are indistinguishable from real Chrome here --
    returns None (letting the caller fall back to --browser/"Safari")
    rather than guess wrong with false confidence."""
    ua = user_agent or ""
    if "Edg/" in ua or "EdgA/" in ua or "EdgiOS/" in ua:
        return "Microsoft Edge"
    if "OPR/" in ua:
        return "Opera"
    if "Firefox/" in ua and "Seamonkey" not in ua:
        return "Firefox"
    if "Chrome/" in ua and "Chromium/" not in ua:
        return "Google Chrome"
    if "Chromium/" in ua:
        return "Chromium"
    if "Safari/" in ua and "Chrome/" not in ua:
        return "Safari"
    return None


# Maps a macOS application name (what --browser/bring_tab_into_view use)
# to the short name Python's webbrowser module recognizes via
# webbrowser.get() -- a much smaller set than the browsers we can *detect*
# or *activate*, since it only covers browsers webbrowser.py ships a
# controller for on macOS.
_WEBBROWSER_CONTROLLER_NAMES = {
    "safari": "safari",
    "google chrome": "chrome",
    "chrome": "chrome",
    "firefox": "firefox",
}


_BUNDLE_ID_TO_APP_NAME = {
    "com.apple.safari": "Safari",
    "com.google.chrome": "Google Chrome",
    "org.mozilla.firefox": "Firefox",
    "com.microsoft.edgemac": "Microsoft Edge",
}


def get_default_browser_app_name():
    """macOS only: the system's actual configured default browser's
    application name (e.g. "Safari", "Google Chrome"), or None if it can't
    be determined -- lets open_url_with_browser detect "Safari is about to
    be launched" even with no --browser override (the common case).
    Confirmed the LaunchServices plist's actual shape directly against a
    real machine (its "http" URL scheme handler's LSHandlerRoleAll is a
    bundle id, e.g. "com.google.chrome") rather than assumed from
    documentation."""
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            [
                "plutil", "-convert", "json", "-o", "-",
                os.path.expanduser("~/Library/Preferences/com.apple.LaunchServices/com.apple.launchservices.secure.plist"),
            ],
            capture_output=True, text=True, timeout=5, check=False,
        )
        data = json.loads(result.stdout)
        for handler in data.get("LSHandlers", []):
            if handler.get("LSHandlerURLScheme") == "http":
                return _BUNDLE_ID_TO_APP_NAME.get(handler.get("LSHandlerRoleAll", ""))
    except Exception:  # noqa: BLE001 -- best-effort only; None just skips the cold-launch special-case below
        pass
    return None


def _is_app_running(app_name):
    result = subprocess.run(
        ["osascript", "-e", f'application "{app_name}" is running'],
        capture_output=True, text=True, timeout=5, check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def _open_in_safaris_startup_tab(url, timeout=10):
    """Launches Safari and navigates its own freshly-created startup tab
    (Start Page, or a restored session) directly to `url`, instead of
    opening a separate new tab alongside it -- confirmed directly that a
    cold Safari launch otherwise leaves two tabs open: its own default
    startup tab, plus a second one for whatever URL was requested. Only
    used for a cold launch (see _is_app_running) -- once Safari's already
    running, a plain new tab is the expected behavior (same as every other
    browser), so this isn't involved at all. Returns True on success, False
    to fall back to the normal open path below (e.g. Safari isn't actually
    installed, or never got a window within `timeout`)."""
    launch = subprocess.run(
        ["osascript", "-e", 'tell application "Safari" to launch'],
        capture_output=True, text=True, timeout=5, check=False,
    )
    if launch.returncode != 0:
        return False

    deadline = time.time() + timeout
    has_window = False
    while time.time() < deadline:
        check = subprocess.run(
            ["osascript", "-e", 'tell application "Safari" to count of windows'],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if check.returncode == 0 and check.stdout.strip().isdigit() and int(check.stdout.strip()) >= 1:
            has_window = True
            break
        time.sleep(0.2)
    if not has_window:
        return False

    result = subprocess.run(
        ["osascript", "-e", f'tell application "Safari" to set URL of document 1 to "{url}"'],
        capture_output=True, text=True, timeout=5, check=False,
    )
    if result.returncode != 0:
        return False
    subprocess.run(["osascript", "-e", 'tell application "Safari" to activate'], capture_output=True, timeout=5, check=False)
    return True


def open_url_with_browser(url, browser_name, new=2):
    """Opens `url`, in `browser_name` (a macOS application name) if
    Python's webbrowser module has a controller for it, otherwise falling
    back to the plain system default (webbrowser.open()) -- e.g. for
    browser_name=None, or browsers webbrowser.py doesn't specifically know
    (Edge, Brave, Opera, ...). Used for testing against a browser other
    than your system default (--browser), separately from
    bring_tab_into_view's use of the same --browser value.

    Special-cased for a cold-launched Safari specifically (see
    _open_in_safaris_startup_tab) -- whether that's because `browser_name`
    says so explicitly, or (the common case) no --browser override was
    given and the system's actual default turns out to be Safari (see
    get_default_browser_app_name)."""
    resolved_name = browser_name or get_default_browser_app_name()
    if (
        sys.platform == "darwin"
        and resolved_name
        and resolved_name.strip().lower() == "safari"
        and not _is_app_running("Safari")
        and _open_in_safaris_startup_tab(url)
    ):
        return

    controller_name = _WEBBROWSER_CONTROLLER_NAMES.get((browser_name or "").strip().lower())
    if controller_name:
        try:
            webbrowser.get(controller_name).open(url, new=new)
            return
        except Exception:  # noqa: BLE001 -- fall back to the system default below
            pass
    webbrowser.open(url, new=new)


def find_ghost_icon():
    """Path to the bundled ghost icon (assets/ghost.png at repo root when
    running from source, or PyInstaller's bundled copy when packaged) for
    show_farewell_dialog()'s custom icon. None if it can't be found --
    showing that dialog without a custom icon is an acceptable
    degradation, not something worth failing over."""
    if getattr(sys, "frozen", False):
        bundled = os.path.join(sys._MEIPASS, "ghost.png")
        return bundled if os.path.exists(bundled) else None
    local = os.path.join(_app_dir, "assets", "ghost.png")
    return local if os.path.exists(local) else None


def show_farewell_dialog():
    """Best-effort, macOS-only: a native modal shown right before signing
    out actually shuts this process down -- a deliberate reminder that
    Casper (the desktop app) is where you come back to start a new
    session, not the browser. Blocks (no timeout -- it's fine to wait
    indefinitely for a real "OK" click) until dismissed. No-ops on
    non-macOS; any failure here is logged but never blocks the shutdown
    that follows it.

    Activates System Events first -- this process has no window/Dock
    presence of its own for "come to the front" to apply to (it's a
    background CLI server, not a GUI app), and without this the dialog can
    surface behind whatever the user was last looking at (e.g. the
    browser) instead of visibly grabbing attention. This is the standard
    trick for exactly that: activating a real, already-focusable
    application right before display dialog carries the focus over to it."""
    if sys.platform != "darwin":
        return
    icon_path = find_ghost_icon()
    icon_clause = f'with icon POSIX file "{icon_path}"' if icon_path else ""
    script = (
        'tell application "System Events" to activate\n'
        f'display dialog "See you next time!" with title "Casper" '
        f'buttons {{"OK"}} default button "OK" {icon_clause}'
    )
    try:
        result = subprocess.run(["osascript", "-e", script], capture_output=True, check=False, text=True)
        if result.returncode != 0:
            logger.warning("show_farewell_dialog: osascript exited %s: %s", result.returncode, result.stderr.strip())
    except Exception as e:  # noqa: BLE001 -- best-effort only, but still log why
        logger.warning("show_farewell_dialog: couldn't show it: %s", e)


def bring_tab_into_view(browser_name, title_substring):
    """Best-effort, macOS-only: raises `browser_name` and switches it to
    whichever of its tabs has `title_substring` in its title. Used when an
    already-open tab self-navigates via JS (see click_anchor_js) -- that
    changes the tab's content, but nothing about a page navigating itself
    brings its own browser window forward, so without this the user has no
    visual indication anything happened. Silently does nothing on
    non-macOS, or if the named browser isn't running, isn't scriptable
    (Firefox has essentially no AppleScript tab support at all), or
    Automation permission hasn't been granted -- this is a nicety, never
    something sign-in depends on.

    Safari and Chromium-based browsers (Chrome, Edge, Opera, ...) use
    genuinely different AppleScript terminology for the same concepts --
    confirmed directly (not assumed) against a real running Safari:
    a tab's page-title property is `name`, not `title` (`title of tab ...`
    is flatly not a term Safari's dictionary recognizes), and a window's
    displayed tab is set via `current tab of window`, not the
    `active tab index` property Chromium's dictionary uses. Using the
    wrong one doesn't fail quietly -- it's a syntax/runtime error, so both
    are handled explicitly rather than guessing one and hoping."""
    if sys.platform != "darwin":
        return

    if browser_name.strip().lower() == "safari":
        script = f"""
        tell application "{browser_name}"
            set winCount to count of windows
            repeat with i from 1 to winCount
                set w to window i
                set tabCount to count of tabs of w
                repeat with j from 1 to tabCount
                    if name of tab j of w contains "{title_substring}" then
                        set current tab of w to tab j of w
                        set index of w to 1
                        activate
                    end if
                end repeat
            end repeat
        end tell
        """
    else:
        script = f"""
        tell application "{browser_name}"
            repeat with w in windows
                repeat with t in tabs of w
                    if title of t contains "{title_substring}" then
                        set active tab index of w to (index of t)
                        set index of w to 1
                        activate
                    end if
                end repeat
            end repeat
        end tell
        """

    try:
        result = subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5, check=False, text=True)
        if result.returncode != 0:
            # Logged (not just swallowed) since this is otherwise invisible --
            # e.g. macOS denying Apple Events from this unsigned,
            # non-.app-bundle binary would show up here rather than as a
            # permission prompt the way a proper .app would get.
            logger.warning(
                "bring_tab_into_view: osascript exited %s targeting %r: %s",
                result.returncode, browser_name, result.stderr.strip(),
            )
    except Exception as e:  # noqa: BLE001 -- best-effort only, but still log why
        logger.warning("bring_tab_into_view: couldn't run osascript: %s", e)


# A complete, ordinary standalone page -- sent as soon as the sign-in
# callback lands (see open_pairing_page_and_wait), before the Cloudflare
# tunnel has actually come up, so the tab shows something rather than a
# bare pending request for however long that takes. Two earlier approaches
# tried to keep that *same* HTTP response open and stream more into it once
# the tunnel was ready (byte-padding past a hoped-for render threshold,
# then explicit chunked transfer encoding) -- neither got Safari to
# actually paint anything before the connection closed. This sidesteps the
# question entirely: this response is done the moment it's sent (ordinary
# framing, nothing kept open), and this page's own JS polls /status on
# this same local listener for when the tunnel's ready, redirecting itself
# once it is -- the same already-working pattern casper_app.py's landing
# page and pages/chat.py's reconnect poll already use elsewhere in this
# codebase, just against this run's pairing listener instead.
PAIRING_SPINNER_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Casper</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; display: flex;
         flex-direction: column; align-items: center; justify-content: center;
         height: 100vh; margin: 0; gap: 1rem; color: #333; }
  .spinner { width: 32px; height: 32px; border: 4px solid #ddd;
             border-top-color: #555; border-radius: 50%;
             animation: spin 0.8s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
</style></head>
<body>
<div class="spinner"></div>
<p>Signed in — connecting to Casper…</p>
<script>
function poll() {
    fetch("/status")
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.ready && data.chat_url) {
                window.location.href = data.chat_url;
            } else {
                setTimeout(poll, 300);
            }
        })
        .catch(function() { setTimeout(poll, 300); });
}
poll();
</script>
</body></html>
"""


def open_pairing_page_and_wait(app_domain, on_authenticated, port, browser_name, timeout=300):
    """First-run sign-in: opens the deployed app's /signin page in a real
    browser tab (so credential entry is a normal web form, not a terminal
    prompt) and waits on a short-lived local HTTP listener for it to hand
    back a token -- the same "loopback" pattern CLI tools like gcloud or AWS
    SSO use for browser-based sign-in.

    Binds to `port` (the same port the main API server uses once pairing
    completes -- they never run at the same time, so there's no conflict)
    rather than a random one, so a landing/chat page left open from a
    previous run can discover this run's /signin URL via /api/pairing-info
    and navigate itself there, instead of a new tab being opened for it.

    `on_authenticated(username, token)` runs in a background thread once
    that callback arrives -- callers do the tunnel-starting work in there
    and return the chat app's URL, which can take several real seconds.
    Rather than blocking the HTTP response on that (see PAIRING_SPINNER_HTML's
    comment for why), the response completes immediately with a page that
    polls this same listener's /status for when that background thread
    finishes, redirecting itself once it does -- so the same tab that
    showed /signin ends up on /chat, no separate tab ever opened for it.
    This function itself still blocks until that background thread
    completes (or `timeout` elapses) before returning, since callers need
    its result (e.g. the tunnel process) available by the time it does --
    it just keeps answering the page's /status polls meanwhile instead of
    sitting on the one request. Exits the process if nothing arrives within
    `timeout` seconds."""
    nonce = secrets.token_urlsafe(16)
    done = {}
    auth_result = {}
    auth_done_event = threading.Event()
    pair_url = (
        f"{_base_url(app_domain)}/signin?callback_port={port}&nonce={quote(nonce, safe='')}"
    )

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlsplit(self.path)
            if parsed.path == "/api/pairing-info":
                # Unauthenticated (no token exists yet to check) and
                # CORS-open, since an already-open page on a different
                # origin (the deployed web app) is what polls this -- it
                # only ever reveals a URL to go sign in at, never anything
                # sensitive. Marking this "polled" is what lets the grace
                # period below skip opening a second tab -- something is
                # already about to navigate itself there.
                done["polled"] = True
                detected = detect_browser_name(self.headers.get("User-Agent", ""))
                if detected:
                    done["browser"] = detected
                body = json.dumps({"signin_url": pair_url}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
                return

            if parsed.path == "/status":
                # Polled by PAIRING_SPINNER_HTML's own JS -- same-origin
                # (that page came from this same listener), no CORS needed.
                body = json.dumps(
                    {"ready": auth_done_event.is_set(), "chat_url": auth_result.get("chat_url")}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
                return

            qs = parse_qs(parsed.query)
            if qs.get("nonce", [None])[0] == nonce and qs.get("token"):
                username = qs.get("username", [""])[0]
                token = qs["token"][0]
                detected = detect_browser_name(self.headers.get("User-Agent", ""))
                if detected:
                    done["browser"] = detected
                done["token_received"] = True

                def _run_on_authenticated():
                    auth_result["chat_url"] = on_authenticated(username, token, done.get("browser"))
                    auth_done_event.set()

                threading.Thread(target=_run_on_authenticated, daemon=True).start()

                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(PAIRING_SPINNER_HTML.encode())
            else:
                self.send_response(400)
                self.end_headers()

        def log_message(self, *_args):
            pass  # keep the console output clean -- we print our own status lines

    try:
        server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        print(f"Couldn't start the sign-in listener on port {port}: {e}", flush=True)
        sys.exit(1)

    print(f"Sign in to continue: {pair_url}", flush=True)

    # Give an already-open landing/chat page (polling /api/pairing-info --
    # see casper_app.py and pages/chat.py's signed-out state) a brief window
    # to discover this pairing session and navigate itself there first.
    # Only open a new tab if nothing claims it in time (e.g. no such page is
    # currently open) -- otherwise we'd end up with two tabs both landing
    # on /signin. This is dead time on every run where no such page happens
    # to be open (the common case -- most launches), so it's kept as short
    # as casper_app.py's own poll interval reasonably allows: that page
    # polls immediately on load and every 750ms after, so 1.5s comfortably
    # covers one full cycle of that (with real margin for network/page-load
    # jitter) without unnecessarily stretching out every other launch.
    grace_deadline = time.time() + 1.5
    while "polled" not in done and "token_received" not in done and time.time() < grace_deadline:
        server.timeout = max(0.1, grace_deadline - time.time())
        server.handle_request()

    if "token_received" not in done and "polled" not in done:
        try:
            open_url_with_browser(pair_url, browser_name)
        except Exception:  # noqa: BLE001 -- fall through to the printed URL either way
            pass
    elif "polled" in done:
        # An already-open tab is about to navigate itself to /signin (no
        # new tab opened for it) -- but that self-navigation doesn't bring
        # its own browser window forward, so do that explicitly. Only ever
        # uses the browser actually detected from that tab's own request
        # (see detect_browser_name) -- deliberately *not* --browser/default:
        # that flag is about which browser *opens a new tab*, a separate
        # concern, and guessing wrong here wouldn't just silently do
        # nothing -- `tell application "X"` launches X if it isn't already
        # running, so a wrong guess could pop open a browser that has
        # nothing to do with this session.
        detected = done.get("browser")
        if detected:
            bring_tab_into_view(detected, "Casper")
        else:
            logger.info("Couldn't tell which browser that tab is in (unrecognized User-Agent) -- not bringing it forward.")

    # Keeps answering requests (the initial token callback, then the
    # spinner page's own /status polls) until on_authenticated's background
    # thread actually finishes -- not just until the token arrives, since
    # callers need its result (e.g. the tunnel process) available by the
    # time this function returns.
    deadline = time.time() + timeout
    while not auth_done_event.is_set() and time.time() < deadline:
        server.timeout = max(0.1, min(1.0, deadline - time.time()))
        server.handle_request()
    server.server_close()

    if not auth_done_event.is_set():
        print("Timed out waiting for sign-in. Run the tool again to retry.", flush=True)
        sys.exit(1)


def ensure_authenticated(app_domain, auth_domain, port, browser_name):
    """Returns (api_key, tunnel_proc), prompting a fresh sign-in only when
    needed: a cached session is reused silently if it still verifies (and
    the tunnel/chat tab are started right away, in a new tab since there's
    no existing one to reuse); a network hiccup while checking it doesn't
    block startup (trust the cached token rather than force a re-login just
    because the auth service was slow to answer); an explicitly
    invalid/absent session falls through to a fresh browser-based sign-in,
    in which case the tunnel only starts once that completes, and the same
    tab that showed /signin is redirected straight to the chat app rather
    than a new tab being opened for it -- see open_pairing_page_and_wait."""
    session = load_session()
    if session:
        valid = verify_session(auth_domain, session["token"])
        if valid is not False:  # True, or None (couldn't check -- trust it)
            if valid is None:
                logger.info("Couldn't verify saved session (offline?) -- using it anyway.")
            tunnel_proc, _ = start_and_open_chat(
                app_domain, session["token"], port, open_new_tab=True, browser_name=browser_name
            )
            return session["token"], tunnel_proc
        # Cleared right away, not just left to /api/shutdown's cleanup --
        # that runs at sign-out time, but this is the moment a *stale*
        # session (e.g. from a run that ended some other way -- killed,
        # crashed, or a shutdown whose own cleanup didn't get a chance to
        # run) actually gets confirmed dead. Without this, a session stuck
        # in that state prints this same message on every single launch
        # rather than at most once.
        clear_session()
        logger.info("Saved session is no longer valid -- signing in again.")

    result = {}

    def on_authenticated(username, token, detected_browser):
        save_session(username, token, detected_browser)
        logger.info("Signed in as %s", username)
        tunnel_proc, chat_url = start_and_open_chat(app_domain, token, port, open_new_tab=False)
        result["token"] = token
        result["tunnel_proc"] = tunnel_proc
        return chat_url

    open_pairing_page_and_wait(app_domain, on_authenticated, port, browser_name)
    return result["token"], result["tunnel_proc"]


app = FastAPI()

# /api/session-info is fetched directly from the browser (not server-side
# like the other endpoints), so it needs real CORS headers or the browser
# blocks it before the request ever lands here. Permissive by design: the
# X-API-Key check on every endpoint is the actual security boundary, not
# CORS -- this just lets a page prove it holds a valid key at all.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["X-API-Key"],
)

# Set in __main__ before the server starts serving -- None only very briefly
# at import time, and always non-None by the time any request is handled.
API_KEY = None

# The running uvicorn.Server instance, set in __main__. Used by /api/shutdown
# to request a graceful stop -- NOT via os.kill(SIGTERM): a signal raised
# from a background thread doesn't reliably unwind back through the
# try/finally around server.run() (confirmed empirically -- the tunnel
# subprocess was left orphaned), whereas setting should_exit lets uvicorn's
# own event loop notice and shut down in-process, which does.
_uvicorn_server = None

# The current Cloudflare tunnel's public URL, set in start_and_open_chat()
# once the tunnel comes up. Lets an already-open chat tab from a previous
# run (see /api/session-info below) discover it without a new tab ever
# being opened for it.
CURRENT_TUNNEL_URL = None


def require_api_key(x_api_key: str = Header(default=None)) -> None:
    if API_KEY is None or x_api_key != API_KEY:
        logger.warning("REJECTED request with invalid/missing API key")
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key.")


@app.get("/api/health")
def health(_=Depends(require_api_key)):
    return {"ok": True}


@app.get("/api/session-info")
def session_info(_=Depends(require_api_key)):
    """Lets a chat tab still open from a previous run discover this run's
    (new) tunnel URL -- see pages/chat.py's background reconnect poll. Only
    reachable with the same API key that tab already has, so this doesn't
    leak anything to a session that isn't already this one."""
    return {"tunnel_url": CURRENT_TUNNEL_URL}


# Deliberately NOT in ACTION_HANDLERS/COMMAND_CATEGORIES below -- this must
# only be reachable from the web app's dedicated sign-out button, never
# something the assistant can invoke on its own.
def _farewell_then_exit():
    show_farewell_dialog()
    if _uvicorn_server is not None:
        # uvicorn finishes the /api/shutdown request itself (so its
        # response already reached the caller, well before this) before it
        # actually stops serving.
        _uvicorn_server.should_exit = True


@app.post("/api/shutdown")
def shutdown(_=Depends(require_api_key)):
    logger.info("Shutdown requested -- showing the farewell dialog, then exiting.")
    # Cleared right away (not from the background thread below) -- this
    # token is also being revoked server-side right now (pages/chat.py's
    # sign-out flow calls the auth service separately), so there's nothing
    # left this file could still be useful for once this request is
    # handled, and leaving it in place only makes the *next* launch
    # discover it's dead the slow way (see clear_session's docstring).
    clear_session()
    # On its own thread so the dialog (which can wait indefinitely for a
    # click) doesn't delay this response -- the caller isn't waiting around
    # for that, it just wants to know the shutdown was received.
    threading.Thread(target=_farewell_then_exit, daemon=True).start()
    return {"success": True, "message": "Shutting down."}


# Every action the remote assistant can invoke, grouped the way the web
# app's sidebar and tool schema present them. Duplicated there rather than
# imported (the two run as separate processes on separate machines) — keep
# the two lists in sync by hand.
COMMAND_CATEGORIES = {
    "Git": ["status", "branch", "log"],
    "Navigation": ["pwd", "cd", "ls", "tree"],
    "Management": ["mkdir", "touch", "cp", "mv", "rm", "rmdir"],
    "Viewing & Searching": ["cat", "less", "head", "tail", "grep", "find"],
}


class CommandRequest(BaseModel):
    action: str
    path: str | None = None
    destination: str | None = None  # only used by "cp" and "mv"
    pattern: str | None = None  # only used by "grep" and "find"
    lines: int = 10  # only used by "head" and "tail"
    limit: int = 5  # "log" entry count; max matches for "grep" and "find"


# Tracks the directory these actions run in. "cd" moves it; it starts at
# ROOT_DIR regardless of the process's actual launch directory, so it's
# always inside the confined tree. Guarded by a lock since FastAPI runs sync
# endpoints like these in a threadpool.
_state_lock = threading.Lock()
current_dir = ROOT_DIR


def resolve_path(path, must_exist=False, must_be_dir=False):
    """Resolve `path` (absolute, or relative to the tracked current_dir) and
    confirm the result stays inside ROOT_DIR. Resolves symlinks (via
    realpath) before checking, so a symlink pointing outside ROOT_DIR can't
    be used to escape it. Every filesystem-touching action funnels through
    this one gate."""
    with _state_lock:
        base = current_dir
    expanded = os.path.expanduser(path or ".")
    joined = expanded if os.path.isabs(expanded) else os.path.join(base, expanded)
    resolved = os.path.realpath(joined)

    if resolved != ROOT_DIR and not resolved.startswith(ROOT_DIR + os.sep):
        logger.warning("REJECTED path %r (resolves to %r, outside %r)", path, resolved, ROOT_DIR)
        raise HTTPException(status_code=400, detail=f"Path is outside the allowed directory: {ROOT_DIR}")
    if must_exist and not os.path.exists(resolved):
        raise HTTPException(status_code=400, detail=f"No such file or directory: {resolved}")
    if must_be_dir and not os.path.isdir(resolved):
        raise HTTPException(status_code=400, detail=f"Not a directory: {resolved}")
    return resolved


def _require(value, field_name, action):
    if not value:
        raise HTTPException(status_code=400, detail=f"'{field_name}' is required for the '{action}' action.")
    return value


def _cap(text, max_chars=20000):
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n... [truncated, {len(text) - max_chars} more characters]"


def _ok(stdout="", stderr=""):
    with _state_lock:
        cwd = current_dir
    return {"success": True, "cwd": cwd, "stdout": stdout, "stderr": stderr}


def _fail(stderr):
    with _state_lock:
        cwd = current_dir
    return {"success": False, "cwd": cwd, "stdout": "", "stderr": stderr}


# --- Git: unchanged from before, still shells out to the git binary -------
GIT_COMMANDS = {
    "status": ["git", "status", "--porcelain"],
    "branch": ["git", "branch", "-a"],
    "log": ["git", "log", "--oneline", "-n"],
}


def run_git(req):
    base_cmd = GIT_COMMANDS[req.action]
    if req.action == "log":
        base_cmd = base_cmd + [str(req.limit)]
    with _state_lock:
        cwd = current_dir
    logger.info("RUN %s (in %s)", " ".join(base_cmd), cwd)
    try:
        result = subprocess.run(base_cmd, capture_output=True, text=True, check=True, cwd=cwd)
        return {"success": True, "cwd": cwd, "stdout": result.stdout.strip(), "stderr": result.stderr.strip()}
    except subprocess.CalledProcessError as e:
        return {
            "success": False,
            "cwd": cwd,
            "stdout": e.stdout.strip(),
            "stderr": e.stderr.strip(),
            "exit_code": e.returncode,
        }


# --- Navigation -------------------------------------------------------
def run_pwd(req):
    with _state_lock:
        cwd = current_dir
    return _ok(stdout=cwd)


def run_cd(req):
    global current_dir
    target = resolve_path(_require(req.path, "path", "cd"), must_exist=True, must_be_dir=True)
    with _state_lock:
        current_dir = target
    logger.info("CD -> %s", target)
    return {"success": True, "cwd": target, "stdout": target, "stderr": ""}


def run_ls(req):
    target = resolve_path(req.path or ".", must_exist=True, must_be_dir=True)
    lines = []
    with os.scandir(target) as it:
        for entry in sorted(it, key=lambda e: e.name):
            kind = "d" if entry.is_dir(follow_symlinks=False) else "f"
            size = entry.stat(follow_symlinks=False).st_size
            lines.append(f"{kind} {size:>10} {entry.name}")
    return _ok(stdout="\n".join(lines))


def run_tree(req, max_depth=4):
    target = resolve_path(req.path or ".", must_exist=True, must_be_dir=True)
    lines = []

    def walk(dir_path, prefix, depth):
        if depth > max_depth:
            return
        try:
            entries = sorted(os.scandir(dir_path), key=lambda e: e.name)
        except OSError as e:
            lines.append(f"{prefix}[error: {e}]")
            return
        for entry in entries:
            is_dir = entry.is_dir(follow_symlinks=False)
            lines.append(f"{prefix}{entry.name}{'/' if is_dir else ''}")
            if is_dir:
                walk(entry.path, prefix + "  ", depth + 1)

    walk(target, "", 1)
    return _ok(stdout=_cap("\n".join(lines)))


# --- Management: intentionally no recursive delete -- "rm" only removes a
# file, "rmdir" only removes an already-empty directory, same as the real
# shell builtins. Combined with ROOT_DIR confinement, that caps the worst
# case to "delete one file/empty dir inside the allowed tree", never a
# recursive wipe. ---------------------------------------------------------
def run_mkdir(req):
    target = resolve_path(_require(req.path, "path", "mkdir"))
    try:
        os.makedirs(target, exist_ok=False)
    except FileExistsError:
        return _fail(f"Already exists: {target}")
    return _ok(stdout=f"Created {target}")


def run_touch(req):
    target = resolve_path(_require(req.path, "path", "touch"))
    if os.path.isdir(target):
        return _fail(f"Is a directory: {target}")
    Path(target).touch(exist_ok=True)
    return _ok(stdout=f"Touched {target}")


def run_cp(req):
    src = resolve_path(_require(req.path, "path", "cp"), must_exist=True)
    dst = resolve_path(_require(req.destination, "destination", "cp"))
    if os.path.isdir(src):
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)
    return _ok(stdout=f"Copied {src} -> {dst}")


def run_mv(req):
    src = resolve_path(_require(req.path, "path", "mv"), must_exist=True)
    dst = resolve_path(_require(req.destination, "destination", "mv"))
    shutil.move(src, dst)
    return _ok(stdout=f"Moved {src} -> {dst}")


def run_rm(req):
    target = resolve_path(_require(req.path, "path", "rm"), must_exist=True)
    if os.path.isdir(target):
        return _fail(f"Is a directory (use 'rmdir' for an empty directory): {target}")
    os.remove(target)
    return _ok(stdout=f"Removed {target}")


def run_rmdir(req):
    target = resolve_path(_require(req.path, "path", "rmdir"), must_exist=True, must_be_dir=True)
    try:
        os.rmdir(target)
    except OSError as e:
        return _fail(str(e))
    return _ok(stdout=f"Removed {target}")


# --- Viewing & Searching ---------------------------------------------------
def _read_text(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return _cap(f.read())


def run_cat(req):
    target = resolve_path(_require(req.path, "path", "cat"), must_exist=True)
    if os.path.isdir(target):
        return _fail(f"Is a directory: {target}")
    return _ok(stdout=_read_text(target))


def run_less(req):
    # No interactive paging over HTTP -- same as 'cat', just capped output.
    return run_cat(req)


def run_head(req):
    target = resolve_path(_require(req.path, "path", "head"), must_exist=True)
    with open(target, "r", encoding="utf-8", errors="replace") as f:
        lines = [next(f, "") for _ in range(max(req.lines, 0))]
    return _ok(stdout=_cap("".join(lines).rstrip("\n")))


def run_tail(req):
    target = resolve_path(_require(req.path, "path", "tail"), must_exist=True)
    with open(target, "r", encoding="utf-8", errors="replace") as f:
        all_lines = f.readlines()
    tail_lines = all_lines[-req.lines:] if req.lines > 0 else []
    return _ok(stdout=_cap("".join(tail_lines).rstrip("\n")))


def run_grep(req):
    pattern = _require(req.pattern, "pattern", "grep")
    target = resolve_path(req.path or ".", must_exist=True)
    try:
        regex = re.compile(pattern)
    except re.error as e:
        raise HTTPException(status_code=400, detail=f"Invalid pattern: {e}") from None

    if os.path.isfile(target):
        files = [target]
    else:
        files = [
            os.path.join(dirpath, name)
            for dirpath, _dirnames, filenames in os.walk(target)
            for name in filenames
        ]

    matches = []
    for file_path in files:
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                for lineno, line in enumerate(f, start=1):
                    if regex.search(line):
                        rel = os.path.relpath(file_path, ROOT_DIR)
                        matches.append(f"{rel}:{lineno}: {line.rstrip()}")
                        if len(matches) >= req.limit:
                            break
        except OSError:
            continue
        if len(matches) >= req.limit:
            break

    return _ok(stdout="\n".join(matches))


def run_find(req):
    target = resolve_path(req.path or ".", must_exist=True, must_be_dir=True)
    pattern = req.pattern or "*"
    matches = []
    for dirpath, dirnames, filenames in os.walk(target):
        for name in sorted(dirnames) + sorted(filenames):
            if fnmatch.fnmatch(name, pattern):
                rel = os.path.relpath(os.path.join(dirpath, name), ROOT_DIR)
                matches.append(rel)
                if len(matches) >= req.limit:
                    break
        if len(matches) >= req.limit:
            break
    return _ok(stdout="\n".join(matches))


ACTION_HANDLERS = {
    "status": run_git,
    "branch": run_git,
    "log": run_git,
    "pwd": run_pwd,
    "cd": run_cd,
    "ls": run_ls,
    "tree": run_tree,
    "mkdir": run_mkdir,
    "touch": run_touch,
    "cp": run_cp,
    "mv": run_mv,
    "rm": run_rm,
    "rmdir": run_rmdir,
    "cat": run_cat,
    "less": run_less,
    "head": run_head,
    "tail": run_tail,
    "grep": run_grep,
    "find": run_find,
}


@app.post("/api/command")
def execute_command(request: CommandRequest, _=Depends(require_api_key)):
    handler = ACTION_HANDLERS.get(request.action)
    if handler is None:
        logger.warning("REJECTED action=%r (not authorized)", request.action)
        raise HTTPException(status_code=400, detail="Action not authorized.")

    logger.info(
        "RUN action=%s path=%r destination=%r pattern=%r",
        request.action, request.path, request.destination, request.pattern,
    )
    try:
        result = handler(request)
        logger.info("%s action=%s", "OK  " if result.get("success") else "FAIL", request.action)
        return result
    except HTTPException:
        raise
    except OSError as e:
        logger.warning("FAIL action=%s: %s", request.action, e)
        return _fail(str(e))


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


def start_and_open_chat(app_domain, api_key, port, open_new_tab, browser_name=None):
    """Starts the Cloudflare tunnel and builds the chat app's URL, passing
    the tunnel URL, API key, this server's own local port, and the confined
    workspace directory along as query params so the chat page connects
    automatically and can greet the user with it -- the port is what lets a
    chat tab still open from a previous run find this run's
    /api/session-info and reconnect itself instead of a new tab being
    opened for it (see pages/chat.py). If `open_new_tab`, also opens it in
    a new browser tab (in `browser_name` if given -- see
    open_url_with_browser -- otherwise the system default) -- pass
    open_new_tab=False when the caller will instead redirect an
    already-open tab there itself (e.g. the /signin tab, once pairing
    completes). Returns (tunnel_proc, chat_url)."""
    global CURRENT_TUNNEL_URL
    tunnel_proc, tunnel_url = start_tunnel(port)
    CURRENT_TUNNEL_URL = tunnel_url

    app_url = build_app_url(app_domain)
    open_url = app_url
    if tunnel_url:
        parts = urlsplit(app_url)
        query = f"{parts.query}&" if parts.query else ""
        query += f"local_agent_url={quote(tunnel_url, safe='')}"
        query += f"&local_agent_token={quote(api_key, safe='')}"
        query += f"&local_agent_port={port}"
        query += f"&local_agent_workspace={quote(ROOT_DIR, safe='')}"
        open_url = urlunsplit(parts._replace(query=query))

    if open_new_tab:
        try:
            open_url_with_browser(open_url, browser_name)
            print(f"🌍 Opened {open_url} in your browser.", flush=True)
        except Exception as e:  # noqa: BLE001 - opening a tab is a nicety, not essential
            print(f"Couldn't open a browser tab for {open_url}: {e}", flush=True)

    return tunnel_proc, open_url


if __name__ == "__main__":
    # Lets `python3 casper_tool.py` work directly, not just `uvicorn casper_tool:app`.
    import argparse

    import uvicorn

    arg_parser = argparse.ArgumentParser(description="Casper — your friendly ghost")
    arg_parser.add_argument(
        "--agent-server",
        nargs="?",
        const="localhost:8501",
        type=str,
        default=None,
        metavar="HOST[:PORT]",
        help=(
            "Open the chat app at HOST[:PORT] (default localhost:8501, "
            "Streamlit's default) instead of the baked-in/app_server.txt domain "
            "— for testing against a Streamlit instance running elsewhere."
        ),
    )
    arg_parser.add_argument(
        "--browser",
        type=str,
        default=None,
        metavar="NAME",
        help=(
            "macOS application name of the browser to use, e.g. 'Safari', "
            "'Google Chrome', 'Firefox' -- for testing against a browser "
            "other than your system default (the default when this isn't "
            "given at all). Only "
            "controls which browser opens when a brand new tab is needed "
            "(no effect if Python's webbrowser module has no specific "
            "support for it, e.g. Edge/Brave/Opera -- falls back to the "
            "system default for those). Unrelated to, and never used for, "
            "bringing an *already-open* tab to the front when it's reused "
            "for sign-in instead of a new one being opened -- that's always "
            "based on whatever browser is actually detected from that tab's "
            "own request, never a guess. No effect on non-macOS; sign-in "
            "itself works regardless of any of this."
        ),
    )
    cli_args = arg_parser.parse_args()

    PORT = int(os.environ.get("CONTROL_TOOL_PORT", "8000"))

    app_domain = cli_args.agent_server or load_app_domain()
    if not app_domain:
        print(
            "No web app domain configured (app_server.txt/--agent-server) — "
            "can't sign in or open the chat app. Exiting.",
            flush=True,
        )
        sys.exit(1)

    # Authenticate before the tunnel starts: pairing only ever talks to
    # localhost (the browser reaches this machine directly, not through the
    # tunnel), so there's nothing tunnel-dependent about it.
    auth_domain = load_auth_domain()
    env_key = os.environ.get("CONTROL_TOOL_KEY")
    if env_key:
        API_KEY = env_key
        tunnel_proc, _ = start_and_open_chat(
            app_domain, API_KEY, PORT, open_new_tab=True, browser_name=cli_args.browser
        )
    else:
        API_KEY, tunnel_proc = ensure_authenticated(app_domain, auth_domain, PORT, cli_args.browser)

    # Built explicitly (rather than the uvicorn.run() convenience wrapper)
    # so /api/shutdown can request a graceful stop via should_exit -- see
    # the comment on _uvicorn_server above.
    config = uvicorn.Config(app, host="0.0.0.0", port=PORT)
    _uvicorn_server = uvicorn.Server(config)
    try:
        _uvicorn_server.run()
    finally:
        if tunnel_proc:
            tunnel_proc.terminate()
