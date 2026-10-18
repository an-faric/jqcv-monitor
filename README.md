# Monitor remoto de resultados C1 de la JQCV

Este repositorio vigila publicaciones oficiales del nivel **C1** en la convocatoria **2026** y envía avisos por correo mediante GitHub Actions. Toda la operación se ejecuta en runners alojados por GitHub: no requiere instalar Python, Git, runners ni tareas programadas en un ordenador personal, y el ordenador puede permanecer apagado.

La única fuente incorporada es la página oficial:

<https://jqcv.gva.es/es/resultats>

No se ha verificado una API, un RSS ni otra página oficial equivalente. El monitor no usa buscadores, copias antiguas, proxies públicos ni aplicaciones de consulta de notas.

## Estado de validación

La implementación y sus pruebas automatizadas están preparadas, pero el monitor **todavía no puede considerarse operativo** porque falta demostrar que un runner alojado por GitHub obtiene HTML actual utilizable y que el selector delimita el bloque real.

Hechos comprobados el 18 de octubre de 2026:

- Las ejecuciones #2 a #5 fallaron en el paso de consulta con `ubuntu-latest`.
- Cambiar únicamente a `macos-latest` no ayudó: también fallaron las ejecuciones [#6](https://github.com/an-faric/jqcv-monitor/actions/runs/39052849863) y [#7](https://github.com/an-faric/jqcv-monitor/actions/runs/39127671837).
- El error aportado por los registros fue un `ConnectTimeout`; los metadatos públicos confirman que el mismo paso falla, aunque GitHub exige sesión para volver a leer su texto completo.
- Desde Codex, el proxy de ese entorno respondió `HTTP 503` de Envoy tras unos 6–7 segundos. Esa respuesta no procede del HTML de la JQCV y no demuestra qué sucede desde Actions.
- La ejecución programada #7, prevista para las 08:00 UTC, comenzó aproximadamente 4 horas y 32 minutos tarde. Los cron de Actions no garantizan puntualidad.
- El selector histórico `ul.list-group.show-quick-actions-on-hover` y el selector de elementos `:scope > li` solo están cubiertos por fixtures sintéticas. No se presentan como validados contra la página real.

No hay evidencia suficiente para atribuir el fallo a geobloqueo, Azure, filtrado de bots, IPv6 o certificados. El modo `diagnose` permite separar esas fases en el runner real.

La nueva [ejecución #8](https://github.com/an-faric/jqcv-monitor/actions/runs/39151806527) sí ejecutó el diagnóstico desde Ubuntu alojado: pasaron las 65 pruebas de ese commit, pero TCP y curl IPv4 agotaron la espera y Python terminó con `ConnectTimeout`, sin respuesta HTTP. La evidencia y los límites de esa conclusión están en [DIAGNOSTICO.md](DIAGNOSTICO.md). El acceso sigue sin resolverse y no se ha validado el selector.

## Qué detecta

La configuración inicial es `TARGET_LEVEL=C1` y `TARGET_YEAR=2026`. El año es explícito: no cambia automáticamente al llegar enero, porque una convocatoria puede publicar resultados durante el año siguiente.

Dentro del bloque configurado se buscan publicaciones que indiquen el nivel y la convocatoria. El estado conserva, cuando la fuente los ofrece:

- título y URL oficial;
- nivel y convocatoria;
- fase parcial, provisional, final o definitiva;
- fecha de publicación indicada por una etiqueta temporal válida;
- fecha o plazo anunciado, conservando el texto oficial;
- enlaces significativos, incluidas versiones de documentos.

Se distinguen dos tipos de evento:

- **Resultados disponibles:** publicación nueva, enlace nuevo, cambio relevante o transición de fase.
- **Anuncio de fecha:** frase oficial que relaciona expresamente resultados o calificaciones con una fecha o plazo futuro.

El código reconoce variantes habituales en castellano y valenciano. Una mención genérica al C1, una convocatoria distinta o una entrada exclusiva de otro nivel no genera aviso. Una publicación de varios niveles sí se acepta si incluye expresamente C1.

El alcance actual se limita a la fuente principal y al bloque seleccionado. No sigue artículos enlazados ni PDF y, por tanto, solo detecta un anuncio si aparece en ese bloque. No existe una fecha oficial confirmada incorporada al código.

## Modos manuales de GitHub Actions

En **Actions → Comprobar resultados C1 JQCV → Run workflow** están disponibles:

1. `diagnose`: comprueba red sin correo ni estado.
2. `preview`: muestra las publicaciones extraídas sin correo ni estado.
3. `test-email`: prueba únicamente SMTP y etiqueta el mensaje como prueba.
4. `check`: consulta, compara, avisa y persiste. Solo se admite en la rama predeterminada.

El workflow ejecuta primero la suite de pruebas. `diagnose`, `preview` y `test-email` tienen permiso de lectura; solo el job `check` recibe `contents: write`. Los secretos SMTP se entregan exclusivamente a los pasos que envían correo.

Al crear la rama de desarrollo `codex/jqcv-c1-monitor` se ejecutan automáticamente las pruebas, `diagnose` y, solo si este pasa, `preview`. Los siguientes cambios de código ejecutan las pruebas; el diagnóstico también está disponible manualmente. No se envía correo ni se persiste estado. Esto permite validar la implementación antes de incorporarla a la rama predeterminada.

### Secuencia mínima de activación

1. Ejecuta `diagnose` en un runner alojado por GitHub.
2. Si termina correctamente, ejecuta `preview` y confirma en el registro que aparecen únicamente entradas pertinentes del C1 de 2026.
3. Ejecuta `test-email` y confirma la recepción del mensaje marcado `[PRUEBA]`.
4. Ejecuta `check` en la rama predeterminada. El primer correo es de inicialización y describe la situación ya existente; no anuncia una novedad.
5. Ejecuta de nuevo `check`. Sin cambios debe mostrar `Sin cambios relevantes.`, no enviar correo y no crear un commit.

No ejecutes `check` hasta validar `preview`: un HTTP 200 no demuestra por sí solo que el selector o la extracción sean correctos.

## Interpretación de `diagnose`

El diagnóstico emite líneas JSON prefijadas con `DIAG` y tiene límites estrictos. No imprime cuerpos, cookies, cabeceras completas, secretos ni valores de proxy.

- `dns`: direcciones A y AAAA disponibles. La ausencia comprobada de una familia se marca `not_applicable`; un fallo global de DNS deja esa disponibilidad como desconocida.
- `tcp`: conexión directa a cada familia disponible, con máximo de dos direcciones.
- `tls`: negociación con SNI y validación normal del certificado; nunca usa `verify=False` ni `curl -k`.
- `http_curl`: una solicitud limitada por familia aplicable, redirecciones y tiempos.
- `http_python`: la ruta real usada por el monitor, sin reintentos en el diagnóstico.
- `summary`: solo es correcta si Python obtiene HTTPS 200, HTML no vacío y permanece en el host oficial.

`preview` sigue siendo obligatorio después de un diagnóstico correcto. La comparación entre las fases permite decidir si existe evidencia para preferir IPv4 u otro cambio; el workflow no fuerza una familia sin esa evidencia.

El diagnóstico tiene un límite global de 180 segundos en Actions y muestra un resumen saneado en la ejecución. Si falla DNS directo, compara también `curl` y Python por la ruta del entorno, porque un proxy puede resolver el nombre por separado.

## Secretos y variables en GitHub

Conserva estos secretos en **Settings → Secrets and variables → Actions → Secrets**:

| Secreto | Uso |
| --- | --- |
| `EMAIL_USER` | Dirección única de la cuenta remitente |
| `EMAIL_PASSWORD` | Credencial SMTP o contraseña de aplicación |
| `EMAIL_TO` | Dirección única que recibe los avisos y pruebas |

No escribas esos valores en archivos, logs o incidencias. El `GITHUB_TOKEN` lo proporciona GitHub Actions; el workflow no requiere un token personal.

Las variables de Actions son opcionales:

| Variable | Predeterminado | Uso |
| --- | --- | --- |
| `TARGET_LEVEL` | `C1` | Nivel vigilado |
| `TARGET_YEAR` | `2026` | Convocatoria; se cambia manualmente cuando corresponda |
| `RESULTS_URL` | URL oficial indicada arriba | Fuente HTTPS |
| `RESULTS_SELECTOR` | `ul.list-group.show-quick-actions-on-hover` | Único bloque de resultados |
| `RESULTS_ITEM_SELECTOR` | `:scope > li` | Entradas dentro del bloque |
| `STATE_BRANCH` | `jqcv-monitor-state` | Rama separada para la referencia persistente |
| `SMTP_HOST` | `smtp.gmail.com` | Servidor SMTP |
| `SMTP_PORT` | `587` | Puerto SMTP |
| `SMTP_SECURITY` | `starttls` | `starttls` o `ssl` |

Para Gmail, `EMAIL_PASSWORD` debe ser una contraseña de aplicación compatible con la política de la cuenta. Con `SMTP_SECURITY=ssl` configura también `SMTP_PORT=465`. En ambos casos se valida el certificado TLS.

## Estado persistente y avisos

`last_content.txt` contiene JSON versionado por combinación de nivel y convocatoria. Aunque conserva el nombre histórico, ya no guarda HTML ni el texto completo del contenedor. Las publicaciones conocidas permanecen en el estado aunque desaparezcan temporalmente, evitando avisos por reordenación o reaparición.

Si existe un snapshot legado con secciones `TEXTO` y `ENLACES`, solo se migra automáticamente cuando coincide con el snapshot histórico que produce el HTML actual. Se conservan el snapshot original y su huella SHA-256 dentro del JSON v2. Si difiere, la ejecución falla y mantiene la referencia para revisar las diferencias en GitHub: no se puede saber si corresponden a una novedad o al cambio de formato. Un archivo vacío, desconocido o corrupto provoca un fallo visible y nunca se reinicializa silenciosamente.

El orden de una comprobación con cambios es:

1. recuperar `last_content.txt` de la rama de estado, conservando la referencia del checkout si esa rama aún no existe;
2. descargar, validar y comparar publicaciones relevantes;
3. enviar el correo;
4. reemplazar el estado de forma atómica;
5. hacer commit únicamente del estado en su rama separada.

Si falla la red, el selector, la validación o SMTP, el estado anterior se conserva. Si SMTP acepta el correo y después falla el commit o el push, una ejecución posterior puede repetir el aviso. SMTP y Git no ofrecen una transacción común, por lo que no se promete entrega exactamente una vez.

El workflow persiste mediante el `GITHUB_TOKEN` en `jqcv-monitor-state`, configurable mediante `STATE_BRANCH`. `state_store.py` utiliza un índice Git temporal: no cambia el checkout ni el historial de la rama de código, no fuerza pushes y no genera commits si el estado es idéntico. Esto permite mantener las protecciones de la rama predeterminada. Si las reglas aplican también a la rama de estado e impiden publicar al bot, la ejecución fallará de forma visible. No se modifica la visibilidad ni se debilitan reglas. El acceso disponible durante esta revisión no permitió inspeccionar esas reglas.

## Correos

Los asuntos distinguen:

- inicialización del monitor;
- resultados disponibles o transición de fase;
- anuncio oficial de fecha o plazo;
- mensaje de prueba.

El cuerpo incluye motivo, título, convocatoria, fase conocida, enlace oficial y las fechas que la fuente indique. La hora de detección aparece en `Europe/Madrid` y también en UTC. Nunca se presenta la hora de detección como fecha oficial, ni se consulta una nota personal o se afirma un aprobado.

## Programación, coste y continuidad

La programación se mantiene a las `08:00 UTC` una vez al día mientras el acceso real siga sin validar. El repositorio era público durante la revisión y los runners estándar alojados por GitHub son gratuitos para repositorios públicos según la documentación vigente; no se cambia su visibilidad.

Una frecuencia de 15 minutos (`7,22,37,52 * * * *`) solo debe activarse después de demostrar que `diagnose`, `preview` y dos `check` consecutivos funcionan. En repositorios privados se aplican las cuotas del plan. GitHub puede retrasar o descartar ejecuciones programadas; además, puede desactivar workflows programados en repositorios públicos tras 60 días sin actividad. El sistema no promete avisos instantáneos.

## Pruebas y límites de la evidencia

Las pruebas cubren filtrado por nivel/año, fases, anuncios de fecha, cambios de texto, enlaces y versiones, normalización, deduplicación, ausencia de cambios, migración, protección del estado, fallos HTTP/TLS/timeouts, SMTP y diagnóstico sin efectos secundarios. La persistencia también se prueba con repositorios Git temporales: creación y lectura de la rama de estado, segunda ejecución sin commit y conservación de la rama de código.

Todos los HTML de `tests/fixtures` son sintéticos. Las pruebas con mocks demuestran comportamiento controlado del código, pero no acceso actual a la JQCV. Para considerar operativo el sistema faltan un `diagnose` satisfactorio, `preview`, `test-email`, persistencia y dos comprobaciones sin cambios en Actions.

## Referencias oficiales

- Resultados JQCV: <https://jqcv.gva.es/es/resultats>
- Eventos programados de GitHub Actions: <https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule>
- Facturación de GitHub Actions: <https://docs.github.com/en/billing/concepts/product-billing/github-actions>
- Contraseñas de aplicación de Google: <https://support.google.com/accounts/answer/185833?hl=es>
- SMTP de Google: <https://knowledge.workspace.google.com/admin/gmail/send-email-from-a-printer-scanner-or-app?hl=es>
