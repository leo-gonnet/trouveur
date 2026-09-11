# One image, three roles: web UI, migrations, and the runner. Same code, different command.

FROM python:3.12-slim-bookworm AS build

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first: they change far less often than the source.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev --extra embeddings

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra embeddings

# Bake the embedding model into the image rather than fetching it on first use. A runtime download
# would repeat on every container recreate, needs egress from the runner, and would make the first
# scan after a deploy fail in a way that looks like a pipeline bug.
#
# The name is imported, never written here. Spelling it out again made this layer bake a model the
# code does not load: the app asks for paraphrase-multilingual-MiniLM-L12-v2 while the image
# cached intfloat/multilingual-e5-small, so the download this layer exists to prevent happened on
# the first scan anyway -- silently, until a fastembed release dropped the stale name and turned
# a wrong-but-quiet image into a failed build.
ENV FASTEMBED_CACHE_PATH=/app/.fastembed
RUN uv run --no-dev python -c \
    "from fastembed import TextEmbedding; from trouveur.ingest.embed.local import MODEL; \
     TextEmbedding(model_name=MODEL)"


FROM python:3.12-slim-bookworm

# Same base as the build stage, so the /app/.venv interpreter symlink stays valid.
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system app \
    && useradd --system --gid app --home-dir /app app

WORKDIR /app
COPY --from=build --chown=app:app /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    FASTEMBED_CACHE_PATH=/app/.fastembed
USER app
EXPOSE 8080

CMD ["trouveur", "serve", "--host", "0.0.0.0", "--port", "8080"]
