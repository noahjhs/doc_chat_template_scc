import streamlit as st
import streamlit_openai

from utils.data_helpers import load_markdown_styles, estimate_cost, model_label

# Apply styles
st.markdown(load_markdown_styles(), unsafe_allow_html=True)

# Show title and description.
st.title("📄 Doc talk (streamlit-openai)")
st.write(
    "Provide a document. Talk to me about it. Built on the `streamlit-openai` component."
)

models = ["gpt-4.1-mini", "gpt-4.1", "gpt-4o", "gpt-3.5-turbo"]

if "active_model" not in st.session_state:
    st.session_state.active_model = models[0]

# The selectbox is remounted under a fresh key whenever we need it to snap
# back to `active_model` (on revert or on confirmed switch), since a widget's
# own key can't be written to once it has rendered in the current run.
if "model_select_key_version" not in st.session_state:
    st.session_state.model_select_key_version = 0


@st.dialog("Change model?")
def confirm_model_change():
    new_model = st.session_state.pending_model
    st.warning(
        f"Switching to **{model_label(new_model)}** will start a new "
        "conversation — the current one will be discarded."
    )
    switch_col, cancel_col = st.columns(2)
    if switch_col.button("Switch & reset", type="primary", width="stretch"):
        st.session_state.active_model = new_model
        st.session_state.model_select_key_version += 1
        del st.session_state["chat"]
        del st.session_state["pending_model"]
        st.rerun()
    if cancel_col.button("Cancel", width="stretch"):
        del st.session_state["pending_model"]
        st.rerun()


def on_model_change():
    key = f"model_select_{st.session_state.model_select_key_version}"
    new_model = st.session_state[key]
    if "chat" in st.session_state and new_model != st.session_state.active_model:
        # Ask for confirmation before discarding the conversation; remount the
        # widget so it visually reverts until the user decides.
        st.session_state.pending_model = new_model
        st.session_state.model_select_key_version += 1
    else:
        st.session_state.active_model = new_model


with st.sidebar:

    # Unlocked at all times — picking a different model mid-conversation is
    # allowed, but it starts a new conversation, so we confirm first.
    st.selectbox(
        "Model",
        models,
        index=models.index(st.session_state.active_model),
        accept_new_options=False,
        format_func=model_label,
        key=f"model_select_{st.session_state.model_select_key_version}",
        on_change=on_model_change,
    )

    # Let the user upload documents via `st.file_uploader`.
    uploaded_files = st.file_uploader(
        "Gimme a document",
        accept_multiple_files=True,
    )

if st.session_state.get("pending_model"):
    confirm_model_change()

if "chat" not in st.session_state:
    st.session_state.chat = streamlit_openai.Chat(
        api_key=st.secrets["OPENAI_API_KEY"],
        model=st.session_state.active_model,
        instructions="Answer the user's questions about the provided document(s).",
        welcome_message="Upload a document in the sidebar, then ask me about it.",
        allow_web_search=False,
        allow_code_interpreter=False,
        allow_image_generation=False,
    )

st.session_state.chat.run(uploaded_files=uploaded_files)

with st.sidebar:
    # Running spend total, using the chat's own cumulative token counters.
    st.divider()
    chat = st.session_state.chat
    st.caption(f"{chat.input_tokens:,} input tokens · {chat.output_tokens:,} output tokens")
    st.caption(
        f"Total spend: ${estimate_cost(st.session_state.active_model, chat.input_tokens, chat.output_tokens):.4f}"
    )
