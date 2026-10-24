# One image for the web UI, migrations and the runner; only the command differs.
#
# Three stages: `deps` installs the libraries, `model` downloads the embedding model, and the final
# stage puts them together with the code. Layers go from least to most often changed, so a code
# commit rebuilds and ships only a few MB.

FROM python:3.12-slim-bookworm AS deps

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev --extra embeddings --extra archive


# The model is baked in so containers never download it at runtime. Its name is read from the
# code, never written here: a copy of the name once drifted and baked the wrong model.
FROM deps AS model
COPY trouveur/ingest/embed/base.py trouveur/ingest/embed/local.py /model/trouveur/ingest/embed/
ENV FASTEMBED_CACHE_PATH=/app/.fastembed
RUN cd /model && /app/.venv/bin/python -c \
    "from fastembed import TextEmbedding; \
     from trouveur.ingest.embed.local import LocalOnnxProvider; \
     TextEmbedding(model_name=LocalOnnxProvider.model)"


FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system app \
    && useradd --system --gid app --home-dir /app app

WORKDIR /app
COPY --from=deps --chown=app:app /app/.venv /app/.venv
COPY --from=model --chown=app:app /app/.fastembed /app/.fastembed
COPY --chown=app:app . /app
RUN --mount=from=ghcr.io/astral-sh/uv:0.12,source=/uv,target=/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    uv sync --frozen --no-dev --extra embeddings --extra archive

# The app runs as a non-root user that can write only its cache directory.
RUN mkdir -p /app/.cache && chown app:app /app/.cache

ENV PATH="/app/.venv/bin:$PATH" \
    FASTEMBED_CACHE_PATH=/app/.fastembed
USER app
EXPOSE 8080

CMD ["trouveur", "serve", "--host", "0.0.0.0", "--port", "8080"]
