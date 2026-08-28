#!/bin/sh
set -e

# Install Python dependencies for the Streamlit app (requirements.txt is the
# only dependency manifest this repo ships; no lockfile-based tool is used).
pip install -r requirements.txt
