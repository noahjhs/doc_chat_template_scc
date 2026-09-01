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

with st.sidebar:

    models = ["gpt-4.1-mini", "gpt-4.1", "gpt-4o", "gpt-3.5-turbo"]

    # The model is fixed once the chat starts (streamlit-openai keeps its own
    # Chat object alive in session state), so lock the picker after that.
    model = st.selectbox(
        "Model",
        models,
        accept_new_options=False,
        format_func=model_label,
        disabled="chat" in st.session_state,
    )

    # Let the user upload documents via `st.file_uploader`.
    uploaded_files = st.file_uploader(
        "Gimme a document",
        accept_multiple_files=True,
    )

if "chat" not in st.session_state:
    st.session_state.chat = streamlit_openai.Chat(
        api_key=st.secrets["OPENAI_API_KEY"],
        model=model,
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
    st.caption(f"Total spend: ${estimate_cost(model, chat.input_tokens, chat.output_tokens):.4f}")
