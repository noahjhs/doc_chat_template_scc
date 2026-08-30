from prompt_toolkit import prompt
import streamlit as st

from utils.data_helpers import (
    load_markdown_styles,
    get_client,
    count_tokens,
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
    # it stays in the conversation history for every later turn.
    if st.session_state.messages:
        new_message = {"role": "user", "content": their_prompt}
    else:
        doc = uploaded_file.read().decode()
        new_message = {
            "role": "user",
            "content": f"Here's a document: {doc} \n\n---\n\n {their_prompt}",
        }

    # Full conversation history (as sent to the API on prior turns) plus the new turn
    history = [
        {"role": m["role"], "content": m.get("context", m["content"])}
        for m in st.session_state.messages
    ]
    our_prompt = history + [new_message]

    input_tokens = sum(count_tokens(m["content"], model) for m in our_prompt)

    # Append the user's message to the chat history
    st.session_state.messages.append(
        {
            "role": "user",
            "content": their_prompt,
            "tokens": input_tokens,
            "context": new_message["content"],
        }
    )

    # Show user's message to the chat message container.
    with st.chat_message("user"):
        st.write(their_prompt)
        st.caption(f"{input_tokens:,} tokens")
        with st.expander("Inspect prompt"):
            st.code(new_message["content"], language=None)

    # In a chat message container labeled with the assistant avatar
    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):

            # Get a streaming response from the OpenAI API
            stream_response = client.chat.completions.create(
                model=model,
                messages=our_prompt,
                stream=True,
            )

            # Show the response as it streams in
            full_response = st.write_stream(stream_response)

        output_tokens = count_tokens(full_response, model)
        st.caption(f"{output_tokens:,} tokens")

    # Add response to chat history
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
