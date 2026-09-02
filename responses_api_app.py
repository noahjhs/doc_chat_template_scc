import streamlit as st
from openai import OpenAI


@st.cache_resource
def get_client():
    return OpenAI(api_key=st.secrets["OPENAI_API_KEY"])


client = get_client()

if "messages" not in st.session_state:
    st.session_state.messages = []

# The Responses API tracks conversation history server-side, keyed off the
# previous turn's response id.
if "previous_response_id" not in st.session_state:
    st.session_state.previous_response_id = None

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.write(message["content"])

if prompt := st.chat_input("Chat"):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.write(prompt)

    # Captures the response id from the stream as a side effect, since
    # st.write_stream fully consumes the event stream.
    response_id = {}

    def capture_response_id(stream):
        for event in stream:
            if event.type == "response.completed":
                response_id["value"] = event.response.id
            yield event

    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):
            stream = client.responses.create(
                model="gpt-4.1-mini",
                input=[{"role": "user", "content": prompt}],
                previous_response_id=st.session_state.previous_response_id,
                stream=True,
            )
            full_response = st.write_stream(capture_response_id(stream))

    st.session_state.previous_response_id = response_id["value"]
    st.session_state.messages.append({"role": "assistant", "content": full_response})
