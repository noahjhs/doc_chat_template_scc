import base64
import json

import requests
import streamlit as st
import streamlit_authenticator as stauth
from openai import OpenAI


@st.cache_resource
def get_client():
    return OpenAI(api_key=st.secrets["OPENAI_API_KEY"])


def build_authenticator():
    # Not cached: Authenticate() creates a cookie-manager widget component
    # internally, and Streamlit disallows widget commands in cached functions.
    auth_config = st.secrets["auth"]
    credentials = {
        "usernames": {
            username: dict(fields)
            for username, fields in auth_config["credentials"]["usernames"].items()
        }
    }
    return stauth.Authenticate(
        credentials=credentials,
        cookie_name=auth_config["cookie"]["name"],
        cookie_key=auth_config["cookie"]["key"],
        cookie_expiry_days=auth_config["cookie"]["expiry_days"],
    )


authenticator = build_authenticator()
authenticator.login()

if st.session_state.get("authentication_status") is False:
    st.error("Username/password is incorrect")
    st.stop()
elif st.session_state.get("authentication_status") is None:
    st.warning("Please enter your username and password")
    st.stop()

client = get_client()

with st.sidebar:
    authenticator.logout()
    st.caption(f"Signed in as {st.session_state.get('name')}")

    st.divider()
    st.subheader("Local agent")
    st.caption(
        "Point this at a running control_api.py instance (see that file) "
        "to let the assistant run a few read-only git commands against "
        "your local repo. Entered here for this session only."
    )
    local_agent_url = st.text_input("Local agent URL", placeholder="http://localhost:8000")
    local_agent_key = st.text_input("API key", type="password")

    local_agent_config = None
    if local_agent_url and local_agent_key:
        local_agent_config = {
            "url": local_agent_url.rstrip("/"),
            "api_key": local_agent_key,
        }
        st.caption("🔧 Local agent configured — git status/branch/log are available.")

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
    "name": "run_git_command",
    "description": (
        "Run a read-only git command against the repository on the user's "
        "local machine, via their local agent server."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["status", "branch", "log"],
                "description": "Which git command to run.",
            },
            "limit": {
                "type": "integer",
                "description": "Number of log entries to show (only used for the 'log' action).",
            },
        },
        "required": ["action"],
    },
}
active_tools = TOOLS + ([LOCAL_AGENT_TOOL] if local_agent_config else [])

if "messages" not in st.session_state:
    st.session_state.messages = []

# The Responses API tracks conversation history server-side, keyed off the
# previous turn's response id.
if "previous_response_id" not in st.session_state:
    st.session_state.previous_response_id = None


def call_local_agent(local_agent_config, action, limit=5):
    """Call the user's local agent server; never raises, so a connection
    failure just gets reported back to the model as text."""
    try:
        response = requests.post(
            f"{local_agent_config['url']}/api/git",
            json={"action": action, "limit": limit},
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
    label = f"🔧 Ran {len(calls)} local git command{'s' if len(calls) != 1 else ''}"
    with st.expander(label):
        for entry in calls:
            st.code(f"$ git {entry['action']}\n{entry['output']}", language="text")


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
                if call["name"] == "run_git_command":
                    action = args.get("action", "")
                    with st.status(f"🔧 Running: git {action}"):
                        output = call_local_agent(
                            local_agent_config, action, args.get("limit", 5)
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
