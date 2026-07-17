# Requerimiento: instalar dependencias en cada worktree nuevo

**Estado:** ✅ aplicado (2026-07-16)

## Problema

`git_setup_node` (en `agile_scripts/langgraph_fleet.py`) crea un `git worktree add` aislado por ticket, pero nunca instalaba dependencias ahí después. Los worktrees de git nunca comparten `node_modules` (está en `.gitignore`), así que cada worktree nuevo nacía sin dependencias — `validation_gate` fallaba con `VITEST: ⚠ vitest no encontrado en node_modules` en el primer intento sobre `app-tennis`.

## Fix aplicado

Justo después de crear el worktree (línea ~925, tras el `_log(f"[git_setup] Worktree aislado: ...")`), se agregó:

```python
    # Los worktrees de git nunca comparten node_modules (está en .gitignore).
    # Instalar dependencias acá para que tsc/vitest existan en la validación.
    if os.path.exists(os.path.join(worktree_path, "package.json")):
        install_rc, install_out = _run(["npm", "install"], cwd=worktree_path, timeout=300)
        _log(f"[git_setup] npm install: {'✓ OK' if install_rc == 0 else '✗ FALLÓ'} ({install_out.strip()[:200]})")
```

## Verificación

Confirmado en el log del job `TASK-68cf789a` sobre `app-tennis`:
```
[08:02:46] [git_setup] npm install: ✓ OK (> tennis-coach-pro@0.1.0 postinstall
> prisma generate
...)
```

## Riesgo pendiente / mejora futura

`npm install` completo por worktree es lento (~45s en este caso) y repetitivo si se despachan muchos tickets sobre el mismo repo. Si el volumen de despachos crece, evaluar:
- Cachear `node_modules` entre worktrees del mismo repo (symlink o copia con hardlinks desde el checkout principal), con el riesgo de que tickets concurrentes corrompan un `node_modules` compartido si uno instala una dependencia nueva mientras otro corre tests.
- `npm ci` en vez de `npm install` si el proyecto tiene `package-lock.json` (más rápido y determinístico), solo si no rompe proyectos sin lockfile.

No es bloqueante — no implementar salvo que el tiempo por despacho se vuelva un problema real.
