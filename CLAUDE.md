# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```

## Architecture

Single-file Streamlit app (`streamlit_app.py`). The user provides an OpenAI API key in the UI, uploads a `.txt` or `.md` file, asks a question, and the full document + question are sent to `gpt-3.5-turbo` in a single prompt. The response is streamed back via `st.write_stream`.

The OpenAI API key can alternatively be stored in `.streamlit/secrets.toml` and accessed via `st.secrets` instead of the UI input.
