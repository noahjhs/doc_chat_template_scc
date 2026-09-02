# get LLM pricing
curl --create-dirs -o .pricing/openai.md "https://developers.openai.com/api/docs/pricing.md" 

# setup secrets on Render host
mkdir -p /opt/render/project/src/.streamlit && cp /etc/secrets/secrets.toml /opt/render/project/src/.streamlit/secrets.toml
