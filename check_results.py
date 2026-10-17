#!/usr/bin/env python3
"""Vigila la lista pública de resultados de la JQCV y avisa por SMTP."""

import argparse
import difflib
import os
from pathlib import Path
import re
import smtplib
import ssl
import sys
import tempfile
import unicodedata
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid, parseaddr
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


URL = "https://jqcv.gva.es/es/resultats"
DEFAULT_SELECTOR = "ul.list-group.show-quick-actions-on-hover"
STATE_FILE = Path(__file__).resolve().with_name("last_content.txt")


class MonitorError(Exception):
    """Error que impide confirmar una comprobación o enviar el aviso."""


def normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split())


def canonical_url(href: str) -> str:
    """Conserva los identificadores de documentos y elimina tokens volátiles."""
    parts = urlsplit(urljoin(URL, href.strip()))
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        return ""
    path = re.sub(r";jsessionid=[^/;?#]*", "", parts.path, flags=re.I)
    ignored = {"p_auth", "p_p_auth", "jsessionid", "_"}
    query = sorted(
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in ignored and not key.lower().startswith("utm_")
    )
    return urlunsplit((
        parts.scheme.lower(), parts.netloc.lower(), path,
        urlencode(query), parts.fragment,
    ))


def fetch_html() -> bytes:
    retries = Retry(
        total=3, backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods={"GET"}, respect_retry_after_header=False,
        # Permite informar del último HTTP 429/5xx, en vez de ocultarlo en RetryError.
        raise_on_status=False,
    )
    try:
        with requests.Session() as session:
            session.mount("https://", HTTPAdapter(max_retries=retries))
            response = session.get(
                URL, timeout=(20, 60),
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; JQCVResultsMonitor/1.1)",
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "es-ES,es;q=0.9",
                    "Cache-Control": "no-cache",
                },
            )
            response.raise_for_status()
            if not response.url.startswith("https://jqcv.gva.es/"):
                raise MonitorError("La petición se ha redirigido fuera de la JQCV.")
            content_type = response.headers.get("Content-Type", "").lower()
            if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
                raise MonitorError("La respuesta no es una página HTML válida.")
            return response.content
    except requests.RequestException as exc:
        detail = normalize(str(exc))
        lower = detail.lower()
        if exc.response is not None:
            cause = f"HTTP {exc.response.status_code}"
        elif isinstance(exc, requests.exceptions.SSLError) or "sslerror" in lower:
            cause = "fallo TLS/certificado"
        elif any(term in lower for term in (
            "nameresolutionerror", "getaddrinfo failed", "name or service not known",
            "temporary failure in name resolution",
        )):
            cause = "fallo de resolución DNS"
        elif isinstance(exc, requests.exceptions.Timeout) or "timeout" in lower or "timed out" in lower:
            cause = "tiempo de espera agotado"
        else:
            cause = "fallo de conexión o de transporte"
        # Solo se muestra el diagnóstico HTTP, nunca contraseñas o direcciones de correo.
        detail = re.sub(r"(?i)(https?://)[^/\s@]+@", r"\1[oculto]@", detail)
        for name in ("EMAIL_USER", "EMAIL_PASSWORD", "EMAIL_TO"):
            value = os.environ.get(name, "")
            if value:
                detail = detail.replace(value, "[oculto]")
        raise MonitorError(
            f"No se pudo descargar la página ({cause}). "
            f"Detalle: {type(exc).__name__}: {detail[:1500]}"
        ) from exc


def extract_content(html: bytes | str, selector: str = DEFAULT_SELECTOR) -> str:
    soup = BeautifulSoup(html, "html.parser")
    try:
        containers = soup.select(selector)
    except Exception as exc:
        raise MonitorError("RESULTS_SELECTOR no contiene un selector CSS válido.") from exc
    if len(containers) != 1:
        raise MonitorError(
            f"El selector encuentra {len(containers)} contenedores; debe encontrar uno. "
            "Comprueba la página y ajusta RESULTS_SELECTOR. No se modifica el estado."
        )
    container = containers[0]
    for element in container.select(
        "script, style, noscript, button, .quick-actions, .lfr-actions, .dropdown-menu"
    ):
        element.decompose()
    text = normalize(container.get_text(" ", strip=True))
    links = set()
    for anchor in container.select("a[href]"):
        href = anchor.get("href", "").strip()
        if not href or href.startswith("#"):
            continue
        target = canonical_url(href)
        if target:
            label = normalize(
                anchor.get_text(" ", strip=True)
                or anchor.get("title", "")
                or anchor.get("aria-label", "")
            )
            links.add((label, target))
    if not text or not links:
        raise MonitorError("La sección está vacía o carece de enlaces HTTP. Se conserva el estado.")
    link_text = "\n".join(f"{label or '(sin título)'} -> {target}" for label, target in sorted(links))
    return f"TEXTO\n{text}\n\nENLACES\n{link_text}\n"


def required_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value.strip():
        raise MonitorError(f"Falta configurar el secreto {name}.")
    return value


def send_email(subject: str, body: str) -> None:
    user = required_env("EMAIL_USER").strip()
    password = required_env("EMAIL_PASSWORD")
    recipient = required_env("EMAIL_TO").strip()
    for address in (user, recipient):
        if "\r" in address or "\n" in address or parseaddr(address)[1] != address or "@" not in address:
            raise MonitorError("EMAIL_USER y EMAIL_TO deben ser direcciones individuales sin nombre.")
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com").strip()
    security = os.environ.get("SMTP_SECURITY", "starttls").strip().lower()
    if not host or security not in {"starttls", "ssl"}:
        raise MonitorError("SMTP_HOST es obligatorio y SMTP_SECURITY debe ser starttls o ssl.")
    try:
        port = int(os.environ.get("SMTP_PORT", "465" if security == "ssl" else "587"))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError as exc:
        raise MonitorError("SMTP_PORT debe ser un puerto válido.") from exc
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = user
    message["To"] = recipient
    message["Date"] = format_datetime(datetime.now(timezone.utc))
    message["Message-ID"] = make_msgid()
    message.set_content(body)
    context = ssl.create_default_context()
    try:
        if security == "ssl":
            client = smtplib.SMTP_SSL(host, port, timeout=30, context=context)
        else:
            client = smtplib.SMTP(host, port, timeout=30)
        with client as smtp:
            smtp.ehlo()
            if security == "starttls":
                smtp.starttls(context=context)
                smtp.ehlo()
            smtp.login(user, password)
            refused = smtp.send_message(message)
            if refused:
                raise MonitorError("El servidor SMTP ha rechazado el destinatario.")
    except (smtplib.SMTPException, OSError) as exc:
        raise MonitorError(
            f"No se pudo enviar el correo ({type(exc).__name__}). "
            "Revisa los secretos y la configuración SMTP; se conserva el estado."
        ) from exc


def save_state(path: Path, content: str) -> None:
    """Reemplaza el archivo de forma atómica, después de enviar el aviso."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(content)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def run_check(state_file: Path, dry_run: bool = False) -> None:
    selector = os.environ.get("RESULTS_SELECTOR", DEFAULT_SELECTOR).strip()
    if not selector:
        raise MonitorError("RESULTS_SELECTOR no puede estar vacío.")
    current = extract_content(fetch_html(), selector)
    if dry_run:
        print(current, end="")
        print("Vista previa: no se ha enviado correo ni guardado el estado.")
        return
    previous = state_file.read_text(encoding="utf-8") if state_file.exists() else None
    if previous is not None and not previous.strip():
        raise MonitorError("El archivo de estado está vacío. Elimínalo para inicializar el monitor.")
    if previous == current:
        print("Sin cambios.")
        return
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if previous is None:
        subject = "JQCV: monitor activado"
        detail = "Se ha registrado la lista inicial. Este mensaje no anuncia una nota nueva.\n\n" + current
    else:
        subject = "JQCV: cambios en la lista de resultados"
        detail = "\n".join(difflib.unified_diff(
            previous.splitlines(), current.splitlines(),
            fromfile="anterior", tofile="actual", lineterm="", n=2,
        ))
    if len(detail) > 20000:
        detail = detail[:20000] + "\n[Contenido del correo recortado; consulta la página.]"
    body = (
        f"Página: {URL}\nComprobación UTC: {now}\n\n{detail}\n\n"
        "El monitor detecta cambios públicos en esta lista. "
        "Consulta la web oficial para comprobar tu calificación personal.\n"
    )
    send_email(subject, body)
    save_state(state_file, current)
    print("Correo enviado y estado actualizado.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", action="store_true", help="Mostrar la lista sin enviar ni guardar.")
    modes.add_argument("--test-email", action="store_true", help="Probar solo el correo, sin consultar la web.")
    args = parser.parse_args()
    try:
        if args.test_email:
            send_email("JQCV: correo de prueba", "El envío SMTP funciona.\n" + URL)
            print("Correo de prueba enviado. Estado sin modificar.")
        else:
            run_check(STATE_FILE, dry_run=args.dry_run)
    except (MonitorError, OSError, UnicodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
