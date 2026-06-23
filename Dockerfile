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
    git \
    ruby

# n8n instalado globalmente (misma versión que la imagen oficial)
RUN npm install -g n8n@latest

# Dependencias Python para el motor LangGraph
COPY requirements.txt /tmp/requirements.txt
RUN python3 -m pip install --no-cache-dir --break-system-packages \
    -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# Directorios de trabajo
RUN mkdir -p /data/scripts /workspace /home/node/.n8n \
    && chown -R node:node /data/scripts /workspace /home/node/.n8n

USER node
WORKDIR /home/node

EXPOSE 5678

CMD ["n8n", "start"]
