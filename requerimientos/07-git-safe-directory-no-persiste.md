# Requerimiento: `git config --global --add safe.directory` no persiste entre recreaciones del contenedor

**Estado:** ✅ aplicado (2026-07-16)

## Contexto

Tras reiniciar la VM de Colima para aumentar su memoria (requerimiento 06), un despacho sobre `app-tennis` falló inmediatamente con:

```
ABORTADO: el workspace no es un repositorio git (o git no está disponible).
```

El repo claramente es válido y existe (visible con `ls` desde el contenedor). La causa real, confirmada con `git status` manual dentro del contenedor:

```
fatal: detected dubious ownership in repository at '/vaults/sdelvillar/tennis-app/app-tennis'
```

El reinicio de la VM cambió cómo se ve la propiedad (UID/GID) de los archivos bind-montados desde el contenedor, disparando la protección `safe.directory` de git moderno.

## Problema con el workaround aplicado

Se corrió manualmente:
```
docker exec -u root langgraph-fleet-api git config --global --add safe.directory '*'
```

Esto escribe en `/root/.gitconfig` — pero:
1. El contenedor corre por defecto como usuario `node` (no root, ver `USER node` al final del `Dockerfile`), mientras que `docker-compose.yml` define `GIT_CONFIG_GLOBAL=/root/.gitconfig` para el servicio `fleet-api` — un `--global` como usuario `node` intenta escribir ahí y falla con `Permission denied` (confirmado). Solo funcionó forzando `-u root` en el `docker exec`.
2. `/root/.gitconfig` no está en ningún volumen persistente del `docker-compose.yml` — se pierde en cualquier recreación del contenedor (`docker compose up -d --force-recreate`, o un rebuild de imagen). El fix aplicado ahora es un parche transitorio, no sobrevive al próximo ciclo de vida del contenedor.

## Fix propuesto

Usar configuración a nivel **sistema** (`/etc/gitconfig`, no `--global`) horneada en el `Dockerfile` — sobrevive a cualquier recreación porque queda en la imagen, y no depende de qué usuario (`root` o `node`) ejecute `git` en tiempo de ejecución:

```dockerfile
# Después de instalar git (línea ~15 del bloque apk add):
RUN git config --system --add safe.directory '*'
```

Alternativa si `--system` diera problemas de permisos durante el build (no debería, el build corre como root antes del `USER node` final): escribir directamente el archivo:
```dockerfile
RUN printf '[safe]\n\tdirectory = *\n' > /etc/gitconfig
```

## Criterios de aceptación

1. Tras `docker compose build fleet-api n8n-mcp-fleet && docker compose up -d --force-recreate`, un `git status` dentro del contenedor sobre cualquier repo montado en `/vaults` o `/projects` **no** debe fallar con "dubious ownership", sin necesidad de ningún `docker exec` manual posterior.
2. Confirmar que `GIT_CONFIG_GLOBAL=/root/.gitconfig` en `docker-compose.yml` sigue sin causar conflicto — la config `--system` en `/etc/gitconfig` se aplica independientemente de qué `--global` esté configurado (git combina ambos niveles, sistema + global + local).
3. Repetir la prueba tras un reinicio completo de la VM de Colima (`colima stop && colima start --memory 8`) para confirmar que el fix sobrevive específicamente al escenario que lo disparó la primera vez (cambio de UID/ownership tras reinicio de VM), no solo a un simple `docker compose restart`.

## Nota

El wildcard `'*'` es apropiado acá porque `/vaults` y `/projects` ya son directorios completamente confiables por diseño de este sistema (el usuario los monta explícitamente para que la flota opere sobre ellos) — no hay necesidad de listar cada proyecto individualmente.

## Verificación

Fix aplicado literalmente como se propuso: `RUN git config --system --add safe.directory '*'` agregado al `Dockerfile` tras el bloque `apk add`. Como el build corre como root antes del `USER node` final, no hay problema de permisos escribiendo `/etc/gitconfig`.

Pendiente de verificación end-to-end (criterios 1 y 3, requieren rebuild real de la imagen + reinicio de Colima) — se hace en conjunto con el resto de los cambios de esta tanda de requerimientos, ver nota de despliegue al pie de este directorio.
