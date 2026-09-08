FROM python:3.11-slim
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Flat copy (not "COPY . .") -- main.py imports db/models by bare name
# ("from db import ...") relying on the process's cwd, same as the existing
# "cd auth_service && uvicorn main:app" invocation. WORKDIR /app reproduces
# that, so main.py/db.py/models.py need zero changes for this to work.
COPY main.py db.py models.py ./

COPY docker-entrypoint.sh /usr/local/bin/auth-entrypoint.sh
RUN chmod +x /usr/local/bin/auth-entrypoint.sh

EXPOSE 8100
ENTRYPOINT ["auth-entrypoint.sh"]
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8100"]
