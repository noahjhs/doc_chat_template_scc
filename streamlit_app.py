from prompt_toolkit import prompt
import streamlit as st

from utils.data_helpers import (
    load_markdown_styles,
    get_client,
    upload_document,
    estimate_cost,
    model_label,
)

# Apply styles
st.markdown(load_markdown_styles(), unsafe_allow_html=True)

# Show title and description.
st.title("📄 Doc talk")
st.write("Provide a document. Talk to me about it. ")

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

    # Every file type the Responses API accepts as `input_file`, grouped for display:
    # https://developers.openai.com/api/docs/guides/file-inputs
    ALLOWED_FILE_TYPES = {
        "PDF": ["pdf"],
        "Spreadsheets": [
            "xla",
            "xlb",
            "xlc",
            "xlm",
            "xls",
            "xlsx",
            "xlt",
            "xlw",
            "csv",
            "tsv",
            "iif",
        ],
        "Documents": ["doc", "docx", "dot", "odt", "rtf"],
        "Presentations": ["pot", "ppa", "pps", "ppt", "pptx", "pwz", "wiz"],
        "Text & code": [
            "asm",
            "bat",
            "c",
            "cc",
            "conf",
            "cpp",
            "css",
            "cxx",
            "def",
            "dic",
            "eml",
            "h",
            "hh",
            "htm",
            "html",
            "ics",
            "ifb",
            "in",
            "js",
            "json",
            "ksh",
            "list",
            "log",
            "markdown",
            "md",
            "mht",
            "mhtml",
            "mime",
            "mjs",
            "nws",
            "pl",
            "py",
            "rst",
            "s",
            "sql",
            "srt",
            "text",
            "txt",
            "vcf",
            "vtt",
            "xml",
        ],
    }

    # Let the user upload a file via `st.file_uploader`.
    uploaded_file = st.file_uploader(
        "Gimme a document",
        type=[ext for exts in ALLOWED_FILE_TYPES.values() for ext in exts],
        help="\n\n".join(
            f"**{category}:** {', '.join(exts)}"
            for category, exts in ALLOWED_FILE_TYPES.items()
        ),
    )

    # Upload to the Files API as soon as a file is selected, rather than
    # waiting for the first chat message. Dedup on Streamlit's per-upload
    # file_id so reruns don't re-upload the same file.
    if uploaded_file:
        if st.session_state.get("uploaded_file_key") != uploaded_file.file_id:
            with st.spinner("Uploading..."):
                st.session_state.uploaded_file_id = upload_document(
                    client, uploaded_file
                )
            st.session_state.uploaded_file_key = uploaded_file.file_id
    else:
        st.session_state.pop("uploaded_file_id", None)
        st.session_state.pop("uploaded_file_key", None)

    # ...or point at one via an external URL instead.
    document_url = st.text_input(
        "...or a document URL",
        placeholder="https://example.com/document.pdf",
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
    disabled=not (uploaded_file or document_url),
):
    # A document only needs to go in once — the Responses API keeps it in
    # server-side history for every later turn — but a *new* or *changed*
    # document should be attached again even mid-conversation.
    active_source_key = uploaded_file.file_id if uploaded_file else document_url

    if active_source_key == st.session_state.get("last_sent_file_key"):
        new_message = {"role": "user", "content": their_prompt}
        context_display = their_prompt
    elif uploaded_file:
        new_message = {
            "role": "user",
            "content": [
                {"type": "input_file", "file_id": st.session_state.uploaded_file_id},
                {"type": "input_text", "text": their_prompt},
            ],
        }
        context_display = (
            f"[Attached file: {uploaded_file.name} "
            f"({st.session_state.uploaded_file_id})]\n\n{their_prompt}"
        )
    else:
        new_message = {
            "role": "user",
            "content": [
                {"type": "input_file", "file_url": document_url},
                {"type": "input_text", "text": their_prompt},
            ],
        }
        context_display = f"[Attached file via URL: {document_url}]\n\n{their_prompt}"

    # Show user's message right away; its token count fills in once the API
    # call returns actual usage below.
    with st.chat_message("user"):
        st.write(their_prompt)
        input_tokens_placeholder = st.empty()
        with st.expander("Inspect prompt"):
            st.code(context_display, language=None)

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
    st.session_state.last_sent_file_key = active_source_key

    # Now that token counts are known, record both turns in the chat history.
    st.session_state.messages.append(
        {
            "role": "user",
            "content": their_prompt,
            "tokens": input_tokens,
            "context": context_display,
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
