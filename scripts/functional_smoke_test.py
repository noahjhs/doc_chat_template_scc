#!/usr/bin/env python3
"""A deeper post-deploy check than smoke_test.sh's own curl-based liveness
checks (which only confirm a service is up and its auth gate is actually
enforcing -- see that script's own docstring). This one drives a real
signup -> pair -> policy-authoring -> eval -> mock tool call round trip
against a LIVE deployment via harness/client.py (plain functions, real
network, no CliRunner/no mocking of anything except the daemon dispatch
itself via call_tool's own mock=True) -- confirming the API isn't just
"up," but functionally correct end to end. This is exactly the kind of
check that was previously only ever done by hand (see this session's own
manual multi-account-pairing verification after a deploy) -- codifying it
here means every deploy gets it for free.

Creates one throwaway account per run -- no cleanup by design, matching
this project's existing posture of throwaway signup accounts for manual
verification against dev. Costs one real (non-mock) OpenAI call for the
tool-call round trip's follow-up hop -- deliberate, since the point is to
verify the real deployment, not a mocked one.

Usage: python scripts/functional_smoke_test.py [dev|prod]
Defaults to dev. Override the domain directly instead via AUTH_DOMAIN
(same env var smoke_test.sh itself honors, e.g. for a local
`uvicorn` instance on localhost). Exits nonzero (with a message on
stderr) on any failure.
"""

import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "harness"))
from harness import client  # noqa: E402

DEV_DOMAIN = "dev-auth.casperagent.dev"
PROD_DOMAIN = "auth.casperagent.dev"


def main() -> int:
    env = sys.argv[1] if len(sys.argv) > 1 else "dev"
    if env == "dev":
        domain = DEV_DOMAIN
    elif env == "prod":
        domain = PROD_DOMAIN
    else:
        print(f"Usage: {sys.argv[0]} [dev|prod]", file=sys.stderr)
        return 2
    domain = os.environ.get("AUTH_DOMAIN", domain)

    username = f"smoketest-{secrets.token_hex(4)}"
    password = secrets.token_urlsafe(16)
    print(f"Functional smoke test against {domain} (as {username})...")

    try:
        signup = client.signup(domain, username, password)
        token = signup["token"]
        if signup["username"] != username:
            raise AssertionError(f"signup returned unexpected username: {signup}")
        print("  OK   signup")

        routing_key = f"rk-smoketest-{secrets.token_hex(4)}"
        pair = client.pair_host(domain, token, routing_key, hostname="smoke-test-host")
        host_id = pair["host_id"]
        print("  OK   pair (fake host, no real daemon needed)")

        layer = client.create_policy_layer(domain, token, "smoketest-layer")
        client.create_policy_layer_rule(
            domain, token, layer["id"], positional_constraints=[{"whitelist": "^echo$"}], option_constraints=[], tier="allow"
        )
        client.add_policy_layer_to_host(domain, token, layer["id"], host_id)
        print("  OK   policy layer authoring + host attachment")

        evaluated = client.eval_policy(domain, token, [layer["id"]], positional_args=["echo", "hi"])
        if evaluated["tier"] != "allow":
            raise AssertionError(f"expected tier=allow from eval, got {evaluated}")
        print("  OK   policy eval matches the rule just authored")

        result = client.call_tool(
            domain, token, "run_shell_command", {"positional_args": ["echo", "hi"]}, mock=True, default_host="smoke-test-host"
        )
        if result["status"] != "done":
            raise AssertionError(f"expected a completed (non-mock OpenAI follow-up) turn, got {result}")
        print("  OK   mock tool call + real model follow-up round-trips end to end")
    except Exception as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 1

    print("All functional checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
