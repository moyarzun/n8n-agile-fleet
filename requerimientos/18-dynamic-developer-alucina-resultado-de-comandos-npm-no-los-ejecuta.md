# Requerimiento: `dynamic_developer` no ejecuta comandos npm/npx reales — cuando se le pide "correr" una instalación, alucina el resultado y reescribe package.json con un árbol de dependencias inventado (downgrades masivos, paquetes eliminados/agregados sin pedirlo)

**Estado:** 🔴 abierto — severidad alta

## Contexto

Al iniciar el plan `mobile-cleanup` (workstream independiente, solo toca `mobile/` — el proyecto Expo/React Native, con su propio `package.json` separado del proyecto Next.js raíz), se despachó únicamente la Tarea 1: instalar `jest-expo` + `@testing-library/react-native` como primer test runner de `mobile/` (que hoy no tiene ninguno configurado). `job_id b12a5330-368e-43e0-a802-cc9db8815f9c`, `ticket_id TASK-2cd6964f`, workspace `/vaults/sdelvillar/tennis-app/app-tennis`, rama `fleet/TASK-2cd6964f-implementar-la-tarea-1-del-plan-docs-sup`.

El `requerimiento` pedía explícitamente, como pasos 1 y 2:

> "Desde el directorio mobile/, correr `npx expo install jest-expo --check` ... Luego correr `npm install --save-dev @testing-library/react-native react-test-renderer` desde mobile/."

El job terminó `✅ Aprobado+validado` en 1 ciclo. **No lo mergeé** porque al revisar el diff manualmente encontré una reescritura completa y no solicitada del árbol de dependencias de `mobile/package.json`.

## Problema — evidencia del diff real

`git diff refactor/modular-dry...fleet/TASK-2cd6964f-... -- mobile/package.json` muestra, además de agregar `jest-expo`/`@testing-library/react-native` (lo pedido), una reescritura completa de versiones **nunca solicitada**:

```diff
-  "name": "tennis-coach-mobile",
+  "name": "tennis-coach-pro-mobile",
...
-    "@clerk/clerk-expo": "^2.5.0",
+    "@clerk/clerk-expo": "^0.23.7",
-    "@expo/vector-icons": "^15.0.3",
+    "@expo/vector-icons": "^14.0.4",
-    "expo-router": "~6.0.23",
+    "expo-router": "~4.0.19",
-    "react": "^19.1.0",
-    "react-dom": "^19.1.0",
+    "react": "18.3.1",
+    "react-dom": "18.3.1",
-    "react-native": "^0.81.5",
+    "react-native": "0.76.7",
-    "react-native-reanimated": "~4.1.1",
+    "react-native-reanimated": "~3.16.1",
-    "react-native-safe-area-context": "~5.6.0",
+    "react-native-safe-area-context": "4.12.0",
    (+ varios downgrades más: expo-constants, expo-device, expo-linking,
      expo-notifications, expo-secure-store, expo-status-bar, react-native-screens)
    (- eliminados por completo: expo-auth-session, expo-font, expo-splash-screen,
      expo-system-ui, expo-updates, expo-web-browser, react-native-web)
    (+ agregados no pedidos: @react-native-async-storage/async-storage,
      @react-native-community/datetimepicker, @react-native-picker/picker,
      react-native-get-random-values, react-native-svg, react-native-url-polyfill)
    "typescript": "~5.7.0" → "~5.3.3"
    "@types/react": "~19.1.10" → "~18.3.12"
```

Downgrades de 2 versiones mayores en dependencias centrales (`react` 19→18, `expo-router` 6→4), eliminación de 7 paquetes usados activamente en la app (`expo-auth-session`, `expo-font`, etc. — confirmables como usados con `grep -rn "expo-font\|expo-auth-session" mobile/app mobile/lib`), y un rename del propio proyecto (`"name"`) — nada de esto estaba en el alcance del ticket, que solo pedía agregar 2 devDependencies de testing.

**Adicionalmente, `mobile/package-lock.json` no tiene NINGÚN cambio** (`git diff ... -- mobile/package-lock.json` no produce salida) — es decir, el lockfile quedó completamente desincronizado del `package.json` reescrito. Un `npm install` real, si de verdad se hubiera ejecutado, habría regenerado el lockfile para reflejar las nuevas versiones.

## Causa raíz (confirmada por el log del propio job)

El log de `dynamic_developer` para el ciclo único de este ticket muestra:

```
[00:05:52] [dynamic_developer] --- Ciclo 1 ---
[00:05:52] [dynamic_developer] Agente 'Node': llamando al modelo LLM...
[00:06:20] [dynamic_developer] Respuesta recibida (2353 chars, 4 bloques FILE_BEGIN detectados)
[00:06:20] [dynamic_developer] Archivos escritos: mobile/package.json, mobile/jest.config.js, mobile/jest.setup.js, mobile/lib/__tests__/smoke.test.ts
```

En ningún punto del log hay evidencia de que se haya invocado un proceso real `npm`/`npx` dentro del workspace — el agente "Node" es una llamada a un modelo de lenguaje que devuelve bloques `FILE_BEGIN/END` con contenido de archivo completo, nada más. Cuando el `requerimiento` le pide "correr `npx expo install jest-expo --check`", el modelo no tiene ninguna herramienta para ejecutar ese comando de verdad — en su lugar, **alucina cuál sería el contenido resultante de `package.json`** basándose en su conocimiento de entrenamiento sobre "un proyecto Expo SDK 54 típico con jest-expo", produciendo un árbol de dependencias plausible pero completamente desconectado de las versiones reales que este proyecto específico ya tenía instaladas y funcionando.

Esto es consistente con el patrón observado: los números de versión que aparecen (`expo-router ~4.0.19`, `react 18.3.1`, `react-native 0.76.7`) corresponden a una generación de Expo/React Native más vieja que la ya presente en el proyecto (`expo-router ~6.0.23`, `react ^19.1.0`, `react-native ^0.81.5`) — probablemente porque los datos de entrenamiento del modelo tienen más peso o ejemplos de esa combinación de versiones más antigua que de la actual, y el modelo "completó" el package.json hacia ese patrón más familiar en vez de preservar exactamente lo que ya había, pese a que el `codebase_reader` sí capturó `package.json` como uno de los archivos existentes leídos (confirmado en el log: "9 archivos existentes capturados: ... package.json ...").

## Segundo problema relacionado — cobertura de `validation_gate` es nula para tickets de `mobile/`

El job pasó `validation_gate` con:

```
TYPESCRIPT (tsc --noEmit): ✓ sin errores de tipos
VITEST (npx vitest run): ✓ Test Files 66 passed | 1 skipped (67) Tests 556 ...
```

Estos son el `tsc`/`vitest` del proyecto **raíz** (Next.js) — que nunca incluye `mobile/` en su alcance (`mobile/` tiene su propio `package.json`, su propio `tsconfig.json`, y ningún test runner corre sobre él desde el proyecto raíz). Es decir: para un ticket cuyo alcance es 100% `mobile/`, `validation_gate` da una señal de "PASÓ ✓" que no significa absolutamente nada sobre la validez real del cambio — ni siquiera detecta que `package.json` quedó con un árbol de dependencias que ni compilaría (por el lockfile desincronizado). Esto no es un bug nuevo en sí, pero es la razón por la que este ticket llegó a "Aprobado+validado" pese a estar roto: no hay ningún chequeo real que hubiera podido atraparlo.

## Investigación sugerida

1. **Prioridad alta:** cuando un `requerimiento` (o una subtarea del `planner`) pide explícitamente ejecutar un comando de instalación/resolución de paquetes (`npm install`, `npx expo install --check`, `npx <algo> install`, etc.), `dynamic_developer` debería:
   - o bien tener una herramienta real para ejecutar ese comando en el workspace (dentro del contenedor, con red disponible — ya se confirmó en este mismo ticket que el contenedor SÍ tiene acceso de red, dado que `npm install` del proyecto raíz corrió bien en `git_setup`) y usar su salida real como la única fuente de verdad para el diff de `package.json`/lockfile,
   - o bien, si esa capacidad no existe today, el prompt del agente debería prohibir explícitamente generar contenido de `package.json` a partir de "lo que el modelo cree que sería el resultado" de un comando de instalación — y en su lugar, limitar los cambios a `package.json` a ediciones puntuales dentro del `devDependencies`/`scripts` ya mencionados en el `requerimiento`, dejando cualquier versión no mencionada explícitamente intacta.
2. **Prioridad alta — guarda de protección para package.json/lockfile:** agregar una guarda específica (además de las guardas de reescritura/alcance ya existentes) que compare, campo por campo, las dependencias de un `package.json` modificado contra el original, y rechace (modo "hard") cualquier cambio de versión en una dependencia que el `requerimiento` no haya mencionado explícitamente por nombre — similar en espíritu a `_find_explicitly_forbidden_files`, pero para el contenido semántico de manifests de paquetes en vez de rutas de archivo.
3. **Prioridad media:** cuando el lockfile correspondiente (`package-lock.json`, `yarn.lock`, etc.) no cambia pero `package.json` sí cambió versiones de dependencias ya instaladas, marcarlo como una señal de alerta explícita en el log — es una fuerte indicación de que ningún proceso de instalación real ocurrió.
4. **Prioridad media:** `validation_gate` debería detectar cuándo el 100% de los archivos de un ticket están bajo un subdirectorio con su propio `package.json`/`tsconfig.json` (como `mobile/`) y, si existe un script de test/typecheck en ESE `package.json`, correrlo en vez de (o además de) el `tsc`/`vitest` del proyecto raíz — de lo contrario la validación no tiene ninguna cobertura real sobre ese código.

## Criterios de aceptación

1. Un `requerimiento` que pide agregar 1-2 devDependencies específicas a un `package.json` existente no debe resultar en cambios de versión de ninguna dependencia no mencionada por nombre, ni en la eliminación de dependencias existentes no mencionadas.
2. Test de regresión que reproduzca el caso exacto: un `package.json` con dependencias en versiones conocidas + un requerimiento pidiendo agregar 1 devDependency nueva → el diff final no debe tocar el valor de ninguna versión preexistente ni remover ninguna entrada preexistente.
3. Si `dynamic_developer` no tiene (o no va a tener en el corto plazo) una herramienta real de ejecución de comandos de instalación, esto debería quedar documentado explícitamente como una limitación conocida del sistema (para que quien despache sepa que debe evitar pedir "corre npm install X" y en su lugar especificar exactamente qué línea agregar a `package.json`, como workaround).

## Nota sobre severidad

Alta: a diferencia de casi todos los hallazgos anteriores (que rompían tests o el propio ticket), este tipo de regresión — si se hubiera mergeado sin revisión manual — habría dejado el proyecto `mobile/` con un `package.json` describiendo versiones de React/Expo Router/React Native dos generaciones más viejas que las realmente instaladas y usadas en el código existente, con el lockfile desincronizado, probablemente rompiendo `npm install` o produciendo un `node_modules` completamente incompatible con el código real del proyecto en el próximo checkout limpio. Ninguna de las guardas actuales (anti-truncamiento, exclusión explícita, fuera de alcance) lo detecta, porque el archivo SÍ estaba en el alcance permitido del ticket — el problema es el *contenido* semántico del cambio, no el archivo en sí. No se mergeó nada. El resto del plan `mobile-cleanup` (Plan 7) queda pausado hasta que esto se evalúe — no se puede confiar en que `dynamic_developer` maneje correctamente ningún paso de instalación de dependencias hasta que se corrija.
