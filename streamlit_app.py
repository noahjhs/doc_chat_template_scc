import streamlit as st
from openai import OpenAI

# Initialize formats
with open("assets/text.md", "r") as f:
    st.markdown(
        f.read(),
        unsafe_allow_html=True
    )

# Show title and description.
st.title("📄 Doc talk")
st.write(
    "Provide a document and talk about it. "
#    "To use this app, you need to provide an OpenAI API key, which you can get [here](https://platform.openai.com/account/api-keys). "
)

openai_api_key = st.secrets["OPENAI_API_KEY"]

# Create an OpenAI client.
client = OpenAI(api_key=openai_api_key)

if True:

    # Let the user pick a model.
    model = st.selectbox(
        "Model",
        ["gpt-4.1-mini", "gpt-4.1", "gpt-4o", "gpt-3.5-turbo"],
    )

    # Let the user upload a file via `st.file_uploader`.
    uploaded_file = st.file_uploader(
        "Gimme a .txt or .md", type=("txt", "md")
    )

    # Ask the user for a question via `st.text_area`.
    question = st.text_area(
        "Now whaddya wanna know?",
        placeholder="",
        disabled=not uploaded_file,
    )

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
