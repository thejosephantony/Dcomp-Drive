FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY controller /app/controller
COPY storage /app/storage
COPY client /app/client
COPY migrations /app/migrations
COPY alembic.ini /app/alembic.ini

ENV PYTHONUNBUFFERED=1
