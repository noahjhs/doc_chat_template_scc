FROM python:3.11-slim
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

COPY docker/app-entrypoint.sh /usr/local/bin/app-entrypoint.sh
RUN chmod +x /usr/local/bin/app-entrypoint.sh

EXPOSE 8501
ENTRYPOINT ["app-entrypoint.sh"]
CMD ["streamlit", "run", "casper_app.py", "--server.address=0.0.0.0", "--server.port=8501"]
