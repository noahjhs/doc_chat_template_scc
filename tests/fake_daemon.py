"""A minimal, scriptable stand-in for a real daemon's POST /api/command --
used where a test needs casper_service to actually reach a live daemon (real
run_shell_command dispatch, or /policies/eval's own routing) but must NOT
re-test real policy matching while doing it (see conversations.py's own
module docstring, and the Principal's own design directive: a test double
must supply an arbitrary/scripted verdict, never attempt to evaluate
anything itself -- real matching semantics are the Go daemon's own tested
concern, see agent/internal/commands/policy_test.go and
tests/test_policy_parity.py).

A real socket server (http.server, background thread), not an in-process
fake -- casper_service's own outbound calls to a daemon go through the real
`requests` library (see conversations.py's _dispatch_shell_command and
main.py's eval_policy), which needs an actual reachable URL regardless of
whether casper_service ITSELF is being driven through an in-process
fastapi.testclient.TestClient in the same test.

Kept as one shared module (not duplicated per test file, unlike this test
suite's usual small per-file helpers) since spinning up/tearing down a
background HTTP server is real, non-trivial infrastructure worth getting
right once."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeDaemon:
    """Serves scripted responses for POST /api/command (and a trivial 200
    for GET /api/health). Two ways to script a response:
      - `queue`: a list of (status_code, response_dict), popped in request
        order -- for a test that knows exactly how many calls to expect
        and what each should return.
      - `respond_with`: a callable (decoded request body dict) -> (status_code,
        response_dict) -- for a test that needs to inspect the request
        (e.g. confirming `approved: true` on a resend) to decide what to
        return.
    Exactly one of the two should be supplied. Every received request body
    is recorded in `.requests` (in order) for after-the-fact assertions."""

    def __init__(self, queue=None, respond_with=None):
        if (queue is None) == (respond_with is None):
            raise ValueError("supply exactly one of queue or respond_with")
        self._queue = list(queue) if queue is not None else None
        self._respond_with = respond_with
        self.requests: list[dict] = []
        self._lock = threading.Lock()

        daemon = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # silence -- a real daemon's own request log has no test-output value here

            def do_GET(self):
                if self.path == "/api/health":
                    self._write(200, {"status": "ok"})
                else:
                    self._write(404, {"detail": "not found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw or b"{}")
                with daemon._lock:
                    daemon.requests.append(body)
                    if daemon._queue is not None:
                        if not daemon._queue:
                            raise AssertionError("FakeDaemon queue exhausted -- test scripted too few daemon calls.")
                        status, response = daemon._queue.pop(0)
                    else:
                        status, response = daemon._respond_with(body)
                self._write(status, response)

            def _write(self, status, response):
                data = json.dumps(response).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"

    def close(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
