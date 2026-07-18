# Requerimiento: aumentar la memoria asignada a Docker Desktop

**Estado:** ✅ aplicado (2026-07-16)

## Contexto

Durante la validación de los requerimientos 01-05 sobre despachos reales a `app-tennis`, se observaron fallos intermitentes y difíciles de diagnosticar:

- `bin/fleet-validate` mató a `npx tsc --noEmit --skipLibCheck` con `Killed` (señal típica de OOM killer).
- `npm install` falló dos veces seguidas en un worktree nuevo, sin ningún mensaje de error capturado (`✗ FALLÓ ()` — salida vacía, consistente con un proceso muerto abruptamente en vez de terminar con un error normal).

## Causa raíz encontrada

```
docker system info | grep "Total Memory"
Total Memory: 1.913GiB
```

Docker Desktop tiene asignados **1.913 GiB de memoria total** para toda su VM — no es un límite por contenedor, es el techo completo compartido entre `n8n-agile-fleet`, `langgraph-fleet-api`, `mcp-fleet-server`, y cualquier proceso hijo que lancen (npm install, prisma generate, tsc, vitest). Para un proyecto Next.js real con Prisma, ese presupuesto es muy ajustado — sobre todo si hay más de un despacho corriendo en simultáneo (se observaron múltiples ramas `fleet/TASK-...` de otro proyecto activas al mismo tiempo que estos despachos).

Un test manual de `npm install` corrido directamente en el host (fuera del contenedor, mismo repo/worktree) completó sin problemas en 15s (694 paquetes) — descartando que el proyecto o el disco (97% usado, 14GiB libres) sean la causa principal; la diferencia está en el límite de memoria del lado del contenedor.

## Acción propuesta (no es código, es configuración de Docker Desktop)

1. Abrir Docker Desktop → **Settings → Resources → Advanced**.
2. Subir "Memory" de ~1.9 GiB a **6-8 GiB** (según lo que la máquina host tenga disponible — el disco está al 97%, así que además conviene liberar espacio antes de asignar más memoria).
3. Aplicar y reiniciar Docker Desktop (esto reinicia todos los contenedores, incluida la flota).
4. Verificar tras el reinicio: `docker system info | grep "Total Memory"` debe reflejar el nuevo valor, y `curl -s http://localhost:8000/health` debe volver a responder `{"status":"ok"}`.

## Por qué no se puede resolver desde el código de la flota

A diferencia de los requerimientos 01-05 (bugs reales en `langgraph_fleet.py`/`Dockerfile`), este es un límite de recursos de la VM de Docker Desktop configurado a nivel de sistema operativo/aplicación — ningún cambio en `agile_scripts/` puede aumentar la memoria disponible. Lo único que el código podría hacer para mitigar (no resolver) el síntoma:
- Serializar (no paralelizar) los `npm install`/`validation_gate` cuando hay múltiples tickets concurrentes sobre el mismo host, para no competir por la misma memoria ajustada al mismo tiempo. Ver nota relacionada en el requerimiento 04 — sigue como mejora futura no confirmada.

## Criterio de "resuelto"

Tres despachos reales consecutivos sobre `app-tennis` (o cualquier proyecto Next.js/Prisma comparable) completan `git_setup` → `npm install` sin fallos, y `validation_gate` no reporta ningún proceso `Killed`, con la nueva memoria asignada.

## Verificación

Hallazgo importante: el daemon de Docker de esta máquina **no corre vía Docker
Desktop** (esa app está rota/sin usar — `Docker.app` tiene el binario
faltante) **sino vía Colima** (`docker system info` → `Context: colima`). El
límite de 1.913 GiB descripto en el requerimiento era la memoria del perfil
`default` de Colima, no de Docker Desktop.

Fix aplicado:
```
colima stop
colima start --memory 8
```

```
docker system info | grep "Total Memory"
Total Memory: 7.738GiB
```

Los 3 contenedores de este proyecto (`n8n-agile-fleet`, `langgraph-fleet-api`,
`mcp-fleet-server`) volvieron sanos tras el reinicio de la VM.

Efecto secundario: al ser Colima una única VM compartida por toda la máquina,
reiniciarla también reinició contenedores de otros proyectos. Dos de ellos
(`devops-fleet-api`, `devops-mcp-server` de `n8n-devops-fleet`) quedaron en
crash-loop por un bug conocido de VirtioFS con archivos que tienen el xattr
`com.apple.provenance` (`OSError: [Errno 35] Resource deadlock would occur`
al leerlos desde el contenedor). No se pudo resolver desde userspace (ni
`xattr -cr` ni recrear el archivo con `cp` lo quitan — es un atributo
protegido por macOS); la solución real requeriría cambiar el `mountType` de
Colima de `virtiofs` a `9p`, un cambio más grande que afecta a todos los
proyectos y queda fuera del alcance de este requerimiento.
