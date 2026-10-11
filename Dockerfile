FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends -y tzdata \
    && groupadd --gid 10001 bot \
    && useradd --uid 10001 --gid bot --create-home bot \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY --chown=10001:10001 main.py .
COPY --chown=10001:10001 finance_bot ./finance_bot

USER 10001:10001

CMD ["python", "main.py"]
