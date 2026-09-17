"""Casper backend test harness -- a client-side library (client.py) plus a
thin Typer CLI (cli.py) on top, both driving auth_service's HTTP API
directly. Built to replace Streamlit as the primary way this project's
security-rule engine gets exercised and iterated on (see the top-level
plan: "Deprecate Streamlit's business logic: extract a backend + build a
CLI test harness"). Deliberately has no GUI/Streamlit dependency at all."""
