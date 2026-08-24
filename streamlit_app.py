import streamlit as st
from openai import OpenAI

# for live coding
#st.session_state.clear()

# Initialize sidebar
#if 'sidebar_state' not in st.session_state:
#    st.session_state.sidebar_state = 'expanded'

#st.set_page_config(initial_sidebar_state = st.session_state.sidebar_state)

# Initialize formats
with open("assets/text.md", "r") as f:
    st.markdown(
        f.read(),
        unsafe_allow_html=True
    )

# Show title and description.
st.title("📄 Doc talk")
st.write(
    "Provide a document and talk about it. ",
)

openai_api_key = st.secrets["OPENAI_API_KEY"]

# Create an OpenAI client.
client = OpenAI(api_key=openai_api_key)

with st.sidebar:

    # Let the user pick a model.
    model = st.selectbox(
        "Model",
        ["gpt-4.1-mini", "gpt-4.1", "gpt-4o", "gpt-3.5-turbo"],
    )

    # Let the user upload a file via `st.file_uploader`.
    uploaded_file = st.file_uploader(
        "Gimme a .txt or .md", 
        type=("txt", "md"),
    )

# Put away sidebar
#if uploaded_file and st.session_state.sidebar_state == 'expanded':
#    st.session_state.sidebar_state = 'collapsed'
#    st.rerun()

def process_and_reset():
    # Save the typed text into processing variable
    st.session_state.submitted_text = st.session_state.my_text_area
    
    # Immediately clear the widget's internal session state
    st.session_state.my_text_area = ""

# Ask the user for a question via `st.text_area`.
st.text_area(
    "Now whaddya wanna know?",
    placeholder="",
    disabled=not uploaded_file,
    key="my_text_area",
    on_change=process_and_reset,
)

question = st.session_state.get("submitted_text", "")

if uploaded_file and question:

    # Process the uploaded file and question.
    document = uploaded_file.read().decode()
    messages = [
        {
            "role": "user",
            "content": f"Here's a document: {document} \n\n---\n\n {question}",
        }
    ]

    # Generate an answer using the OpenAI API.
    stream = client.chat.completions.create(
        model=model,
        messages=messages,
        stream=True,
    )

    # Stream the response to the app using `st.write_stream`.
    st.write_stream(stream)

