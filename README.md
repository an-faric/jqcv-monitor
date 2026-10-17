# Monitor de resultados de la JQCV

Monitor gratuito con Python y GitHub Actions para detectar cambios en el texto
y los enlaces de la lista pública de https://jqcv.gva.es/es/resultats.
Envía el aviso por SMTP y conserva la nueva referencia mediante un commit.

## Qué hace

- Consulta únicamente la página pública indicada; compara el bloque seleccionado.
- Usa `ul.list-group.show-quick-actions-on-hover` como selector inicial.
- Compara texto normalizado y enlaces, sin guardar todo el HTML.
- Ignora espacios, tokens conocidos de sesión/autenticación y parámetros `utm_`.
  Conserva parámetros como `version`, que pueden señalar un documento nuevo.
- Primera ejecución: envía un correo de activación con la lista inicial y crea
  `last_content.txt`. Ese correo no significa que haya aparecido una nota nueva.
- Si no cambia nada, termina sin correo, sin reescribir el estado y sin commit.
- Si cambia algo, envía un correo con las diferencias, guarda el nuevo estado
  y el workflow hace `git commit` y `git push`.
- Si falla la web, el selector o el correo, termina con error y conserva el
  estado anterior para volver a intentarlo en una ejecución posterior.

**Alcance:** detecta publicaciones en esta lista pública. No inicia sesión en
una consulta de calificaciones ni sabe cuál es tu nota. Si cambian el contenido
de un PDF o una aplicación enlazada sin cambiar la lista o su URL, no lo detectará.

**Comprobación de la página:** el 17/10/2026 la URL no devolvió HTML utilizable
desde el entorno de preparación (errores de acceso/HTTP 503). Por tanto, el
selector propuesto no se presenta como confirmado contra el HTML actual.
El modo `preview` permite comprobarlo desde GitHub y `RESULTS_SELECTOR` permite
ajustarlo sin editar Python. Si no encuentra exactamente un bloque válido,
el programa falla y conserva el estado; nunca sustituye la lista por toda la página.

## Estructura

```text
jqcv-monitor/
├── check_results.py
├── requirements.txt
├── README.md
├── .gitignore
├── .github/
│   └── workflows/
│       └── check.yml
└── tests/
    ├── test_check_results.py
    └── fixtures/
        └── results.html
```

`last_content.txt` se genera en la raíz tras la primera ejecución correcta.
No lo crees vacío y no lo añadas a `.gitignore`: debe permanecer en Git.
El HTML de las pruebas es un ejemplo sintético, no una captura de la web real.

## 1. Crear el repositorio

1. En GitHub, crea un repositorio dedicado, por ejemplo `jqcv-monitor`.
2. Elige **Public** si quieres usar gratuitamente los runners estándar sin
   consumir la cuota de minutos de un repositorio privado.
3. Sube el contenido de esta carpeta a la raíz de la rama predeterminada
   (normalmente `main`). Incluye `.github/workflows/check.yml`.
4. No subas contraseñas ni archivos `.env`.

También puedes subirlo desde tu equipo con Git. Sustituye `TU_USUARIO`:

```bash
git init -b main
git add .
git commit -m "Añadir monitor de resultados JQCV"
git remote add origin https://github.com/TU_USUARIO/jqcv-monitor.git
git push -u origin main
```

Los comandos presuponen un repositorio remoto recién creado y vacío. No se ha
creado ni publicado un repositorio en tu cuenta durante la preparación del código.

## 2. Preparar Gmail

La configuración incluida usa Gmail con TLS, `smtp.gmail.com:587`.
El destinatario puede pertenecer a cualquier proveedor.

1. En la cuenta que enviará los avisos, activa la verificación en dos pasos.
2. Abre https://myaccount.google.com/apppasswords.
3. Crea una contraseña de aplicación, por ejemplo con el nombre `JQCV GitHub`.
4. Copia esa contraseña de aplicación de 16 caracteres **sin espacios**.
   Usa esa contraseña en `EMAIL_PASSWORD`, no la contraseña habitual de Google.

Si no aparece la opción, consulta las restricciones de tu cuenta. Algunas
cuentas educativas o de empresa no permiten contraseñas de aplicación; en ese
caso puedes utilizar una cuenta personal que sí las permita. Si cambias la
contraseña de Google, puede ser necesario generar otra contraseña de aplicación.

## 3. Crear los secretos

Ve al repositorio → **Settings → Secrets and variables → Actions → Secrets →
New repository secret**. Crea exactamente estos tres secretos:

| Nombre | Valor |
| --- | --- |
| `EMAIL_USER` | Dirección completa de la cuenta remitente, por ejemplo `tu_cuenta@gmail.com` |
| `EMAIL_PASSWORD` | Contraseña de aplicación de esa cuenta, sin espacios en el caso de Gmail |
| `EMAIL_TO` | Una dirección de correo a la que quieras recibir los avisos |

`EMAIL_USER` y `EMAIL_TO` deben contener una única dirección, sin nombre visible
ni listas de destinatarios. Pueden tener el mismo valor.

No necesitas crear un secreto `GITHUB_TOKEN`: GitHub lo proporciona al workflow.
El YAML ya declara `permissions: contents: write` para guardar el estado.

## 4. Permitir los commits automáticos

En **Settings → Actions → General**, comprueba que GitHub Actions está habilitado
y que la política de permisos permite escritura de contenido. Si corresponde
a tu configuración, selecciona **Workflow permissions → Read and write permissions**.

Usa un repositorio dedicado que permita al token del workflow hacer commits
directos a la rama predeterminada. Las reglas que exijan PR o bloqueen al bot
pueden impedir el `push`; no hace falta un token personal para esta solución.

El workflow solo se inicia por horario o por ejecución manual, y solo funciona
en la rama predeterminada. Sus propios commits no vuelven a iniciar el monitor.
La concurrencia impide que dos ejecuciones de este monitor procesen a la vez
el mismo estado. El guardado solo incluye `last_content.txt`, nunca `git add .`.

## 5. Probar y activar

En **Actions → Comprobar resultados JQCV → Run workflow**, selecciona la rama
predeterminada y ejecuta estas opciones:

1. **`test-email`**: comprueba SMTP y la recepción del correo sin consultar la web
   ni crear estado. Comprueba también la carpeta de spam.
2. **`preview`**: descarga la página y muestra en el registro el texto y los
   enlaces que se vigilarán. No envía correo ni guarda estado. Comprueba que
   corresponde a la lista de resultados que te interesa.
3. **`check`**: activa el monitor. En la primera ejecución correcta recibirás
   el correo de activación y aparecerá un commit con `last_content.txt`.
4. Ejecuta otra vez **`check`**: si no hay cambios, verás `Sin cambios.` y no
   recibirás otro correo ni se generará otro commit.

Después se ejecutará cada día según el `cron`. No tienes que dejar tu
ordenador encendido. No hay un servicio de pago adicional ni una base de datos.

### Si el selector no encuentra la lista

1. Abre https://jqcv.gva.es/es/resultats en tu navegador.
2. Inspecciona el contenedor que agrupa las convocatorias y resultados.
3. Usa un selector CSS que identifique **exactamente ese contenedor**.
4. En **Settings → Secrets and variables → Actions → Variables**, crea una
   variable de repositorio `RESULTS_SELECTOR` con ese selector.
5. Repite `preview` hasta ver la lista correcta y luego ejecuta `check`.

Ejemplo del selector inicial: `ul.list-group.show-quick-actions-on-hover`.
Si encuentra varios bloques, añade un antecesor específico de la sección
identificado en el HTML real. No uses `body` o `html` como sustitutos: incluirían
menús, avisos y pie de página. `requests` no ejecuta JavaScript; si el portal
traslada la lista a una carga dinámica, habrá que adaptar la petición a su
fuente pública de datos, en lugar de guardar una página vacía.

## Horario y rapidez

El YAML incluye:

```yaml
schedule:
  - cron: '0 8 * * *'
```

Equivale a las **08:00 UTC**, es decir, las 09:00 en invierno o las 10:00 en
verano en la España peninsular. Una comprobación diaria puede tardar hasta
unas 24 horas en detectar un cambio que permanezca publicado, más el posible
retraso de ejecución de GitHub. No es un aviso instantáneo.

Para revisar cada 15 minutos, sustituye la línea `cron` por:

```yaml
  - cron: '7,22,37,52 * * * *'
```

Son los minutos 7, 22, 37 y 52 de cada hora, evitando el comienzo de la hora,
que suele tener más carga. Tampoco garantiza un plazo exacto: GitHub puede
retrasar o descartar ejecuciones programadas. Un cambio que aparezca y
desaparezca entre dos consultas puede pasar inadvertido.

## Coste y continuidad

Los runners estándar de GitHub Actions son gratuitos en repositorios públicos.
Este workflow usa `ubuntu-latest` y no sube artefactos ni activa cachés.
Los repositorios privados consumen su cuota incluida de minutos; una revisión
cada 15 minutos puede agotarla, por lo que la opción pública es la adecuada
para mantener este planteamiento gratuito.

En repositorios públicos, GitHub desactiva los workflows programados tras
60 días sin actividad del repositorio. Las revisiones que no detectan cambios
no hacen commits: revisa periódicamente que siga habilitado. Si se desactiva,
vuelve a habilitarlo en Actions y ejecuta `check` manualmente.

## Configuración SMTP opcional

En **Actions → Variables** puedes crear estas variables de repositorio:

| Variable | Valor predeterminado | Uso |
| --- | --- | --- |
| `SMTP_HOST` | `smtp.gmail.com` | Servidor de un proveedor compatible con autenticación por contraseña de aplicación |
| `SMTP_PORT` | `587` | Puerto SMTP |
| `SMTP_SECURITY` | `starttls` | `starttls` para 587 o `ssl` para TLS implícito en 465 |
| `RESULTS_SELECTOR` | `ul.list-group.show-quick-actions-on-hover` | Selector único del bloque de resultados |

Para TLS implícito, configura **ambas** variables `SMTP_SECURITY=ssl` y
`SMTP_PORT=465` en GitHub, ya que el YAML establece 587 por defecto.
El código exige TLS y verifica certificados. SMTP básico no sirve con
proveedores que exijan exclusivamente OAuth; el caso preparado es Gmail con
contraseña de aplicación.

## Ejecución local y dependencias

Necesitas Python 3.11 o posterior. GitHub Actions configura Python 3.13.
Las únicas dependencias externas directas son:

```text
requests>=2.32.5,<3
beautifulsoup4>=4.13.4,<5
```

`smtplib`, `ssl`, `email`, `difflib` y las demás utilidades son de la biblioteca
estándar; no se instalan por separado. `urllib3` llega como dependencia de
`requests`.

En Linux/macOS, desde la raíz del proyecto:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python check_results.py --dry-run
python -m unittest discover -s tests -v
```

En Windows puedes usar `.venv\Scripts\python.exe` en lugar de `python` tras
crear el entorno. `--dry-run` y las pruebas automatizadas no requieren secretos.

Para enviar correo localmente, proporciona las mismas variables de entorno
que en GitHub. Ejemplo Bash, sin escribir la contraseña en el comando:

```bash
export EMAIL_USER='tu_cuenta@gmail.com'
export EMAIL_TO='tu_destino@example.com'
read -rsp 'Contraseña de aplicación: ' EMAIL_PASSWORD
export EMAIL_PASSWORD
python check_results.py --test-email
python check_results.py
unset EMAIL_PASSWORD
```

La ejecución local guarda el estado localmente; los commits automáticos son
responsabilidad del workflow de GitHub.

## Errores, reintentos y limitaciones de entrega

- **HTTP 403/429/5xx o timeout:** consulta el registro de Actions. Los errores
  temporales previstos tienen hasta tres reintentos adicionales; después el
  job falla sin modificar el estado. No se desactiva la verificación TLS.
- **0 o varios contenedores:** revisa `RESULTS_SELECTOR` y repite `preview`.
- **Autenticación SMTP:** revisa la contraseña de aplicación y los secretos.
  El programa no imprime la contraseña ni las respuestas SMTP completas.
- **Fallo del commit o push:** revisa permisos y reglas de rama. El workflow
  intenta incorporar cambios remotos con `git pull --rebase` y reintenta
  el push hasta tres veces, sin forzar la historia ni resolver conflictos
  automáticamente.
- **Estado vacío:** elimínalo y ejecuta `check` para reinicializar; no lo crees
  manualmente. Eliminar un estado válido reinicia la referencia y genera un
  nuevo correo de activación, perdiendo la comparación con la versión anterior.

SMTP y Git no comparten una transacción. Si el servidor acepta el correo pero
el runner se interrumpe o falla el push, la siguiente ejecución puede repetir
el aviso. Se prioriza poder volver a avisar frente a marcar como comunicado
un cambio cuyo correo falló. La aceptación SMTP tampoco garantiza que el
destinatario lo vea en la bandeja de entrada.

Las pruebas incluidas validan cambios de texto y enlaces, aislamiento del
bloque, normalización, primer arranque, ausencia de cambios, conservación del
estado ante errores, vista previa y configuración TLS de SMTP. No envían
correos reales ni acreditan acceso a la página desde los runners de GitHub.

## Referencias oficiales

- JQCV: https://jqcv.gva.es/es/resultats
- Horarios, posibles retrasos e inactividad de 60 días:
  https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule
- Gratuidad y cuotas de Actions:
  https://docs.github.com/en/billing/concepts/product-billing/github-actions
- Contraseñas de aplicación de Google:
  https://support.google.com/accounts/answer/185833?hl=es
- Envío por SMTP de Google:
  https://knowledge.workspace.google.com/admin/gmail/send-email-from-a-printer-scanner-or-app?hl=es
- Acciones oficiales usadas:
  https://github.com/actions/checkout y https://github.com/actions/setup-python
