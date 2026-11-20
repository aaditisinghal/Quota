# Quota API + screens, serving the scripted Acme world.
#   docker build -t quota .
#   docker run --rm -p 8000:8000 quota            -> http://localhost:8000
# Model or Stripe sandbox runs: docker run --rm -p 8000:8000 --env-file .env quota --world acme --live

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    QUOTA_ELASTIC_OFF=1

WORKDIR /app

# Dependencies first, read straight from pyproject.toml, so code changes don't bust this layer.
COPY pyproject.toml ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" \
        > /tmp/requirements.txt \
    && pip install -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# Run from source in /app: the code resolves .env and .llm_cache relative to the repo root,
# which keeps them in a directory the unprivileged user can write.
COPY quota ./quota
RUN useradd --create-home --uid 10001 quota && chown -R quota:quota /app
USER quota

EXPOSE 8000

# Healthy only when the ledger's hash chain and every invariant hold, not merely when the port is open.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import json,sys,urllib.request as u; h=json.load(u.urlopen('http://127.0.0.1:8000/ledger/health', timeout=4)); sys.exit(0 if all(i['ok'] for i in h['invariants']) else 1)"

ENTRYPOINT ["python", "-m", "quota", "serve", "--host", "0.0.0.0", "--port", "8000"]
CMD ["--world", "acme"]
