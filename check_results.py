"""Vigila publicaciones oficiales de resultados C1 de la JQCV."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import smtplib
import ssl
import sys
import tempfile
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid, parseaddr
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

URL = "https://jqcv.gva.es/es/resultats"
DEFAULT_SELECTOR = "ul.list-group.show-quick-actions-on-hover"
DEFAULT_ITEM_SELECTOR = ":scope > li"
DEFAULT_TARGET_LEVEL = "C1"
DEFAULT_TARGET_YEAR = "2026"
STATE_FILE = Path(__file__).resolve().with_name("last_content.txt")
STATE_VERSION = 2
MAX_EMAIL_DETAIL = 20_000

RESULT_WORDS = re.compile(
    r"\b(?:resultats?|resultados?|qualificacio(?:ns)?|calificacion(?:es)?)\b"
)
FUTURE_PUBLICATION = re.compile(
    r"\b(?:"
    r"es\s+publicar(?:a|an)|se\s+publicar(?:a|an)|"
    r"publicacio\s+prevista|publicacion\s+prevista|"
    r"data\s+prevista|fecha\s+prevista|"
    r"previsio[n]?\s+de\s+publicacio[n]?|"
    r"(?:consulta|publicacio[n]?)[^.!?]{0,80}"
    r"(?:resultats?|resultados?|qualificacions|calificaciones)[^.!?]{0,80}a\s+partir"
    r")\b"
)
TIMING_WORDS = re.compile(
    r"(?:"
    r"\b\d{1,2}[/-]\d{1,2}[/-]\d{4}\b|"
    r"\b\d{1,2}\s+(?:de\s+|d['’]\s*)"
    r"(?:gener|enero|febrer|febrero|marc|marzo|abril|maig|mayo|juny|junio|"
    r"juliol|julio|agost|agosto|setembre|septiembre|octubre|novembre|noviembre|"
    r"desembre|diciembre)(?:\s+(?:de|del)\s+\d{4})?\b|"
    r"\b(?:primer|segon|segundo|tercer|quart|cuarto)\s+trimestre\b|"
    r"\b(?:durant|durante|en\s+el\s+mes\s+de)\s+"
    r"(?:gener|enero|febrer|febrero|marc|marzo|abril|maig|mayo|juny|junio|"
    r"juliol|julio|agost|agosto|setembre|septiembre|octubre|novembre|noviembre|"
    r"desembre|diciembre)\b|"
    r"\b\d+\s+(?:dies|dias|setmanes|semanas|mesos|meses)\b"
    r")"
)
AVAILABILITY_WORDS = re.compile(
    r"\b(?:disponibles?|publicats?|publicados?|consulta|consultar|consultables?|"
    r"ja\s+es\s+poden|ya\s+se\s+pueden)\b"
)
NOT_AVAILABLE = re.compile(
    r"\b(?:no\s+(?:se\s+han\s+publicado|s'han\s+publicat|estan?\s+disponibles?|"
    r"hay|hi\s+ha)|pendientes?\s+de\s+publicacion|pendents?\s+de\s+publicacio)\b"
)
REQUIREMENT_WORDS = re.compile(r"\b(?:requisit[os]?|requisitos|requisits|requerit|exigit)\b")
PHASE_PATTERNS = (
    ("definitive", re.compile(r"\b(?:definitius?|definitives?|definitivos?|definitivas?)\b")),
    ("final", re.compile(r"\b(?:finals?|finales?)\b")),
    ("provisional", re.compile(r"\b(?:provisionals?|provisionales?)\b")),
    ("partial", re.compile(r"\b(?:parcials?|parciales?)\b")),
)
PHASE_LABELS = {
    "partial": "parcial",
    "provisional": "provisional",
    "final": "final",
    "definitive": "definitiva",
    "unknown": "no indicada",
}


class MonitorError(Exception):
    """Error que impide confirmar una comprobación o enviar el aviso."""


@dataclass(frozen=True)
class MonitorConfig:
    source_url: str = URL
    selector: str = DEFAULT_SELECTOR
    item_selector: str = DEFAULT_ITEM_SELECTOR
    target_level: str = DEFAULT_TARGET_LEVEL
    target_year: str = DEFAULT_TARGET_YEAR

    @classmethod
    def from_environment(cls, *, validate_source: bool = True) -> "MonitorConfig":
        config = cls(
            source_url=os.environ.get("RESULTS_URL", URL).strip(),
            selector=os.environ.get("RESULTS_SELECTOR", DEFAULT_SELECTOR).strip(),
            item_selector=os.environ.get("RESULTS_ITEM_SELECTOR", DEFAULT_ITEM_SELECTOR).strip(),
            target_level=os.environ.get("TARGET_LEVEL", DEFAULT_TARGET_LEVEL).strip().upper(),
            target_year=os.environ.get("TARGET_YEAR", DEFAULT_TARGET_YEAR).strip(),
        )
        if not re.fullmatch(r"[A-Z]\d{1,2}", config.target_level):
            raise MonitorError("TARGET_LEVEL debe tener un formato como C1.")
        if not re.fullmatch(r"20\d{2}", config.target_year):
            raise MonitorError("TARGET_YEAR debe ser un año de cuatro cifras; no cambia automáticamente.")
        if validate_source:
            if not config.selector or not config.item_selector:
                raise MonitorError("Los selectores de resultados no pueden estar vacíos.")
            if not is_official_url(config.source_url):
                raise MonitorError("RESULTS_URL debe ser HTTPS de un dominio oficial gva.es.")
            if config.selector.strip().casefold() in {"body", "html", "*"}:
                raise MonitorError("RESULTS_SELECTOR debe delimitar el bloque de resultados.")
        return config

    @property
    def scope_key(self) -> str:
        return f"{self.target_level}:{self.target_year}"


@dataclass(frozen=True)
class Publication:
    stable_id: str
    semantic_key: str
    kind: str
    title: str
    url: str
    resource_urls: tuple[str, ...]
    level: str
    year: str
    phase: str
    announced_timing: str | None
    official_published_at: str | None
    evidence: str
    source_url: str

    def as_record(self) -> dict[str, object]:
        return {
            "stable_id": self.stable_id,
            "semantic_key": self.semantic_key,
            "kind": self.kind,
            "title": self.title,
            "url": self.url,
            "resource_urls": list(self.resource_urls),
            "level": self.level,
            "year": self.year,
            "phase": self.phase,
            "announced_timing": self.announced_timing,
            "official_published_at": self.official_published_at,
            "evidence": self.evidence,
            "source_url": self.source_url,
        }

    @classmethod
    def from_record(cls, record: object) -> "Publication":
        if not isinstance(record, dict):
            raise MonitorError("El estado contiene una publicación no válida.")
        try:
            if not isinstance(record["resource_urls"], list):
                raise TypeError("resource_urls debe ser una lista")
            for field in ("stable_id", "semantic_key", "kind", "title", "url", "level",
                          "year", "phase", "evidence", "source_url"):
                if not isinstance(record[field], str) or not record[field]:
                    raise TypeError("campo de publicación no válido")
            resources = tuple(str(value) for value in record["resource_urls"])
            publication = cls(
                stable_id=str(record["stable_id"]),
                semantic_key=str(record["semantic_key"]),
                kind=str(record["kind"]),
                title=str(record["title"]),
                url=str(record["url"]),
                resource_urls=resources,
                level=str(record["level"]),
                year=str(record["year"]),
                phase=str(record["phase"]),
                announced_timing=(
                    None if record.get("announced_timing") is None
                    else str(record["announced_timing"])
                ),
                official_published_at=(
                    None if record.get("official_published_at") is None
                    else str(record["official_published_at"])
                ),
                evidence=str(record["evidence"]),
                source_url=str(record["source_url"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise MonitorError("El estado contiene una publicación incompleta.") from exc
        if publication.kind not in {"results", "date_announcement"}:
            raise MonitorError("El estado contiene un tipo de publicación desconocido.")
        if publication.phase not in PHASE_LABELS:
            raise MonitorError("El estado contiene una fase desconocida.")
        return publication


@dataclass(frozen=True)
class ExtractionResult:
    source_url: str
    candidate_count: int
    publications: tuple[Publication, ...]


@dataclass(frozen=True)
class ChangeEvent:
    event_type: str
    current: Publication
    previous: Publication | None = None


def normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split())


def fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", normalize(text)).casefold()
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def is_official_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        return (
            parts.scheme == "https"
            and (host == "gva.es" or host.endswith(".gva.es"))
            and parts.username is None and parts.password is None
            and parts.port in {None, 443}
        )
    except ValueError:
        return False


def _safe_netloc(parts) -> str:
    hostname = parts.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    try:
        port = parts.port
    except ValueError:
        return ""
    return f"{hostname}:{port}" if port else hostname


def canonical_url(href: str, base_url: str = URL) -> str:
    """Normaliza la URL sin eliminar identificadores o versiones del documento."""
    parts = urlsplit(urljoin(base_url, href.strip()))
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        return ""
    path = re.sub(r";jsessionid=[^/;?#]*", "", parts.path, flags=re.I)
    ignored = {"p_auth", "p_p_auth", "jsessionid", "_"}
    query = sorted(
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in ignored and not key.lower().startswith("utm_")
    )
    netloc = _safe_netloc(parts)
    if not netloc:
        return ""
    return urlunsplit((
        parts.scheme.lower(), netloc.lower(), path,
        urlencode(query), parts.fragment,
    ))


def _identity_url(url: str) -> str:
    parts = urlsplit(url)
    identity_ignored = {"version", "versionid", "documentversion"}
    query = sorted(
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in identity_ignored
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _level_pattern(level: str) -> re.Pattern[str]:
    match = re.fullmatch(r"([A-Z])(\d{1,2})", level)
    if not match:
        return re.compile(r"(?!)")
    letter, number = match.groups()
    return re.compile(rf"(?<![a-z0-9]){letter.casefold()}[\s._-]*{number}(?![a-z0-9])")


def _explicit_levels(text: str) -> set[str]:
    return {
        f"{letter.upper()}{number}"
        for letter, number in re.findall(r"(?<![a-z0-9])([abc])[\s._-]*(\d{1,2})(?![a-z0-9])", fold(text))
    }


def _nearest_heading_text(container) -> str:
    # Solo títulos de la sección que contiene la lista; nunca de otra entrada.
    current = container
    while current.parent is not None and current.name not in {"main", "body", "html"}:
        for sibling in current.previous_siblings:
            if getattr(sibling, "name", None) in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                return normalize(sibling.get_text(" ", strip=True))
        current = current.parent
    return ""


def _candidate_context(node, container) -> str:
    parts = [normalize(node.get_text(" ", strip=True))]
    heading = _nearest_heading_text(container)
    if heading:
        parts.append(heading)
    for current in node.parents:
        if current is container:
            break
        for attribute in ("data-year", "data-level", "aria-label"):
            value = current.get(attribute)
            if value:
                parts.append(normalize(str(value)))
    for attribute in ("data-year", "data-level", "aria-label"):
        value = container.get(attribute)
        if value:
            parts.append(normalize(str(value)))
    return normalize(" ".join(parts))


def _phase(text: str) -> str:
    folded = fold(text)
    for name, pattern in PHASE_PATTERNS:
        if pattern.search(folded):
            return name
    return "unknown"


def _publication_years(node, container, item_text: str) -> set[str]:
    """El año de convocatoria tiene precedencia sobre la fecha de una noticia."""
    call_years = set(re.findall(
        r"\bconvocatoria\s+(?:(?:de|del|de l'any|del ano)\s+)?(20\d{2})\b",
        fold(item_text),
    ))
    if call_years:
        return call_years
    title_nodes = node.select("h1, h2, h3, h4, h5, h6, a[href]")
    title_years = set(re.findall(
        r"(?<!\d)20\d{2}(?!\d)",
        " ".join(element.get_text(" ", strip=True) for element in title_nodes),
    ))
    if title_years:
        return title_years
    for current in [node, *node.parents]:
        declared = str(current.get("data-year", ""))
        if re.fullmatch(r"20\d{2}", declared):
            return {declared}
        if current is container:
            break
    context_years = set(re.findall(r"(?<!\d)20\d{2}(?!\d)", _nearest_heading_text(container)))
    return context_years or set(re.findall(r"(?<!\d)20\d{2}(?!\d)", item_text))


def _announcement_timing(text: str) -> str | None:
    sentences = re.split(r"(?<=[.!?;])\s+|[\r\n]+", normalize(text))
    for sentence in sentences:
        folded = fold(sentence)
        if (
            RESULT_WORDS.search(folded)
            and FUTURE_PUBLICATION.search(folded)
            and TIMING_WORDS.search(folded)
        ):
            return sentence[:600]
    return None


def _published_at(node) -> str | None:
    for element in node.select("time[datetime]"):
        attributes = " ".join((
            str(element.get("itemprop", "")),
            str(element.get("id", "")),
            " ".join(element.get("class", [])),
            str(element.get("aria-label", "")),
            str(element.get("title", "")),
            str(element.get("data-label", "")),
        ))
        folded_attributes = fold(attributes)
        if not (
            str(element.get("itemprop", "")).casefold() == "datepublished"
            or re.search(r"\b(?:published|publicat|publicado|publicacio|publicacion)\b", folded_attributes)
        ):
            continue
        value = normalize(str(element.get("datetime", "")))
        if not value:
            continue
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        return value
    return None


def _semantic_key(title: str, url: str, level: str, year: str) -> str:
    raw = fold(title)
    raw = RESULT_WORDS.sub(" ", raw)
    for _, pattern in PHASE_PATTERNS:
        raw = pattern.sub(" ", raw)
    raw = _level_pattern(level).sub(" ", raw)
    raw = re.sub(rf"(?<!\d){re.escape(year)}(?!\d)", " ", raw)
    raw = re.sub(
        r"\b(?:convocatoria|convocatoria|consulta|document|documents|documento|documentos|"
        r"publicacio|publicacion|pdf|html?)\b",
        " ", raw,
    )
    tokens = [token for token in re.findall(r"[a-z0-9]+", raw) if not token.isdigit()]
    meaningful = " ".join(tokens)
    if not meaningful:
        # El fallback solo se usa cuando hay una única entrada semánticamente genérica
        # en ambos lados de la comparación; _reconcile rechaza emparejamientos ambiguos.
        meaningful = f"scope {level.casefold()} {year}"
    return hashlib.sha256(meaningful.encode("utf-8")).hexdigest()[:24]


def _make_publication(node, container, config: MonitorConfig) -> Publication | None:
    item_text = normalize(node.get_text(" ", strip=True))
    if not item_text:
        return None
    context = _candidate_context(node, container)
    folded_item = fold(item_text)
    folded_context = fold(context)

    explicit_levels = _explicit_levels(item_text)
    if explicit_levels and config.target_level not in explicit_levels:
        return None
    if not _level_pattern(config.target_level).search(folded_context):
        return None

    if config.target_year not in _publication_years(node, container, item_text):
        return None

    timing = _announcement_timing(item_text)
    if REQUIREMENT_WORDS.search(folded_item) or NOT_AVAILABLE.search(folded_item):
        return None
    if timing is not None:
        kind = "date_announcement"
    elif FUTURE_PUBLICATION.search(folded_item):
        # Un futuro sin fecha verificable no significa resultados disponibles.
        return None
    elif RESULT_WORDS.search(folded_item) or (
        RESULT_WORDS.search(folded_context) and AVAILABILITY_WORDS.search(folded_item)
    ):
        kind = "results"
    else:
        return None

    anchors: list[tuple[str, str]] = []
    for anchor in node.select("a[href]"):
        target = canonical_url(str(anchor.get("href", "")), config.source_url)
        if target and is_official_url(target):
            anchors.append((normalize(anchor.get_text(" ", strip=True)), target))
    if kind == "results" and not anchors and not AVAILABILITY_WORDS.search(folded_item):
        return None

    chosen_title = ""
    chosen_url = ""
    level_matcher = _level_pattern(config.target_level)
    for label, target in anchors:
        folded_label = fold(label)
        if level_matcher.search(folded_label) or RESULT_WORDS.search(folded_label):
            chosen_title, chosen_url = label, target
            break
    if not chosen_url and anchors:
        chosen_title, chosen_url = anchors[0]
    if not chosen_title:
        heading = node.select_one("h1, h2, h3, h4, h5, h6")
        chosen_title = normalize(heading.get_text(" ", strip=True)) if heading else item_text
    if not chosen_url:
        chosen_url = canonical_url(config.source_url, config.source_url)

    resources = tuple(sorted({target for _, target in anchors}))
    # Los enlaces sin título legible no se confunden con una publicación confirmada.
    if not anchors and kind == "results" and not AVAILABILITY_WORDS.search(folded_item):
        return None
    identity_material = _identity_url(chosen_url)
    if chosen_url == canonical_url(config.source_url, config.source_url):
        identity_material += "|" + fold(chosen_title)
    stable_id = hashlib.sha256(
        f"{identity_material}|{config.target_level}|{config.target_year}".encode("utf-8")
    ).hexdigest()[:24]
    return Publication(
        stable_id=stable_id,
        semantic_key=_semantic_key(chosen_title, chosen_url, config.target_level, config.target_year),
        kind=kind,
        title=chosen_title[:500],
        url=chosen_url,
        resource_urls=resources,
        level=config.target_level,
        year=config.target_year,
        phase=_phase(item_text),
        announced_timing=timing,
        official_published_at=_published_at(node),
        evidence=item_text,
        source_url=config.source_url,
    )


def extract_publications(html: bytes | str, config: MonitorConfig | None = None) -> ExtractionResult:
    """Extrae entradas C1/año de un bloque delimitado; nunca usa toda la página como fallback."""
    config = config or MonitorConfig.from_environment()
    soup = BeautifulSoup(html, "html.parser")
    try:
        containers = soup.select(config.selector)
    except Exception as exc:
        raise MonitorError("RESULTS_SELECTOR no contiene un selector CSS válido.") from exc
    if len(containers) != 1:
        raise MonitorError(
            f"El selector encuentra {len(containers)} contenedores; debe encontrar uno. "
            "No se modifica el estado."
        )
    container = containers[0]
    if container.name in {"html", "body"}:
        raise MonitorError("El selector debe delimitar resultados, no toda la página.")
    for element in container.select(
        "script, style, noscript, button, .quick-actions, .lfr-actions, .dropdown-menu"
    ):
        element.decompose()
    if re.search(
        r"\b(?:mantenimiento|manteniment|maintenance|temporarily unavailable|"
        r"servicio no disponible|servei no disponible)\b",
        fold(container.get_text(" ", strip=True)),
    ):
        raise MonitorError("La fuente muestra mantenimiento o indisponibilidad; se conserva el estado.")
    try:
        candidates = container.select(config.item_selector)
    except Exception as exc:
        raise MonitorError("RESULTS_ITEM_SELECTOR no contiene un selector CSS válido.") from exc
    candidates = [candidate for candidate in candidates if normalize(candidate.get_text(" ", strip=True))]
    if not candidates:
        raise MonitorError(
            "El bloque de resultados no contiene elementos estructurados válidos. "
            "Puede ser una página de mantenimiento; se conserva el estado."
        )

    publications: dict[str, Publication] = {}
    for candidate in candidates:
        publication = _make_publication(candidate, container, config)
        if publication is None:
            continue
        previous = publications.get(publication.stable_id)
        if previous is None:
            publications[publication.stable_id] = publication
            continue
        merged_resources = tuple(sorted(set(previous.resource_urls) | set(publication.resource_urls)))
        # Orden determinista incluso si un mismo enlace aparece varias veces.
        rank = {"unknown": 0, "partial": 1, "provisional": 2, "final": 3, "definitive": 4}
        winner = max(
            (previous, publication),
            key=lambda item: (rank[item.phase], item.kind, fold(item.title), fold(item.evidence)),
        )
        publications[publication.stable_id] = replace(
            winner,
            resource_urls=merged_resources,
        )
    return ExtractionResult(
        source_url=config.source_url,
        candidate_count=len(candidates),
        publications=tuple(sorted(publications.values(), key=lambda item: item.stable_id)),
    )


def _redact_exception(exc: BaseException) -> str:
    detail = normalize(str(exc))
    detail = re.sub(r"(?i)(https?://)[^/\s@]+@", r"\1[oculto]@", detail)
    for name in ("EMAIL_USER", "EMAIL_PASSWORD", "EMAIL_TO"):
        value = os.environ.get(name, "")
        if value:
            detail = detail.replace(value, "[oculto]")
    return detail[:1_500]


def fetch_html(config: MonitorConfig | None = None) -> bytes:
    config = config or MonitorConfig.from_environment()
    retries = Retry(
        total=2,
        connect=2,
        read=2,
        status=2,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods={"GET"},
        respect_retry_after_header=False,
        raise_on_status=False,
    )
    try:
        with requests.Session() as session:
            session.max_redirects = 5
            session.mount("https://", HTTPAdapter(max_retries=retries))
            response = session.get(
                config.source_url,
                timeout=(12, 45),
                headers={
                    "User-Agent": "JQCVResultsMonitor/2.0 (+GitHub-Actions)",
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "ca,es;q=0.9",
                    "Cache-Control": "no-cache",
                },
            )
            response.raise_for_status()
            final = urlsplit(response.url)
            expected = urlsplit(config.source_url)
            if final.scheme != "https" or final.hostname != expected.hostname:
                raise MonitorError("La petición se ha redirigido fuera del host oficial configurado.")
            content_type = response.headers.get("Content-Type", "").lower()
            if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
                raise MonitorError("La respuesta no es una página HTML válida.")
            if not response.content.strip():
                raise MonitorError("La fuente ha devuelto una respuesta HTML vacía.")
            return response.content
    except requests.RequestException as exc:
        detail = _redact_exception(exc)
        lower = detail.lower()
        if exc.response is not None:
            cause = f"HTTP {exc.response.status_code}"
        elif isinstance(exc, requests.exceptions.SSLError) or "sslerror" in lower:
            cause = "fallo TLS/certificado"
        elif isinstance(exc, requests.exceptions.ConnectTimeout):
            cause = "tiempo de espera de conexión agotado"
        elif isinstance(exc, requests.exceptions.ReadTimeout):
            cause = "tiempo de espera de lectura agotado"
        elif any(term in lower for term in (
            "nameresolutionerror", "getaddrinfo failed", "name or service not known",
            "temporary failure in name resolution",
        )):
            cause = "fallo de resolución DNS"
        elif isinstance(exc, requests.exceptions.Timeout) or "timeout" in lower or "timed out" in lower:
            cause = "tiempo de espera agotado"
        else:
            cause = "fallo de conexión o de transporte"
        raise MonitorError(
            f"No se pudo descargar la página ({cause}). "
            f"Detalle: {type(exc).__name__}: {detail}"
        ) from exc


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


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(content)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _blank_state() -> dict[str, object]:
    return {"schema_version": STATE_VERSION, "scopes": {}}


def _legacy_snapshot(html: bytes | str, config: MonitorConfig) -> str:
    """Reproduce exactamente el formato histórico para migrar solo sin diferencias."""
    soup = BeautifulSoup(html, "html.parser")
    blocks = soup.select(config.selector)
    if len(blocks) != 1:
        raise MonitorError("No se puede reconciliar el bloque con el estado legado.")
    block = blocks[0]
    for element in block.select(
        "script, style, noscript, button, .quick-actions, .lfr-actions, .dropdown-menu"
    ):
        element.decompose()
    links = set()
    for anchor in block.select("a[href]"):
        href = str(anchor.get("href", "")).strip()
        if not href or href.startswith("#"):
            continue
        target = canonical_url(href, config.source_url)
        if target:
            label = normalize(anchor.get_text(" ", strip=True)
                              or anchor.get("title", "") or anchor.get("aria-label", ""))
            links.add((label, target))
    link_text = "\n".join(
        f"{label or '(sin título)'} -> {target}" for label, target in sorted(links)
    )
    return f"TEXTO\n{normalize(block.get_text(' ', strip=True))}\n\nENLACES\n{link_text}\n"


def _read_state(path: Path) -> tuple[str, dict[str, object], str | None]:
    if not path.exists():
        return "missing", _blank_state(), None
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        raise MonitorError("El archivo de estado está vacío; no se reinicializa automáticamente.")
    try:
        state = json.loads(raw)
    except json.JSONDecodeError:
        if raw.startswith("TEXTO\n") and "\nENLACES\n" in raw:
            return "legacy", _blank_state(), raw
        raise MonitorError("El archivo de estado no es JSON v2 ni un snapshot legado reconocido.")
    if not isinstance(state, dict) or state.get("schema_version") != STATE_VERSION:
        raise MonitorError("La versión del archivo de estado no está soportada.")
    if not isinstance(state.get("scopes"), dict):
        raise MonitorError("El archivo de estado no contiene scopes válidos.")
    return "json", state, None


def _serialize_state(state: dict[str, object]) -> str:
    return json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _scope_from_publications(
    config: MonitorConfig,
    publications: tuple[Publication, ...],
    detected_at: datetime,
    legacy_raw: str | None = None,
) -> dict[str, object]:
    scope: dict[str, object] = {
        "target_level": config.target_level,
        "target_year": config.target_year,
        "initialized_at_utc": detected_at.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "items": {item.stable_id: item.as_record() for item in publications},
    }
    if legacy_raw is not None:
        scope["migrated_legacy_sha256"] = hashlib.sha256(legacy_raw.encode("utf-8")).hexdigest()
        scope["legacy_snapshot"] = legacy_raw
    return scope


def _publication_fingerprint(publication: Publication) -> str:
    significant = {
        "kind": publication.kind,
        "title": fold(publication.title),
        "url": publication.url,
        "resource_urls": sorted(publication.resource_urls),
        "level": publication.level,
        "year": publication.year,
        "phase": publication.phase,
        "announced_timing": fold(publication.announced_timing or ""),
        "official_published_at": publication.official_published_at,
        "source_url": publication.source_url,
        "evidence": fold(publication.evidence),
    }
    encoded = json.dumps(significant, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _event_type(previous: Publication | None, current: Publication) -> str:
    if current.kind == "date_announcement":
        return "date_announcement"
    if previous is None:
        return "new_results"
    if previous.kind == "date_announcement" and current.kind == "results":
        return "results_available"
    if previous.phase != current.phase and current.phase != "unknown":
        return "phase_transition"
    return "updated_results"


def _reconcile(
    old_records: object,
    current: tuple[Publication, ...],
) -> tuple[dict[str, object], tuple[ChangeEvent, ...]]:
    if not isinstance(old_records, dict):
        raise MonitorError("El scope del estado no contiene un mapa de publicaciones válido.")
    old = {key: Publication.from_record(value) for key, value in old_records.items()}
    if any(key != item.stable_id for key, item in old.items()):
        raise MonitorError("La identidad de una publicación del estado no coincide con su clave.")
    known: dict[str, object] = dict(old_records)
    events: list[ChangeEvent] = []
    used_old: set[str] = set()
    old_semantic: dict[str, list[str]] = {}
    current_semantic: dict[str, int] = {}
    for key, item in old.items():
        old_semantic.setdefault(item.semantic_key, []).append(key)
    for item in current:
        current_semantic[item.semantic_key] = current_semantic.get(item.semantic_key, 0) + 1

    for item in current:
        match_id: str | None = None
        if item.stable_id in old and item.stable_id not in used_old:
            match_id = item.stable_id
        else:
            candidates = [
                key for key in old_semantic.get(item.semantic_key, [])
                if key not in used_old
            ]
            if len(candidates) == 1 and current_semantic[item.semantic_key] == 1:
                match_id = candidates[0]
        if match_id is None:
            record = item.as_record()
            record["seen_fingerprints"] = [_publication_fingerprint(item)]
            known[item.stable_id] = record
            events.append(ChangeEvent(_event_type(None, item), item))
            continue
        used_old.add(match_id)
        previous = old[match_id]
        current_item = replace(item, stable_id=match_id)
        seen = old_records[match_id].get("seen_fingerprints", [])
        if not isinstance(seen, list) or not all(isinstance(value, str) for value in seen):
            raise MonitorError("El historial de versiones del estado no es válido.")
        fingerprint = _publication_fingerprint(current_item)
        if _publication_fingerprint(previous) == fingerprint or fingerprint in seen:
            continue
        record = current_item.as_record()
        record["seen_fingerprints"] = sorted(set(seen) | {
            _publication_fingerprint(previous), fingerprint,
        })
        known[match_id] = record
        events.append(ChangeEvent(_event_type(previous, current_item), current_item, previous))
    return known, tuple(events)


def _detection_times(now: datetime | None = None) -> tuple[str, str]:
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    utc_text = instant.astimezone(timezone.utc).isoformat(timespec="seconds")
    madrid = instant.astimezone(ZoneInfo("Europe/Madrid"))
    madrid_text = madrid.strftime("%Y-%m-%d %H:%M:%S %Z (Europe/Madrid)")
    return madrid_text, utc_text


def _publication_lines(publication: Publication) -> list[str]:
    lines = [
        f"Título: {publication.title}",
        f"Nivel: {publication.level}",
        f"Convocatoria: {publication.year}",
        f"Fase: {PHASE_LABELS.get(publication.phase, publication.phase)}",
        f"Enlace oficial: {publication.url}",
    ]
    if publication.announced_timing:
        lines.append(f"Fecha o plazo anunciado (texto oficial): {publication.announced_timing}")
    if publication.official_published_at:
        lines.append(f"Fecha de publicación indicada por la fuente: {publication.official_published_at}")
    for target in publication.resource_urls:
        if target != publication.url:
            lines.append(f"Otro enlace oficial: {target}")
    lines.append(f"Texto de la publicación: {publication.evidence[:1_500]}")
    return lines


EVENT_LABELS = {
    "new_results": "Nueva publicación de resultados del C1",
    "results_available": "Resultados del C1 disponibles tras un anuncio previo",
    "updated_results": "Cambio relevante en una publicación del C1",
    "phase_transition": "Cambio de fase de los resultados del C1",
    "date_announcement": "Anuncio oficial de fecha o plazo de publicación",
}


def _initialization_email(
    config: MonitorConfig,
    result: ExtractionResult,
    now: datetime,
) -> tuple[str, str]:
    madrid, utc_text = _detection_times(now)
    lines = [
        "Tipo de aviso: inicialización del monitor.",
        "Este mensaje registra la situación ya existente; no anuncia una publicación nueva.",
        f"Nivel vigilado: {config.target_level}",
        f"Convocatoria vigilada: {config.target_year}",
        f"Detección: {madrid}",
        f"Registro técnico UTC: {utc_text}",
        f"Fuente oficial: {config.source_url}",
        "",
    ]
    if result.publications:
        lines.append("Publicaciones pertinentes encontradas al inicializar:")
        for publication in result.publications:
            lines.extend(["", *_publication_lines(publication)])
    else:
        lines.append("No se encontraron publicaciones pertinentes del alcance configurado.")
    lines.extend([
        "",
        "El monitor detecta disponibilidad pública; no consulta calificaciones personales ni implica un resultado individual.",
    ])
    return f"JQCV {config.target_level} {config.target_year}: monitor inicializado", "\n".join(lines)


def _changes_email(
    config: MonitorConfig,
    events: tuple[ChangeEvent, ...],
    now: datetime,
) -> tuple[str, str]:
    madrid, utc_text = _detection_times(now)
    if any(event.event_type in {"new_results", "results_available", "phase_transition"} for event in events):
        subject = f"JQCV {config.target_level} {config.target_year}: resultados disponibles"
    elif all(event.event_type == "date_announcement" for event in events):
        subject = f"JQCV {config.target_level} {config.target_year}: anuncio oficial de publicación"
    else:
        subject = f"JQCV {config.target_level} {config.target_year}: publicación actualizada"
    lines = [
        f"Nivel vigilado: {config.target_level}",
        f"Convocatoria vigilada: {config.target_year}",
        f"Detección: {madrid}",
        f"Registro técnico UTC: {utc_text}",
        f"Fuente oficial: {config.source_url}",
    ]
    for event in events:
        lines.extend(["", f"Motivo del aviso: {EVENT_LABELS[event.event_type]}"])
        if event.previous and event.event_type == "phase_transition":
            lines.append(
                "Transición: "
                f"{PHASE_LABELS.get(event.previous.phase, event.previous.phase)} -> "
                f"{PHASE_LABELS.get(event.current.phase, event.current.phase)}"
            )
        lines.extend(_publication_lines(event.current))
    lines.extend([
        "",
        "La hora de detección no es una fecha oficial de publicación.",
        "Consulta siempre el enlace oficial. El aviso no contiene ni consulta datos personales.",
    ])
    body = "\n".join(lines)
    if len(body) > MAX_EMAIL_DETAIL:
        body = body[:MAX_EMAIL_DETAIL] + "\n[Contenido recortado; consulta los enlaces oficiales.]"
    return subject, body


def _preview(result: ExtractionResult, config: MonitorConfig) -> None:
    print(f"Fuente oficial: {result.source_url}")
    print(f"Alcance: nivel={config.target_level} convocatoria={config.target_year}")
    print(f"Elementos estructurales examinados: {result.candidate_count}")
    print(f"Publicaciones pertinentes: {len(result.publications)}")
    print(json.dumps(
        [publication.as_record() for publication in result.publications],
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ))
    print("Vista previa: no se ha enviado correo ni leído o modificado el estado.")


def run_check(
    state_file: Path = STATE_FILE,
    *,
    preview: bool = False,
    dry_run: bool = False,
    config: MonitorConfig | None = None,
    now: datetime | None = None,
) -> None:
    config = config or MonitorConfig.from_environment()
    html = fetch_html(config)
    result = extract_publications(html, config)
    if preview or dry_run:
        _preview(result, config)
        return

    instant = now or datetime.now(timezone.utc)
    state_kind, state, legacy_raw = _read_state(state_file)
    scopes = state["scopes"]
    assert isinstance(scopes, dict)

    if state_kind == "legacy":
        if _legacy_snapshot(html, config) != legacy_raw:
            raise MonitorError(
                "El estado legado difiere de la fuente actual. Se conserva sin cambios: "
                "la migración requiere revisar esas diferencias en GitHub para no perder avisos."
            )
        scopes[config.scope_key] = _scope_from_publications(
            config, result.publications, instant, legacy_raw=legacy_raw
        )
        _atomic_write(state_file, _serialize_state(state))
        print("Estado legado migrado a JSON v2 sin generar avisos de novedad.")
        return

    if state_kind == "missing" or config.scope_key not in scopes:
        subject, body = _initialization_email(config, result, instant)
        send_email(subject, body)
        scopes[config.scope_key] = _scope_from_publications(config, result.publications, instant)
        _atomic_write(state_file, _serialize_state(state))
        print("Correo de inicialización enviado y estado guardado.")
        return

    scope = scopes[config.scope_key]
    if not isinstance(scope, dict):
        raise MonitorError("El scope configurado del estado no es válido.")
    old_records = scope.get("items")
    if isinstance(old_records, dict) and old_records and not result.publications:
        raise MonitorError(
            "La extracción actual no contiene publicaciones C1 aunque el estado sí. "
            "Se conserva el estado para evitar una pérdida por página incompleta o cambio de estructura."
        )
    known, events = _reconcile(old_records, result.publications)
    if not events:
        print("Sin cambios relevantes.")
        return
    subject, body = _changes_email(config, events, instant)
    send_email(subject, body)
    scope["items"] = known
    _atomic_write(state_file, _serialize_state(state))
    print(f"Correo enviado y estado actualizado ({len(events)} cambio(s) relevante(s)).")


def send_test_email(config: MonitorConfig | None = None) -> None:
    config = config or MonitorConfig.from_environment(validate_source=False)
    madrid, utc_text = _detection_times()
    subject = f"[PRUEBA] JQCV {config.target_level} {config.target_year}: SMTP"
    body = (
        "Este es un mensaje de prueba del monitor; no anuncia resultados.\n"
        f"Destinatario configurado: EMAIL_TO.\nDetección: {madrid}\n"
        f"Registro técnico UTC: {utc_text}\nFuente vigilada: {config.source_url}\n"
    )
    send_email(subject, body)
    print("Correo de prueba enviado. Estado sin modificar.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--preview", action="store_true", help="Mostrar publicaciones sin correo ni estado.")
    modes.add_argument("--dry-run", action="store_true", help="Alias compatible de --preview.")
    modes.add_argument("--test-email", action="store_true", help="Probar solo SMTP sin consultar la web.")
    args = parser.parse_args()
    try:
        if args.test_email:
            send_test_email(MonitorConfig.from_environment(validate_source=False))
        else:
            config = MonitorConfig.from_environment()
            run_check(preview=args.preview or args.dry_run, config=config)
    except (MonitorError, OSError, UnicodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
