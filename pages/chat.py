import base64
import json
from datetime import datetime

import requests
import streamlit as st
from openai import OpenAI

from utils.auth import require_login


@st.cache_resource
def get_client():
    return OpenAI(api_key=st.secrets["OPENAI_API_KEY"])


authenticator = require_login()
client = get_client()

# One entry per request: each script rerun (page load, widget change, chat
# message) is a fresh request from the browser. Streamlit reports None for
# localhost connections specifically (see st.context.ip_address docs) —
# show that plainly rather than the literal string "None".
if "ip_log" not in st.session_state:
    st.session_state.ip_log = []
ip = st.context.ip_address or "localhost"
st.session_state.ip_log.append(f"{datetime.now().strftime('%H:%M:%S')}  {ip}")
st.session_state.ip_log = st.session_state.ip_log[-100:]  # cap growth

with st.sidebar:
    authenticator.logout()
    st.caption(f"Signed in as {st.session_state.get('name')}")

    st.divider()
    st.subheader("Request IP log")
    st.text_area(
        "Request IP log",
        value="\n".join(reversed(st.session_state.ip_log)),
        height=150,
        disabled=True,
        label_visibility="collapsed",
    )

    st.divider()
    st.subheader("Local agent")
    local_agent_url = st.query_params.get("local_agent_url", "")
    local_agent_key = st.secrets.get("LOCAL_AGENT_API_KEY", "")

    local_agent_config = None
    if local_agent_url and local_agent_key:
        local_agent_config = {
            "url": local_agent_url.rstrip("/"),
            "api_key": local_agent_key,
        }
        st.caption("🔧 Local agent configured — git status/branch/log, ls, and cd are available.")
    else:
        st.caption(
            "Not connected. Get it from the home page (sidebar nav above) "
            "and run it on your own machine — it'll open a new tab here "
            "already connected."
        )

# All the built-in Responses API tools that don't need extra setup (unlike
# file_search, which needs a vector store), plus the local git tool if
# the agent server above is configured.
TOOLS = [
    {"type": "web_search"},
    {"type": "code_interpreter", "container": {"type": "auto"}},
    {"type": "image_generation"},
]
LOCAL_AGENT_TOOL = {
    "type": "function",
    "name": "run_local_command",
    "description": (
        "Run a command on the user's local machine via their local agent "
        "server — not arbitrary shell access, but a fixed set of safe "
        "operations: read-only git status/branch/log, 'ls' to list the "
        "current directory, and 'cd' to switch which directory later "
        "commands run in (persists across calls until changed again)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["status", "branch", "log", "ls", "cd"],
                "description": "Which local action to run.",
            },
            "limit": {
                "type": "integer",
                "description": "Number of log entries to show (only used for the 'log' action).",
            },
            "path": {
                "type": "string",
                "description": (
                    "Directory to switch to (only used for the 'cd' action). "
                    "Absolute, or relative to the current directory."
                ),
            },
        },
        "required": ["action"],
    },
}
active_tools = TOOLS + ([LOCAL_AGENT_TOOL] if local_agent_config else [])
GIT_ACTIONS = {"status", "branch", "log"}  # the rest ("ls", "cd") aren't git subcommands

if "messages" not in st.session_state:
    st.session_state.messages = []

# The Responses API tracks conversation history server-side, keyed off the
# previous turn's response id.
if "previous_response_id" not in st.session_state:
    st.session_state.previous_response_id = None


def call_local_agent(local_agent_config, action, limit=5, path=None):
    """Call the user's local agent server; never raises, so a connection
    failure just gets reported back to the model as text."""
    try:
        response = requests.post(
            f"{local_agent_config['url']}/api/git",
            json={"action": action, "limit": limit, "path": path},
            headers={"X-API-Key": local_agent_config["api_key"]},
            timeout=15,
        )
        if response.status_code == 401:
            return "Local agent error: invalid API key."
        response.raise_for_status()
        return json.dumps(response.json())
    except requests.RequestException as e:
        return f"Local agent error: {e}"


def show_web_search(searches, sources):
    """Render a demo-friendly summary of a web search tool call: the
    query(ies) used and the deduplicated list of cited sources."""
    if not searches:
        return
    with st.expander(f"🔍 Searched the web: {', '.join(searches)}"):
        seen = {}
        for title, url in sources:
            seen.setdefault(url, title)
        for url, title in seen.items():
            st.markdown(f"- [{title}]({url})")


def show_code_interpreter(code_blocks):
    """Render a demo-friendly summary of code interpreter tool calls: the
    Python code that was actually executed."""
    if not code_blocks:
        return
    label = f"🧮 Ran code ({len(code_blocks)} block{'s' if len(code_blocks) != 1 else ''})"
    with st.expander(label):
        for code in code_blocks:
            st.code(code, language="python")


def show_local_agent_calls(calls):
    """Render a demo-friendly summary of local agent tool calls."""
    if not calls:
        return
    label = f"🔧 Ran {len(calls)} local agent command{'s' if len(calls) != 1 else ''}"
    with st.expander(label):
        for entry in calls:
            if entry["action"] == "cd":
                prefix = "$ cd"
            elif entry["action"] in GIT_ACTIONS:
                prefix = f"$ git {entry['action']}"
            else:
                prefix = f"$ {entry['action']}"
            st.code(f"{prefix}\n{entry['output']}", language="text")


for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.write(message["content"])
        if message.get("image"):
            st.image(base64.b64decode(message["image"]))
        show_web_search(message.get("searches"), message.get("sources", []))
        show_code_interpreter(message.get("code_blocks", []))
        show_local_agent_calls(message.get("local_agent_calls", []))

if prompt := st.chat_input("Chat"):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.write(prompt)

    def capture_response_meta(stream, meta):
        """Captures the response id (for chaining), plus tool activity, from
        the stream as a side effect, since st.write_stream fully consumes it."""
        for event in stream:
            if event.type == "response.completed":
                meta["id"] = event.response.id
                for item in event.response.output:
                    if item.type == "image_generation_call" and item.result:
                        meta["image"] = item.result
                    elif item.type == "web_search_call" and item.action:
                        meta["searches"].append(item.action.query)
                    elif item.type == "code_interpreter_call" and item.code:
                        meta["code_blocks"].append(item.code)
                    elif item.type == "function_call":
                        meta["function_calls"].append(
                            {
                                "call_id": item.call_id,
                                "name": item.name,
                                "arguments": item.arguments,
                            }
                        )
                    elif item.type == "message":
                        for content in item.content:
                            for annotation in (
                                getattr(content, "annotations", None) or []
                            ):
                                if annotation.type == "url_citation":
                                    meta["sources"].append(
                                        (annotation.title, annotation.url)
                                    )
            yield event

    # Accumulates results across hops of the tool-calling loop below: the
    # model can request a tool call, get its output fed back, and decide to
    # call more before giving a final answer.
    aggregate = {
        "searches": [],
        "sources": [],
        "code_blocks": [],
        "local_agent_calls": [],
        "image": None,
    }
    turn_input = [{"role": "user", "content": prompt}]
    full_response = ""

    with st.chat_message("assistant"):
        while True:
            response_meta = {
                "searches": [],
                "sources": [],
                "code_blocks": [],
                "function_calls": [],
            }
            with st.spinner("Thinking..."):
                stream = client.responses.create(
                    model="gpt-4.1-mini",
                    input=turn_input,
                    previous_response_id=st.session_state.previous_response_id,
                    tools=active_tools,
                    stream=True,
                )
                hop_text = st.write_stream(capture_response_meta(stream, response_meta))

            full_response += hop_text
            st.session_state.previous_response_id = response_meta["id"]
            aggregate["searches"].extend(response_meta["searches"])
            aggregate["sources"].extend(response_meta["sources"])
            aggregate["code_blocks"].extend(response_meta["code_blocks"])
            if "image" in response_meta:
                aggregate["image"] = response_meta["image"]

            if not response_meta["function_calls"]:
                break

            # Dispatch each requested tool call and feed the output back in
            # as the next hop's input.
            turn_input = []
            for call in response_meta["function_calls"]:
                args = json.loads(call["arguments"])
                if call["name"] == "run_local_command":
                    action = args.get("action", "")
                    if action == "cd":
                        label = f"🔧 cd {args.get('path', '')}"
                    elif action in GIT_ACTIONS:
                        label = f"🔧 Running: git {action}"
                    else:
                        label = f"🔧 Running: {action}"
                    with st.status(label):
                        output = call_local_agent(
                            local_agent_config,
                            action,
                            args.get("limit", 5),
                            args.get("path"),
                        )
                        st.code(output, language="text")
                    aggregate["local_agent_calls"].append(
                        {"action": action, "output": output}
                    )
                else:
                    output = f"Unknown tool: {call['name']}"
                turn_input.append(
                    {
                        "type": "function_call_output",
                        "call_id": call["call_id"],
                        "output": output,
                    }
                )

        if aggregate["image"]:
            st.image(base64.b64decode(aggregate["image"]))
        show_web_search(aggregate["searches"], aggregate["sources"])
        show_code_interpreter(aggregate["code_blocks"])
        show_local_agent_calls(aggregate["local_agent_calls"])

    st.session_state.messages.append(
        {
            "role": "assistant",
            "content": full_response,
            "image": aggregate["image"],
            "searches": aggregate["searches"],
            "sources": aggregate["sources"],
            "code_blocks": aggregate["code_blocks"],
            "local_agent_calls": aggregate["local_agent_calls"],
        }
    )
