# syntax=docker/dockerfile:1
# =============================================================================
# Multi-stage:
#   1) build-web: Node/Vite → /web/dist (efêmero)
#   2) build-api: Python + toolchain de compilação → /opt/venv (só deps runtime)
#   3) runtime:   python:3.13-slim + só os artefatos, non-root, sem toolchain
#
# Os cache mounts (`RUN --mount=type=cache`) exigem BuildKit — padrão no
# Docker 23+, Docker Desktop, docker compose moderno e GitHub Actions.
# =============================================================================

# ---- Stage 1: front ---------------------------------------------------------
# A versão do Node vem de `.node-version` (fonte única): o CI passa
# `--build-arg NODE_VERSION=$(cat .node-version)`. O default é só fallback e
# um guard no CI falha se ele divergir do arquivo.
ARG NODE_VERSION=24
FROM node:${NODE_VERSION}-alpine AS build-web
WORKDIR /web
# browsers do Playwright nao sao necessarios no build do Vite
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
COPY apps/web/package.json apps/web/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci --no-audit --no-fund
COPY apps/web ./
RUN npm run build

# ---- Stage 2: dependências Python (toolchain só existe aqui) ----------------
FROM python:3.15-slim AS build-api

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

# build-essential/libpq-dev ficam APENAS neste stage: psycopg[binary] e
# psycopg2-binary trazem libpq embutida, então o runtime não precisa deles.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY apps/api/pyproject.toml apps/api/uv.lock /app/apps/api/
RUN --mount=type=cache,target=/root/.cache/uv \
    cd /app/apps/api && uv sync --frozen --no-dev --python /usr/local/bin/python3.13

# ---- Stage 3: runtime -------------------------------------------------------
# MESMA base do build-api de propósito: o venv em /opt/venv tem pyvenv.cfg
# apontando para /usr/local/bin/python3.13, que existe identicamente aqui.
# Trocar a base (ex.: alpine, distro-slim diferente) quebra o venv.
FROM python:3.15-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app/apps/api

# tini NÃO vem na base Debian slim; instalado só aqui (~algumas centenas de KB)
# como PID 1 para reap de zumbis. Alternativa seria `init: true` no compose,
# mas o Dockerfile precisa funcionar via `docker run` puro também.
#
# Remove o pip que vem na base: não é usado em runtime (o venv é do uv e não
# inclui system site-packages) e a cópia da base vendoriza libs com CVE HIGH
# (msgpack, urllib3, pkg_resources). Remove também o wheel do ensurepip.
RUN apt-get update && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /usr/local/lib/python3.13/site-packages/pip* \
              /usr/local/bin/pip* \
              /usr/local/lib/python3.13/ensurepip/_bundled/pip-*.whl

# Usuário sem shell, uid/gid fixos. Porta >1024 e migrations não gravam disco,
# então não precisa de root nem de setcap.
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app

# Só o venv do stage de build (sem toolchain). Permissões padrão 755/644 já
# são legíveis pelo usuário `app`; não precisa de chown recursivo (caro).
COPY --from=build-api /opt/venv /opt/venv

# apps/api chega sem tests/ e .hypothesis/ (ver .dockerignore).
COPY --chown=app:app apps/api /app/apps/api
COPY --chown=app:app scripts /app/scripts
# Copia o build do front (Stage 1)
COPY --from=build-web --chown=app:app /web/dist /app/apps/web/dist

WORKDIR /app
USER app

# Porta interna do container (o Coolify usa o domínio com o sufixo :8000).
# Não publica nada no host: a publicação é do Compose, não da imagem.
EXPOSE 8000

# Healthcheck sem curl (curl não está mais na imagem): urllib do venv.
# urllib.urlopen levanta em non-200/sem conexão → exit != 0.
#
# Usa /api/v1/livez (LIVENESS), não /api/v1/healthz (readiness): o healthcheck
# do container decide restart, e uma queda do Postgres não deve reiniciar um
# processo saudável — a API vive sem o banco; quem depende do Postgres é o
# Postgres. Readiness (503 + degraded) é para proxy/LB.
# A porta é a INTERNA do container (API_INTERNAL_PORT), não a publicada no host.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["/opt/venv/bin/python", "-c", "import os, urllib.request; urllib.request.urlopen('http://localhost:%s/api/v1/livez' % os.environ.get('API_INTERNAL_PORT', '8000'), timeout=4)"]

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["bash", "-lc", "/app/scripts/entrypoint.sh api"]
