# Single small image used for all three API replicas AND the one-shot `migrate`
# service. Identical code everywhere is what makes the replicas interchangeable,
# which is what lets Nginx load-balance without sticky sessions.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY tests ./tests

EXPOSE 8000

# Plain HTTP on the private Docker network: TLS is terminated at Nginx.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
