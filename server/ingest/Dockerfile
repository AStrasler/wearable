# Build context MUST be the server/ dir (set in docker-compose.yml) so we can
# install the local whoop-protocol package.
# NumPy 2.5.3 and SciPy 1.18.1 require Python 3.12 or newer.
FROM python:3.12-slim
WORKDIR /app
COPY ingest/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY packages/whoop-protocol /app/whoop-protocol
RUN pip install --no-cache-dir /app/whoop-protocol
COPY db /app/db
COPY ingest/app /app/app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
