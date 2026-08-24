from prompt_toolkit import prompt
import streamlit as st
from openai import OpenAI

# Set styles
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


# Initialize chat history
if "messages" not in st.session_state:
    st.session_state.messages = []

# Display prior messages
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.write(message["content"])

# Handle user input
if their_prompt := st.chat_input(
    placeholder="Chat",
    disabled=not uploaded_file,
):
    # Append the user's message to the chat history
    st.session_state.messages.append(
        {
            "role": "user", 
            "content": their_prompt,
        }
    )

    # Show user's message to the chat message container.
    with st.chat_message("user"):
        st.write(their_prompt)

   # Read the user's prompt and document
    doc = uploaded_file.read().decode()
    
    # Generate our prompt
    our_prompt = [
        {
            "role": "user",
            "content": f"Here's a document: {doc} \n\n---\n\n {their_prompt}",
        }
    ]

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

    # Add response to chat history
    st.session_state.messages.append(
        {
            "role": "assistant", 
            "content": full_response
        }
    )