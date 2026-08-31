import pandas as pd
import streamlit as st
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


def upload_document(client: OpenAI, uploaded_file) -> str:
    """Upload a file via the Files API for use as Responses API input; returns its file id."""
    result = client.files.create(file=(uploaded_file.name, uploaded_file), purpose="user_data")
    return result.id


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate the dollar cost of a turn using short-context pricing per 1M tokens."""
    prices = load_pricing().loc[model, ["Short context input", "Short context output"]]
    input_price, output_price = (float(p.strip("$")) for p in prices)
    return input_tokens / 1_000_000 * input_price + output_tokens / 1_000_000 * output_price


def model_label(model: str) -> str:
    """Model name with input/output prices (per 1M tokens) for display in a selector."""
    row = load_pricing().loc[model]
    return f"{model} ({row['Short context input']} in / {row['Short context output']} out)"
