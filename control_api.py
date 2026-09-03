import os
import subprocess

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

# Run this on your own machine, e.g.:
#   openssl rand -hex 32 > api_key.txt
#   uvicorn control_api:app --port 8000
# then make it reachable from the deployed app (e.g. via an ngrok/cloudflared
# tunnel) and give the app that URL plus the same key from api_key.txt.
API_KEY_FILE = os.environ.get(
    "CONTROL_API_KEY_FILE", os.path.join(os.path.dirname(__file__), "api_key.txt")
)
try:
    with open(API_KEY_FILE) as f:
        API_KEY = f.read().strip()
except FileNotFoundError:
    raise RuntimeError(
        f"No API key file at {API_KEY_FILE} — it's the only thing stopping "
        "anyone who finds the URL from running these commands. Create it "
        "with a long random secret, e.g.: openssl rand -hex 32 > api_key.txt"
    ) from None

if not API_KEY:
    raise RuntimeError(f"{API_KEY_FILE} is empty — put a secret key in it.")

app = FastAPI()


def require_api_key(x_api_key: str = Header(default=None)) -> None:
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key.")


@app.get("/api/health")
def health(_=Depends(require_api_key)):
    return {"ok": True}


# 1. Define structured input using Pydantic
class GitCommandRequest(BaseModel):
    action: str  # e.g., "status", "log", "branch"
    limit: int = 5


# 2. Strict allowlist mapping to map inputs to safe local commands
ALLOWED_ACTIONS = {
    "status": ["git", "status", "--porcelain"],
    "branch": ["git", "branch", "-a"],
    "log": ["git", "log", "--oneline", "-n"],
}


@app.post("/api/git")
def execute_git_command(request: GitCommandRequest, _=Depends(require_api_key)):
    if request.action not in ALLOWED_ACTIONS:
        raise HTTPException(status_code=400, detail="Action not authorized.")

    # Construct the command safely without shell=True
    base_cmd = ALLOWED_ACTIONS[request.action]
    if request.action == "log":
        base_cmd = base_cmd + [str(request.limit)]

    try:
        # 3. Execute locally and capture output
        result = subprocess.run(base_cmd, capture_output=True, text=True, check=True)
        # 4. Return clean, structured JSON back to the AI agent
        return {
            "success": True,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
    except subprocess.CalledProcessError as e:
        return {
            "success": False,
            "stdout": e.stdout.strip(),
            "stderr": e.stderr.strip(),
            "exit_code": e.returncode,
        }


if __name__ == "__main__":
    # Lets `python3 control_api.py` work directly, not just `uvicorn control_api:app`.
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
