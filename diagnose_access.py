"""Diagnóstico acotado y sin efectos secundarios del acceso HTTPS a la JQCV."""

from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import ssl
import subprocess
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

import requests

DEFAULT_URL = "https://jqcv.gva.es/es/resultats"
USER_AGENT = "JQCVResultsMonitor-Diagnostic/2.0 (+GitHub-Actions)"
MAX_BODY = 2 * 1024 * 1024
TCP_TIMEOUT = 4
TLS_TIMEOUT = 8
REPORT: list[dict[str, object]] = []


def emit(stage: str, status: str, **fields: object) -> None:
    record = {"stage": stage, "status": status, **fields}
    REPORT.append(record)
    print("DIAG " + json.dumps(record, ensure_ascii=False, sort_keys=True), flush=True)


def safe_url(url: str) -> str:
    parts = urlsplit(url)
    hostname = parts.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = f"{hostname}:{port}" if port else hostname
    path = parts.path.split(";jsessionid=", 1)[0]
    return urlunsplit((parts.scheme, netloc, path, "", ""))


def elapsed_ms(start: float) -> int:
    return round((time.monotonic() - start) * 1000)


def exception_kind(exc: BaseException) -> str:
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(exc, ConnectionRefusedError):
        return "refused"
    if isinstance(exc, ssl.SSLCertVerificationError):
        return "certificate_validation"
    if isinstance(exc, ssl.SSLError):
        return "tls_error"
    if isinstance(exc, socket.gaierror):
        return "dns_error"
    return "os_error"


def runtime_metadata() -> None:
    try:
        completed = subprocess.run(
            ["curl", "--version"], capture_output=True, text=True, timeout=5, check=False
        )
        lines = completed.stdout.splitlines()
        curl_version = lines[0].split(" ", 2)[:2] if lines else []
        curl_text = " ".join(curl_version) if curl_version else "unavailable"
    except (OSError, subprocess.SubprocessError):
        curl_text = "unavailable"
    proxy_names = [
        name for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY",
                          "https_proxy", "http_proxy", "all_proxy", "no_proxy")
        if os.environ.get(name)
    ]
    emit(
        "runtime",
        "ok",
        context="github_actions" if os.environ.get("GITHUB_ACTIONS") == "true" else "other",
        utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        runner_os=os.environ.get("RUNNER_OS", platform.system()),
        runner_arch=os.environ.get("RUNNER_ARCH", platform.machine()),
        image_os=os.environ.get("ImageOS", "unknown"),
        image_version=os.environ.get("ImageVersion", "unknown"),
        github_run_id=os.environ.get("GITHUB_RUN_ID", "not_available"),
        github_run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT", "not_available"),
        github_sha=os.environ.get("GITHUB_SHA", "not_available"),
        python=platform.python_version(),
        openssl=ssl.OPENSSL_VERSION,
        requests=requests.__version__,
        curl=curl_text,
        proxy_variables_present=proxy_names,
    )


def resolve(host: str, port: int) -> dict[int, list[tuple] | None]:
    start = time.monotonic()
    result: dict[int, list[tuple]] = {socket.AF_INET: [], socket.AF_INET6: []}
    try:
        rows = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM, socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        emit(
            "dns",
            "failed",
            duration_ms=elapsed_ms(start),
            error="dns_error",
            exception=type(exc).__name__,
            errno=exc.errno,
        )
        # Un fallo DNS no demuestra que no exista una familia de direcciones.
        return {socket.AF_INET: None, socket.AF_INET6: None}
    seen: set[tuple[int, str]] = set()
    for family, _, _, _, sockaddr in rows:
        if family not in result:
            continue
        key = (family, sockaddr[0])
        if key in seen or len(result[family]) >= 4:
            continue
        seen.add(key)
        result[family].append(sockaddr)
    emit(
        "dns",
        "ok",
        duration_ms=elapsed_ms(start),
        ipv4=[row[0] for row in result[socket.AF_INET]],
        ipv6=[row[0] for row in result[socket.AF_INET6]],
    )
    return result


def probe_tcp(family: int, addresses: list[tuple] | None, family_name: str) -> tuple | None:
    if addresses is None:
        emit("tcp", "skipped", family=family_name, reason="dns_unknown")
        return None
    if not addresses:
        emit("tcp", "not_applicable", family=family_name, reason=f"no_{family_name.lower()}_dns")
        return None
    for sockaddr in addresses[:2]:
        start = time.monotonic()
        try:
            with socket.socket(family, socket.SOCK_STREAM) as connection:
                connection.settimeout(TCP_TIMEOUT)
                connection.connect(sockaddr)
            emit(
                "tcp", "ok", family=family_name, address=sockaddr[0],
                port=sockaddr[1], duration_ms=elapsed_ms(start), transport="direct",
            )
            return sockaddr
        except OSError as exc:
            emit(
                "tcp", "failed", family=family_name, address=sockaddr[0],
                port=sockaddr[1], duration_ms=elapsed_ms(start),
                error=exception_kind(exc), exception=type(exc).__name__,
                errno=getattr(exc, "errno", None), transport="direct",
            )
    return None


def probe_tls(host: str, family: int, sockaddr: tuple | None, family_name: str) -> None:
    if sockaddr is None:
        emit("tls", "skipped", family=family_name, reason="no_successful_tcp", transport="direct")
        return
    start = time.monotonic()
    context = ssl.create_default_context()
    try:
        with socket.socket(family, socket.SOCK_STREAM) as raw:
            raw.settimeout(TLS_TIMEOUT)
            raw.connect(sockaddr)
            with context.wrap_socket(raw, server_hostname=host) as secure:
                cipher = secure.cipher()
                emit(
                    "tls", "ok", family=family_name, address=sockaddr[0],
                    duration_ms=elapsed_ms(start), transport="direct",
                    certificate_validation=True, tls_version=secure.version(),
                    cipher=cipher[0] if cipher else "unknown",
                )
    except OSError as exc:
        fields: dict[str, object] = {
            "family": family_name,
            "address": sockaddr[0],
            "duration_ms": elapsed_ms(start),
            "transport": "direct",
            "error": exception_kind(exc),
            "exception": type(exc).__name__,
            "errno": getattr(exc, "errno", None),
        }
        if isinstance(exc, ssl.SSLCertVerificationError):
            fields["verify_code"] = exc.verify_code
        emit("tls", "failed", **fields)


CURL_ERRORS = {
    5: "proxy_dns_error",
    6: "dns_error",
    7: "tcp_error",
    28: "timeout",
    35: "tls_error",
    47: "too_many_redirects",
    51: "certificate_validation",
    52: "empty_reply",
    56: "transport_error",
    60: "certificate_validation",
}


def probe_curl(url: str, family_name: str, applicable: bool) -> bool:
    if not applicable:
        emit("http_curl", "not_applicable", family=family_name, reason=f"no_{family_name.lower()}_dns")
        return False
    flags = {"IPv4": ["--ipv4"], "IPv6": ["--ipv6"], "environment": []}[family_name]
    output_format = "\t".join((
        "%{http_code}", "%{remote_ip}", "%{num_redirects}", "%{url_effective}",
        "%{time_namelookup}", "%{time_connect}", "%{time_appconnect}",
        "%{time_starttransfer}", "%{time_total}", "%{size_download}",
        "%{content_type}", "%{ssl_verify_result}",
    ))
    command = [
        "curl", *flags, "--silent", "--show-error", "--location", "--max-redirs", "5",
        "--proto", "=https", "--proto-redir", "=https", "--connect-timeout", "8",
        "--max-time", "25", "--output", os.devnull,
        "--header", f"User-Agent: {USER_AGENT}",
        "--header", "Accept: text/html,application/xhtml+xml",
        "--header", "Accept-Language: ca,es;q=0.9",
        "--write-out", output_format, url,
    ]
    start = time.monotonic()
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    except subprocess.TimeoutExpired:
        emit("http_curl", "failed", family=family_name, duration_ms=elapsed_ms(start), error="timeout")
        return False
    except OSError as exc:
        emit(
            "http_curl", "failed", family=family_name, duration_ms=elapsed_ms(start),
            error="client_unavailable", errno=getattr(exc, "errno", None),
        )
        return False
    fields = completed.stdout.split("\t")
    if len(fields) != 12:
        emit(
            "http_curl", "failed", family=family_name, duration_ms=elapsed_ms(start),
            curl_exit=completed.returncode,
            error=CURL_ERRORS.get(completed.returncode, "curl_error"),
        )
        return False
    code, remote_ip, redirects, final_url, dns_t, connect_t, tls_t, first_t, total_t, size, content_type, verify = fields
    reached_http = code.isdigit() and int(code) > 0
    status = "ok" if completed.returncode == 0 and reached_http and 200 <= int(code) < 400 else "failed"
    emit(
        "http_curl", status, family=family_name, duration_ms=elapsed_ms(start),
        http_status=int(code) if code.isdigit() else 0, remote_ip=remote_ip,
        redirects=int(redirects) if redirects.isdigit() else None,
        final_url=safe_url(final_url), time_namelookup=dns_t, time_connect=connect_t,
        time_appconnect=tls_t, time_starttransfer=first_t, time_total=total_t,
        bytes=int(float(size)) if size else 0, content_type=content_type[:120],
        ssl_verify_result=int(verify) if verify.isdigit() else None,
        curl_exit=completed.returncode,
        error=CURL_ERRORS.get(completed.returncode) if completed.returncode else None,
    )
    return status == "ok"


def requests_error(exc: BaseException) -> str:
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return "connect_timeout"
    if isinstance(exc, requests.exceptions.ReadTimeout):
        return "read_timeout"
    if isinstance(exc, requests.exceptions.SSLError):
        return "tls_error"
    if isinstance(exc, requests.exceptions.TooManyRedirects):
        return "too_many_redirects"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "connection_error"
    if isinstance(exc, requests.exceptions.Timeout):
        return "timeout"
    return "request_error"


def probe_requests(url: str, expected_host: str) -> bool:
    start = time.monotonic()
    session = requests.Session()
    session.max_redirects = 5
    response = None
    try:
        response = session.get(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "ca,es;q=0.9",
            },
            allow_redirects=True,
            stream=True,
            timeout=(8, 20),
        )
        total = 0
        nonempty = False
        oversized = False
        deadline = time.monotonic() + 30
        for chunk in response.iter_content(64 * 1024):
            if chunk:
                nonempty = True
                total += len(chunk)
            if total > MAX_BODY:
                oversized = True
                break
            if time.monotonic() > deadline:
                raise requests.exceptions.ReadTimeout("bounded total read time exceeded")
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        final = urlsplit(response.url)
        redirect_chain = [
            {"status": item.status_code, "url": safe_url(item.url)}
            for item in response.history
        ]
        operational = (
            response.status_code == 200
            and final.scheme == "https"
            and final.hostname == expected_host
            and content_type in {"text/html", "application/xhtml+xml"}
            and nonempty
            and not oversized
        )
        emit(
            "http_python", "ok" if operational else "failed",
            duration_ms=elapsed_ms(start), http_status=response.status_code,
            final_url=safe_url(response.url), redirects=redirect_chain,
            content_type=content_type[:120], bytes_read=total,
            body_nonempty=nonempty, body_too_large=oversized,
            tls_validation=True, transport="environment",
        )
        return operational
    except requests.RequestException as exc:
        emit(
            "http_python", "failed", duration_ms=elapsed_ms(start),
            error=requests_error(exc), exception=type(exc).__name__, transport="environment",
            http_status=response.status_code if response is not None else None,
        )
        return False
    finally:
        if response is not None:
            response.close()
        session.close()


def run_diagnostics(url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("La URL de diagnóstico debe ser HTTPS.")
    port = parts.port or 443
    runtime_metadata()
    addresses = resolve(parts.hostname, port)
    tcp4 = probe_tcp(socket.AF_INET, addresses[socket.AF_INET], "IPv4")
    tcp6 = probe_tcp(socket.AF_INET6, addresses[socket.AF_INET6], "IPv6")
    probe_tls(parts.hostname, socket.AF_INET, tcp4, "IPv4")
    probe_tls(parts.hostname, socket.AF_INET6, tcp6, "IPv6")
    for family, name in ((socket.AF_INET, "IPv4"), (socket.AF_INET6, "IPv6")):
        if addresses[family] is None:
            emit("http_curl", "skipped", family=name, reason="dns_unknown")
        else:
            probe_curl(url, name, bool(addresses[family]))
    if addresses[socket.AF_INET] is None and addresses[socket.AF_INET6] is None:
        # El proxy puede resolver el nombre aunque falle el DNS directo de Codex.
        probe_curl(url, "environment", True)
    operational = probe_requests(url, parts.hostname)
    emit(
        "summary", "ok" if operational else "failed",
        source_usable_by_python=operational,
        note=(
            "HTTP 200 HTML no valida el selector; ejecute preview."
            if operational else
            "El diagnóstico no atribuye la causa a geobloqueo, bots, Azure ni certificados sin evidencia de fase."
        ),
    )
    return operational


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("RESULTS_URL", DEFAULT_URL))
    args = parser.parse_args()
    REPORT.clear()
    try:
        success = run_diagnostics(args.url)
        if os.environ.get("GITHUB_ACTIONS") == "true":
            summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
            if summary_path:
                with open(summary_path, "a", encoding="utf-8") as summary:
                    summary.write("### Diagnóstico del acceso JQCV\n\n```json\n")
                    summary.write(json.dumps(REPORT, ensure_ascii=False, indent=2))
                    summary.write("\n```\n")
            if not success:
                for record in REPORT:
                    if record["status"] == "failed" and record["stage"] != "summary":
                        detail = json.dumps(record, ensure_ascii=False, sort_keys=True)
                        detail = detail.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
                        print(f"::error title=Diagnóstico JQCV::{detail}", file=sys.stderr)
        return 0 if success else 1
    except ValueError as exc:
        emit("configuration", "failed", error="invalid_url", detail=str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
