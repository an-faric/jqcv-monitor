# Evidencia del acceso y validaciones pendientes

## GitHub Actions: ejecución 39151806527

Ejecución [#8](https://github.com/an-faric/jqcv-monitor/actions/runs/39151806527), commit `bf7cdcc`, rama `codex/jqcv-c1-monitor`. Job [117746811313](https://github.com/an-faric/jqcv-monitor/actions/runs/39151806527/job/117746811313).

El 18 de octubre de 2026, entre las 19:55:50 y las 19:56:16 de Europe/Madrid (17:55:50–17:56:16 UTC), el runner alojado `ubuntu-latest` ejecutó el diagnóstico nuevo. Checkout, instalación de Python 3.13, dependencias y las 65 pruebas de ese commit terminaron correctamente. El diagnóstico terminó con error y la vista previa quedó omitida; no se ejecutaron SMTP ni persistencia.

Las anotaciones públicas del job muestran:

| Fase/cliente | Observación |
| --- | --- |
| DNS/IPv4 | El diagnóstico obtuvo `193.145.200.66` y la usó en la prueba TCP. No se fija esa dirección en el código. |
| TCP directo | Puerto 443: `TimeoutError` tras 4.004 ms. |
| curl IPv4 | Código de salida 28; 8.008 ms aproximadamente. `http_status=0`, cero bytes, cero redirecciones, sin IP remota conectada. |
| Tiempos de curl | DNS 0,000457 s; TCP, TLS y primer byte 0 s. No llegó a negociar TLS ni a recibir HTTP. |
| Python Requests | `ConnectTimeout`, sin código HTTP, tras 10.369 ms aproximadamente. |

Las líneas completas de DNS/AAAA y los pasos omitidos de TLS están en los logs y en el resumen saneado del diagnóstico. La vista pública permitió recuperar las anotaciones de fallo, pero exige autenticación para los logs completos. No se afirma a partir de esa vista si existe una dirección IPv6. `ssl_verify_result=0` sin conexión en curl no constituye una validación de certificado.

Conclusión confirmada: en esa ejecución no se estableció una conexión TCP utilizable con el origen mediante las rutas probadas. No se obtuvo HTML y no se ejecutó el parser contra contenido real. Estos datos no identifican quién descarta o impide la conexión ni demuestran geobloqueo, filtrado de Azure/bots o un problema de certificados.

## Historial observado

La [ejecución #9, 39152065947](https://github.com/an-faric/jqcv-monitor/actions/runs/39152065947), commit `967258d`, terminó correctamente en GitHub Actions con Python 3.13 y la suite ampliada de 68 pruebas. Su job [117747584964](https://github.com/an-faric/jqcv-monitor/actions/runs/39152065947/job/117747584964) confirma instalación y pruebas correctas. Fue una validación del código: diagnóstico de red, vista previa y correo quedaron omitidos, igual que el job de persistencia. Ese resultado correcto no acredita acceso a la JQCV.

Los metadatos públicos de las ejecuciones #2–#5 muestran fallos del monitor en Ubuntu; [#6](https://github.com/an-faric/jqcv-monitor/actions/runs/39052849863) y [#7](https://github.com/an-faric/jqcv-monitor/actions/runs/39127671837) también fallaron tras cambiar a macOS. El texto detallado de esos errores lo aportó el usuario; la revisión pública confirmó las etapas fallidas, sin poder releer los logs completos. La ejecución #1 correcta no permite conocer públicamente su modo y no se usa como evidencia de acceso a la web.

## Codex: resultado distinto

Desde Codex, el DNS directo falló y la petición por el proxy del entorno recibió HTTP 503 de Envoy después de unos 6–7 segundos, con indicación de timeout de conexión aguas arriba. Esa respuesta es de la infraestructura del entorno. No equivale a una respuesta de la JQCV y es distinta del timeout TCP observado en Actions.

No se pudo inspeccionar HTML real ni verificar una fuente oficial alternativa relacionada con la sección de resultados. Por ello no se ha adoptado una API, RSS, intermediario ni otra URL como sustitución.

## Qué sigue pendiente

- Obtener HTML actual utilizable desde el runner alojado y confirmar el bloque/elementos de resultados mediante `preview`.
- Si el diagnóstico cambia tras una modificación real de red o una fuente oficial comprobada, aplicar la corrección que justifique esa evidencia. Cambiar el selector o elevar timeouts no solventa este fallo TCP.
- Verificar `test-email` con los secretos existentes de Actions, de forma separada.
- Ejecutar dos `check` sin cambios y verificar la rama `jqcv-monitor-state`. Las pruebas con repositorios Git temporales ya cubren persistencia, ausencia de commits repetidos y rechazo de cambios concurrentes, pero no equivalen a esas ejecuciones reales.

Los cambios se han preparado en una rama de desarrollo; no se han integrado en `main` ni activado en la programación de producción. Crear un PR mediante `gh` devolvió `Forbidden` en la API GraphQL. La rama está disponible en GitHub y se puede abrir el PR desde su interfaz, sin proporcionar tokens en el chat.
