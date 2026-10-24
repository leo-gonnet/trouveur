# One image, three roles: web UI, migrations, and the runner. Same code, different command.

FROM python:3.12-slim-bookworm AS deps

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first: they change far less often than the source.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev --extra embeddings --extra archive


# Bake the embedding model into the image rather than fetching it on first use. A runtime download
# would repeat on every container recreate, needs egress from the runner, and would make the first
# scan after a deploy fail in a way that looks like a pipeline bug.
#
# The names are imported, never written here. Spelling one out again made this layer bake a model
# the code does not load: the app asks for paraphrase-multilingual-MiniLM-L12-v2 while the image
# cached intfloat/multilingual-e5-small, so the download this layer exists to prevent happened on
# the first scan anyway -- silently, until a fastembed release dropped the stale name and turned
# a wrong-but-quiet image into a failed build.
#
# Only the two files that name the model are copied, so this stage, and the model layer of the
# image, rebuild when they change rather than on every commit. Without __init__.py files the
# directories import as namespace packages, which skips the package imports that need the rest of
# the source. If local.py gains an import, add its file here or the build fails.
FROM deps AS model
COPY trouveur/ingest/embed/base.py trouveur/ingest/embed/local.py /model/trouveur/ingest/embed/
ENV FASTEMBED_CACHE_PATH=/app/.fastembed
RUN cd /model && /app/.venv/bin/python -c \
    "from fastembed import TextEmbedding; \
     from trouveur.ingest.embed.local import LocalOnnxProvider; \
     TextEmbedding(model_name=LocalOnnxProvider.model)"


FROM python:3.12-slim-bookworm

# Same base as the deps stage, so the /app/.venv interpreter symlink stays valid.
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system app \
    && useradd --system --gid app --home-dir /app app

WORKDIR /app
# Least often changed first, so a code-only commit rebuilds, pushes and pulls a few MB instead of
# the venv and the model again. The project is installed into the venv last, as its own layer:
# installing it in the deps stage restamps a dozen files on every commit, and that alone made the
# whole venv layer new each time.
COPY --from=deps --chown=app:app /app/.venv /app/.venv
COPY --from=model --chown=app:app /app/.fastembed /app/.fastembed
COPY --chown=app:app . /app
RUN --mount=from=ghcr.io/astral-sh/uv:0.12,source=/uv,target=/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    uv sync --frozen --no-dev --extra embeddings --extra archive

# WORKDIR creates /app as root before the COPY lands, and `COPY --chown` only sets ownership on
# what it copies -- so /app itself stays root-owned while its contents do not. $HOME is /app, so
# anything that wants a cache under it fails at runtime as the app user. huggingface_hub's Xet
# backend does exactly that, and the error it surfaces is a bare "Permission denied (os error
# 13)" from inside a Rust extension, which names nothing and points at no path.
#
# Only the cache directory is made writable, not /app: the application's own code has no reason
# to be writable by the user running it.
RUN mkdir -p /app/.cache && chown app:app /app/.cache

ENV PATH="/app/.venv/bin:$PATH" \
    FASTEMBED_CACHE_PATH=/app/.fastembed
USER app
EXPOSE 8080

CMD ["trouveur", "serve", "--host", "0.0.0.0", "--port", "8080"]
