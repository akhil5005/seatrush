# Tiny image whose only job is to generate load. Kept separate from the API
# image so the load generator can run on the private compose network in
# `direct` mode (straight to api1/api2/api3, bypassing Nginx).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY flashsale.py .

ENTRYPOINT ["python", "flashsale.py"]
