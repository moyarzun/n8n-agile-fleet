# Requerimiento: agregar `bash` a la imagen Docker de la flota

**Estado:** ✅ aplicado (2026-07-16)

## Problema

La imagen base es `node:22-alpine`, que solo trae `/bin/sh` (BusyBox ash) — no `bash`. El `Dockerfile` instala `python3, git, ruby, rust`, etc. vía `apk`, pero nunca instalaba `bash`. Cualquier proyecto cuyo script de validación determinista (`FLEET_VALIDATE_CMD`, ej. `bin/fleet-validate` en `app-tennis`) empiece con `#!/usr/bin/env bash` fallaba con:

```
VALIDACIÓN PROYECTO (`bin/fleet-validate`): ✗ exit 127
env: can't execute 'bash': No such file or directory
```

Esto bloqueaba `validation_gate` para todo proyecto con scripts bash, independientemente de la calidad del código generado.

## Fix aplicado

En `Dockerfile`, agregado `bash \` a la lista de paquetes de la línea `RUN apk add --update --no-cache \` (junto a `curl` y `git`):

```dockerfile
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
```

Requiere reconstruir la imagen (`docker compose build fleet-api n8n-mcp-fleet`) para que tome efecto — no alcanza con reiniciar el contenedor sin rebuild.

## Verificación

```
docker exec langgraph-fleet-api which bash   # → /bin/bash
docker exec langgraph-fleet-api bash -c "echo bash-ok"   # → bash-ok
```

Ambos confirmados tras el rebuild del 2026-07-16.
