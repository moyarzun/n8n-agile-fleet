# Requerimiento: `git_setup_node` debe abortar (o reintentar) si `npm install` falla, no solo loguearlo

**Estado:** ✅ aplicado (2026-07-16)

## Contexto

Al validar el requerimiento 01 (`npm install` en cada worktree nuevo) sobre un segundo despacho real (`TASK-ee412dcb`, `app-tennis`, tras el fix del requerimiento 03), el log mostró:

```
[15:23:52] [git_setup] npm install: ✗ FALLÓ ()
```

El pipeline **no abortó** — siguió adelante normalmente hacia `planner`/`dynamic_developer`. El worktree quedó con `node_modules` parcialmente instalado (524 entradas de directorio, pero paquetes clave como `vitest` presentes solo como carpeta vacía, sin sus archivos reales — probablemente el install murió a mitad de camino).

Consecuencia: `validation_gate` falló repetidamente durante 3-4 ciclos completos (`~20 min` de cómputo LLM) con errores confusos (`Could not resolve 'vitest/config'`) que llevaron a sospechar erróneamente que el agente `Node` había reescrito `vitest.config.ts` de forma destructiva — **no fue así**, el archivo estaba intacto. El verdadero problema (dependencias corruptas) quedó oculto porque el fallo de `npm install` solo se logueó, nunca se trató como bloqueante.

## Problema en el código actual

El fix del requerimiento 01 agregó:

```python
    if os.path.exists(os.path.join(worktree_path, "package.json")):
        install_rc, install_out = _run(["npm", "install"], cwd=worktree_path, timeout=300)
        _log(f"[git_setup] npm install: {'✓ OK' if install_rc == 0 else '✗ FALLÓ'} ({install_out.strip()[:200]})")
```

Este bloque **nunca chequea `install_rc`** para decidir si continuar — el pipeline avanza igual sin importar el resultado.

## Fix propuesto

```python
    if os.path.exists(os.path.join(worktree_path, "package.json")):
        install_rc, install_out = _run(["npm", "install"], cwd=worktree_path, timeout=300)
        if install_rc != 0:
            # Un solo reintento antes de abortar — cubre fallos transitorios
            # (contención de red/cache si hay varios despachos concurrentes
            # sobre el mismo repo).
            _log(f"[git_setup] npm install falló, reintentando una vez: {install_out.strip()[:200]}")
            install_rc, install_out = _run(["npm", "install"], cwd=worktree_path, timeout=300)

        if install_rc != 0:
            _git(["worktree", "remove", "--force", worktree_path], workspace)
            _git(["worktree", "prune"], workspace)
            shutil.rmtree(worktree_path, ignore_errors=True)
            return _abort(
                f"ABORTADO: npm install falló dos veces en el worktree nuevo. "
                f"No tiene sentido continuar con dependencias parcialmente "
                f"instaladas — los ciclos de validación fallarían de forma "
                f"confusa sin indicar la causa real.\n\n{install_out.strip()[:500]}"
            )

        _log(f"[git_setup] npm install: ✓ OK ({install_out.strip()[:200]})")
```

## Criterios de aceptación

1. Si `npm install` falla dos veces seguidas, `git_setup_node` debe abortar el grafo completo (mismo mecanismo que el abort existente por árbol sucio o repo no-git) — no debe llegar a `planner`/`dynamic_developer` con dependencias rotas.
2. Si `npm install` falla una vez pero el reintento tiene éxito, el pipeline continúa normalmente (no se pierde el caso transitorio).
3. El worktree y la rama del intento abortado se limpian (no quedan huérfanos en `.fleet-worktrees/`).
4. Test de regresión: mockear `_run` para simular 2 fallos consecutivos de `npm install` y confirmar que `git_setup_node` retorna `aborted=True` con un mensaje que mencione "npm install".

## Nota relacionada

Esto también sugiere revisar si conviene serializar (no paralelizar) los `npm install` cuando hay múltiples despachos concurrentes sobre el **mismo repo** — si la contención de red/cache de npm es la causa real del fallo intermitente, un lock por repo (no por ticket) evitaría el problema de raíz en vez de solo reintentar. No implementar esto último sin confirmar primero que la contención es efectivamente la causa (podría ser simplemente un timeout de red puntual sin relación con concurrencia).

## Verificación

Fix aplicado literalmente como se propuso en `agile_scripts/langgraph_fleet.py`
(reintento único + abort con limpieza de worktree/rama si vuelve a fallar).

Tests de regresión en `tests/test_git_setup_node.py`:

```
test_npm_install_falla_dos_veces_aborta_git_setup PASSED
test_npm_install_falla_una_vez_pero_reintento_ok_continua PASSED
```

El primero mockea `_run` para que solo las llamadas `["npm", "install"]`
fallen (dejando pasar las llamadas de `git` reales), confirma `aborted=True`,
el mensaje menciona "npm install" y que no queda worktree huérfano en
`.fleet-worktrees/`. El segundo confirma que un fallo transitorio con
reintento exitoso no aborta el pipeline.

Nota de alcance: no se implementó el lock por repo mencionado en la sección
anterior — sigue siendo una mejora futura, no confirmada como necesaria.
