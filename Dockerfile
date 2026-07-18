# Base con Node.js sobre Alpine (incluye apk) + instalamos n8n y Python
FROM node:22-alpine

# Herramientas de sistema necesarias para compilar dependencias nativas de Python
# + git (GitFlow: ramas/commits/PR por ticket) y ruby (validación determinista de
#   sintaxis `ruby -c` sobre el código generado; evita aprobar código que no parsea).
RUN apk add --update --no-cache \
    python3 \
    py3-pip \
    build-base \
    python3-dev \
    libffi-dev \
    openssl-dev \
    curl \
    bash \
    git \
    ruby \
    rust \
    cargo \
    chromium \
    chromium-chromedriver

# Marca todo repo bind-montado (/vaults, /projects) como confiable a nivel
# sistema (/etc/gitconfig) — sobrevive a recreación del contenedor y a
# cambios de UID/ownership tras reinicios de la VM (Colima), a diferencia de
# `git config --global` (efímero, se pierde en cada recreación).
RUN git config --system --add safe.directory '*'

# Vercel CLI — para deploy automático a staging desde staging_tester_node
# Playwright usa el chromium del sistema (PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1)
RUN npm install -g n8n@latest vercel@latest

ENV PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium-browser
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1

# Dependencias Python para el motor LangGraph
COPY requirements.txt /tmp/requirements.txt
RUN python3 -m pip install --no-cache-dir --break-system-packages \
    -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# langgraph-checkpoint-sqlite con --no-deps: su dependencia sqlite-vec no tiene
# distribución para Alpine/musl aarch64 (ni wheel ni sdist), y SqliteSaver — lo
# único que usamos — no la necesita (import lazy solo para vector store).
RUN python3 -m pip install --no-cache-dir --break-system-packages --no-deps \
    "langgraph-checkpoint-sqlite==3.1.0"

# Directorios de trabajo
RUN mkdir -p /data/scripts /workspace /home/node/.n8n \
    && chown -R node:node /data/scripts /workspace /home/node/.n8n

USER node
WORKDIR /home/node

EXPOSE 5678

CMD ["n8n", "start"]
