import pandas as pd
import streamlit as st
import tiktoken
from openai import OpenAI


# Load pricing data and cache it
@st.cache_data
def load_pricing(path="./.pricing/openai.md"):
    """Parse the 'Standard pricing data' table into a DataFrame indexed by model."""
    with open(path, "r") as f:
        lines = f.read().splitlines()

    header_idx = lines.index("### Standard pricing data")
    table_lines = []
    for line in lines[header_idx + 1 :]:
        if line.startswith("|"):
            table_lines.append(line)
        elif table_lines:
            break

    columns = [cell.strip() for cell in table_lines[0].strip("|").split("|")][1:]

    rows = []
    for row in table_lines[2:]:
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        rows.append(cells)

    df = pd.DataFrame(rows, columns=["Model"] + columns).set_index("Model")
    return df


# Load styles and cache them
@st.cache_data
def load_markdown_styles() -> str:
    return open("assets/text.md", "r").read()


# Create an OpenAI client and cache it
@st.cache_resource
def get_client():
    openai_api_key = st.secrets["OPENAI_API_KEY"]
    return OpenAI(api_key=openai_api_key)


def count_tokens(text: str, model: str) -> int:
    """Count tokens in `text` using the tokenizer for `model`."""
    try:
        encoding = tiktoken.encoding_for_model(model)
    except KeyError:
        encoding = tiktoken.get_encoding("o200k_base")
    return len(encoding.encode(text))
