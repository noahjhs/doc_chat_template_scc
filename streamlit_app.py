import streamlit as st
from openai import OpenAI

# for live coding
#st.session_state.clear()

# Initialize formats
with open("assets/text.md", "r") as f:
    st.markdown(
        f.read(),
        unsafe_allow_html=True,
    )

# Show title and description.
st.title("📄 Doc talk")
st.write(
    "Provide a document. Talk to me about it. ",
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

def answer_them():

    # Read the user's prompt and document
    doc = uploaded_file.read().decode()
    their_prompt = st.session_state.prompt

    # Generate our prompt
    our_prompt = [
        {
            "role": "user",
            "content": f"Here's a document: {doc} \n\n---\n\n {their_prompt}",
        }
    ]

    # Prompt the model, get its response
    response = client.chat.completions.create(
        model=model,
        messages=our_prompt,
        stream=True,
    )

    # Show the response
    st.write_stream(response)



# Prompt the user
if their_prompt := st.chat_input(
    placeholder="Chat",
    disabled=not uploaded_file,
    key="prompt",

):
    # Show the user's prompt in the chat message container.
    st.chat_message("user").write(their_prompt)

    # Get the model's response and show it in a chat message container.
    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):
            answer_them()

