import base64

import streamlit as st
from openai import OpenAI


@st.cache_resource
def get_client():
    return OpenAI(api_key=st.secrets["OPENAI_API_KEY"])


client = get_client()

# All the built-in Responses API tools that don't need extra setup (unlike
# file_search, which needs a vector store).
TOOLS = [
    {"type": "web_search"},
    {"type": "code_interpreter", "container": {"type": "auto"}},
    {"type": "image_generation"},
]

if "messages" not in st.session_state:
    st.session_state.messages = []

# The Responses API tracks conversation history server-side, keyed off the
# previous turn's response id.
if "previous_response_id" not in st.session_state:
    st.session_state.previous_response_id = None


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


for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.write(message["content"])
        if message.get("image"):
            st.image(base64.b64decode(message["image"]))
        show_web_search(message.get("searches"), message.get("sources", []))

if prompt := st.chat_input("Chat"):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.write(prompt)

    # Captures the response id (for chaining), plus any generated image and
    # web search activity, from the stream as a side effect, since
    # st.write_stream fully consumes it.
    response_meta = {"searches": [], "sources": []}

    def capture_response_meta(stream):
        for event in stream:
            if event.type == "response.completed":
                response_meta["id"] = event.response.id
                for item in event.response.output:
                    if item.type == "image_generation_call" and item.result:
                        response_meta["image"] = item.result
                    elif item.type == "web_search_call" and item.action:
                        response_meta["searches"].append(item.action.query)
                    elif item.type == "message":
                        for content in item.content:
                            for annotation in (
                                getattr(content, "annotations", None) or []
                            ):
                                if annotation.type == "url_citation":
                                    response_meta["sources"].append(
                                        (annotation.title, annotation.url)
                                    )
            yield event

    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):
            stream = client.responses.create(
                model="gpt-4.1-mini",
                input=[{"role": "user", "content": prompt}],
                previous_response_id=st.session_state.previous_response_id,
                tools=TOOLS,
                stream=True,
            )
            full_response = st.write_stream(capture_response_meta(stream))
        if "image" in response_meta:
            st.image(base64.b64decode(response_meta["image"]))
        show_web_search(response_meta["searches"], response_meta["sources"])

    st.session_state.previous_response_id = response_meta["id"]
    st.session_state.messages.append(
        {
            "role": "assistant",
            "content": full_response,
            "image": response_meta.get("image"),
            "searches": response_meta["searches"],
            "sources": response_meta["sources"],
        }
    )
