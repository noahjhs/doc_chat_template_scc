"""tools/exposure_probe.py -- run by hand (not a test): needs the test stack up and the mini reachable. It leaves nothing behind.

What does a friend's encrypted mirror (and Casper's own catcher) show, on the test stack?
Two throwaway headless personas on the mini (Riley owns a folder, Sam holds it), driven through the test stack's
MCP like the e2e does; then the holder's disk, Syncthing config and the catcher container are read from outside."""
import json, os, re, subprocess, sys, time, urllib.parse
sys.argv = ["x", "--env", "test"]
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scenario as sc
sc.DEPLOY = sc.CONF["deployments"]["test"]
tag = os.urandom(2).hex()
P = {"riley": {"username": f"xposure-riley-{tag}", "port": 8731, "st_port": 22031},
     "sam": {"username": f"xposure-sam-{tag}", "port": 8732, "st_port": 22032}}
ctx = {}
def out(s=""): print(s, flush=True)
try:
    for k, p in P.items():
        pw = os.urandom(12).hex()
        tok = sc.api("POST", "/signup", {"username": p["username"], "password": pw})["token"]
        sc.save_headless_state(p, {"password": pw, "session": tok})
        st = sc.headless_state(p)
        rk = "rk-" + os.urandom(8).hex()
        pair = sc.api("POST", "/hosts/pair", {"routing_key": rk, "hostname": f"{k}-mac"}, token=tok)
        st.update(routing_key=rk, device_token=pair["device_token"], command_key=pair["command_key"], host_id=pair["host_id"])
        st["agent_token"] = sc.dev("POST", f"/dev/users/{p['username']}/agent-token")["token"]
        sc.save_headless_state(p, st)
        p["st"] = st
    # Riley's folder, with known content, on the mini
    d = f"casper-headless/{P['riley']['username']}/home/Documents"
    tree = f"""mkdir -p ~/{d}/taxes/2025 ~/{d}/empty-dir && printf 'receipts are in the blue folder' > ~/{d}/notes.txt
head -c 3145728 /dev/urandom > ~/{d}/taxes/2025/return.pdf; printf 0123456789 > ~/{d}/taxes/w2.txt; touch -t 201707140240 ~/{d}/notes.txt; chmod 600 ~/{d}/notes.txt"""
    sc.mini(f"mkdir -p ~/casper-headless/{P['riley']['username']}/home; " + tree)
    import types
    _run = sc.subprocess.run
    first = [True]
    def run_once(cmd, *a, **kw):  # copy the daemon binary only for the first persona (overwriting a running arm64 binary kills it)
        if cmd and cmd[0] == "scp" and not first[0]:
            return types.SimpleNamespace(returncode=0)
        if cmd and cmd[0] == "scp":
            first[0] = False
        return _run(cmd, *a, **kw)
    sc.subprocess.run = run_once
    for k, p in P.items():
        try:
            sc.headless_start(p, p["st"])
        except Exception:
            dd = f"casper-headless/{p['username']}"
            out(sc.mini(f"ls -la ~/{dd} ~/{dd}/data 2>&1 | head -20; echo ---; cat ~/{dd}/daemon.log 2>&1 | tail -20; echo ---; cat ~/{dd}/daemon.out 2>&1 | tail"))
            raise
    # wait for presence
    tk = {k: p["st"]["agent_token"] for k, p in P.items()}
    for _ in range(40):
        if all(any(h["connected"] for h in sc.mcp(tk[k], "list_hosts") if h["role"] == "owner") for k in P): break
        time.sleep(3)
    sc.mcp(tk["sam"], "publish_offering", host="sam-mac", max_gb=1, kind="mirror", preview=False)
    invite = sc.mcp(tk["sam"], "create_invite", quota_gb=0.5, for_whom="Riley", preview=False)
    code = re.search(r"CASPER-[A-Z2-9-]{14}", invite).group(0)
    out(sc.mcp(tk["riley"], "redeem_invite", code=code, preview=False)[:120])
    sam_name = f"{P['sam']['username']}/sam-mac"
    out(sc.mcp(tk["riley"], "mirror_folder", source_host="riley-mac", path="Documents", mirrors=[sam_name], use_casper_catcher=True, preview=False)[:140])
    pend = sc.api("GET", "/conversations/pending-approvals", token=P["riley"]["st"]["session"])["pending_approvals"]
    [consent] = [x for x in pend if x["kind"] == "mirror_consent"]
    sc.api("POST", "/hosts/consent", {"approval_id": consent["id"], "approve": True}, token=P["riley"]["st"]["device_token"])
    for _ in range(80):
        txt = sc.mcp(tk["riley"], "protection_status")
        if "protected. Every change" in txt: break
        time.sleep(3)
    out("=== protection_status ===\n" + txt)
    sd = f"~/casper-headless/{P['sam']['username']}/data"
    out("=== ORIGINAL (Riley's Mac) ===")
    out(sc.mini(f"cd ~/{d} && find . -not -path './.st*' | sort | while read f; do stat -f '%N %z bytes mtime=%m mode=%Lp' \"$f\"; done"))
    out("=== SAM'S DISK (held) ===")
    out(sc.mini(f"cd {sd}/mirror/held && find . | sort | while read f; do stat -f '%N %z mtime=%m btime=%B mode=%Lp' \"$f\"; done"))
    out("=== SAM'S SYNCTHING FOLDER CONFIG ===")
    out(sc.mini(f"grep -o '<folder id=\"casper[^>]*>' {sd}/syncthing/config.xml"))
    out("=== CASPER'S CATCHER (test stack container) ===")
    out(sc.mini("source ~/.orbstack/shell/init.zsh; docker exec casper-catcher-test sh -c 'cd /var/syncthing; find . -not -path \"./config/*\" | sort | head -40; grep -o \"<folder id=[^>]*>\" config/config.xml | head'"))
finally:
    for p in P.values():
        try: sc.headless_stop(p)
        except Exception as e: out(f"stop {p['username']}: {e}")
    try:
        names = [p["username"] for p in P.values()]
        for n in names:
            sc.dev("DELETE", f"/dev/users/{urllib.parse.quote(n)}")
        out("cleanup: deleted " + ", ".join(names))
    except Exception as e:
        out(f"cleanup accounts failed: {e}")
