from prompt_toolkit import prompt
import streamlit as st

from utils.data_helpers import (
    load_markdown_styles,
    get_client,
    estimate_cost,
    model_label,
)

# Apply styles
st.markdown(
    load_markdown_styles(),
    unsafe_allow_html=True,
)

# Show title and description.
st.title("📄 Doc talk")
st.write(
    "Provide a document. Talk to me about it. ",
)

client = get_client()

# Initialize running spend total
if "total_spend" not in st.session_state:
    st.session_state.total_spend = 0.0

# The Responses API tracks conversation history server-side, keyed off the
# previous turn's response id.
if "previous_response_id" not in st.session_state:
    st.session_state.previous_response_id = None

with st.sidebar:

    models = ["gpt-4.1-mini", "gpt-4.1", "gpt-4o", "gpt-3.5-turbo"]

    # Let the user pick a model.
    model = st.selectbox(
        "Model",
        models,
        accept_new_options=False,
        format_func=model_label,
    )

    # Let the user upload a file via `st.file_uploader`.
    uploaded_file = st.file_uploader(
        "Gimme a .txt or .md",
        type=("txt", "md"),
    )

    # Running total spend, updated in place as the conversation progresses.
    st.divider()
    spend_placeholder = st.empty()
    spend_placeholder.caption(f"Total spend: ${st.session_state.total_spend:.4f}")


# Initialize chat history
if "messages" not in st.session_state:
    st.session_state.messages = []

# Display prior messages
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.write(message["content"])
        st.caption(f"{message['tokens']:,} tokens")
        if "context" in message:
            with st.expander("Inspect prompt"):
                st.code(message["context"], language=None)

# Handle user input
if their_prompt := st.chat_input(
    placeholder="Chat",
    disabled=not uploaded_file,
):
    # Generate this turn's message. The document only needs to go in once —
    # the Responses API keeps it in server-side history for every later turn.
    if st.session_state.messages:
        new_message = {"role": "user", "content": their_prompt}
    else:
        doc = uploaded_file.read().decode()
        new_message = {
            "role": "user",
            "content": f"Here's a document: {doc} \n\n---\n\n {their_prompt}",
        }

    # Show user's message right away; its token count fills in once the API
    # call returns actual usage below.
    with st.chat_message("user"):
        st.write(their_prompt)
        input_tokens_placeholder = st.empty()
        with st.expander("Inspect prompt"):
            st.code(new_message["content"], language=None)

    # Captures the response id (for chaining) and usage from the stream as a
    # side effect, since st.write_stream fully consumes the event stream.
    response_meta = {}

    def capture_response_meta(stream):
        for event in stream:
            if event.type == "response.completed":
                response_meta["id"] = event.response.id
                response_meta["usage"] = event.response.usage
            yield event

    # In a chat message container labeled with the assistant avatar
    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):

            # Get a streaming response from the OpenAI API. Only the new
            # message is sent — previous_response_id pulls in prior turns.
            stream_response = client.responses.create(
                model=model,
                input=[new_message],
                previous_response_id=st.session_state.previous_response_id,
                stream=True,
            )

            # Show the response as it streams in
            full_response = st.write_stream(capture_response_meta(stream_response))

        usage = response_meta["usage"]
        input_tokens = usage.input_tokens
        output_tokens = usage.output_tokens
        st.caption(f"{output_tokens:,} tokens")

    input_tokens_placeholder.caption(f"{input_tokens:,} tokens")
    st.session_state.previous_response_id = response_meta["id"]

    # Now that token counts are known, record both turns in the chat history.
    st.session_state.messages.append(
        {
            "role": "user",
            "content": their_prompt,
            "tokens": input_tokens,
            "context": new_message["content"],
        }
    )
    st.session_state.messages.append(
        {
            "role": "assistant",
            "content": full_response,
            "tokens": output_tokens,
        }
    )

    # Update the running spend total in the sidebar
    st.session_state.total_spend += estimate_cost(model, input_tokens, output_tokens)
    spend_placeholder.caption(f"Total spend: ${st.session_state.total_spend:.4f}")
