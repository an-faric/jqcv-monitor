"""Pruebas sintéticas del diagnóstico; no realizan conexiones reales."""

from __future__ import annotations

import contextlib
import io
import os
import socket
import ssl
import sys
import unittest
from unittest.mock import MagicMock, Mock, patch

import requests

import diagnose_access as diagnose


class SanitizingTests(unittest.TestCase):
    def test_safe_url_removes_credentials_query_fragment_and_session(self):
        self.assertEqual(
            diagnose.safe_url(
                "https://user:secret@jqcv.gva.es/es/resultats;jsessionid=ABC?token=secret#part"
            ),
            "https://jqcv.gva.es/es/resultats",
        )

    @patch.object(diagnose.subprocess, "run")
    def test_runtime_reports_only_proxy_variable_names(self, run):
        run.return_value = Mock(stdout="curl 8.0 test\n", returncode=0)
        output = io.StringIO()
        with patch.dict(os.environ, {"HTTPS_PROXY": "https://user:secret@proxy.invalid"}, clear=True):
            with contextlib.redirect_stdout(output):
                diagnose.runtime_metadata()
        text = output.getvalue()
        self.assertIn("HTTPS_PROXY", text)
        self.assertNotIn("user", text)
        self.assertNotIn("secret", text)


class DnsAndTransportTests(unittest.TestCase):
    @patch.object(diagnose.socket, "getaddrinfo")
    def test_a_only_dns_marks_ipv6_not_applicable(self, getaddrinfo):
        getaddrinfo.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("192.0.2.10", 443)),
        ]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            addresses = diagnose.resolve("jqcv.gva.es", 443)
            result = diagnose.probe_tcp(socket.AF_INET6, addresses[socket.AF_INET6], "IPv6")
        self.assertIsNone(result)
        self.assertIn('"status": "not_applicable"', output.getvalue())
        self.assertIn("no_ipv6_dns", output.getvalue())

    @patch.object(diagnose.ssl, "create_default_context")
    @patch.object(diagnose.socket, "socket")
    def test_tls_uses_default_validation_and_sni(self, socket_class, context_factory):
        raw = MagicMock()
        raw.__enter__.return_value = raw
        socket_class.return_value = raw
        secure = MagicMock()
        secure.__enter__.return_value = secure
        secure.version.return_value = "TLSv1.3"
        secure.cipher.return_value = ("TLS_AES_256_GCM_SHA384", "TLSv1.3", 256)
        context = MagicMock(check_hostname=True, verify_mode=ssl.CERT_REQUIRED)
        context.wrap_socket.return_value = secure
        context_factory.return_value = context
        with contextlib.redirect_stdout(io.StringIO()):
            diagnose.probe_tls(
                "jqcv.gva.es", socket.AF_INET, ("192.0.2.10", 443), "IPv4"
            )
        context.wrap_socket.assert_called_once_with(raw, server_hostname="jqcv.gva.es")
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)


class CurlTests(unittest.TestCase):
    @patch.object(diagnose.subprocess, "run")
    def test_curl_is_bounded_forces_family_and_never_disables_tls(self, run):
        run.return_value = Mock(
            returncode=0,
            stdout=(
                "200\t192.0.2.10\t1\thttps://jqcv.gva.es/es/resultats?ignored=1\t"
                "0.01\t0.02\t0.03\t0.04\t0.05\t1234\ttext/html\t0"
            ),
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(diagnose.probe_curl(diagnose.DEFAULT_URL, "IPv4", True))
        command = run.call_args.args[0]
        self.assertIn("--ipv4", command)
        self.assertIn("--max-time", command)
        self.assertIn("--max-redirs", command)
        self.assertNotIn("-k", command)
        self.assertNotIn("--insecure", command)
        self.assertNotIn("--verbose", command)

    @patch.object(diagnose.subprocess, "run")
    def test_curl_timeout_is_classified_without_stderr(self, run):
        run.return_value = Mock(returncode=28, stdout="", stderr="contains-private-proxy-detail")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertFalse(diagnose.probe_curl(diagnose.DEFAULT_URL, "IPv4", True))
        self.assertIn('"error": "timeout"', output.getvalue())
        self.assertNotIn("private-proxy", output.getvalue())


class RequestsTests(unittest.TestCase):
    def response(self, **overrides):
        response = Mock()
        response.status_code = overrides.get("status_code", 200)
        response.url = overrides.get("url", diagnose.DEFAULT_URL)
        response.history = overrides.get("history", [])
        response.headers = overrides.get("headers", {"Content-Type": "text/html; charset=utf-8"})
        response.iter_content.return_value = overrides.get("chunks", [b"<html>ok</html>"])
        return response

    @patch.object(diagnose.requests, "Session")
    def test_python_http_success_requires_usable_official_html(self, session_class):
        session_class.return_value.get.return_value = self.response()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            success = diagnose.probe_requests(diagnose.DEFAULT_URL, "jqcv.gva.es")
        self.assertTrue(success)
        kwargs = session_class.return_value.get.call_args.kwargs
        self.assertTrue(kwargs["stream"])
        self.assertEqual(kwargs["timeout"], (8, 20))
        self.assertNotIn("verify", kwargs)
        self.assertIn('"tls_validation": true', output.getvalue())

    @patch.object(diagnose.requests, "Session")
    def test_http_status_non_html_empty_and_external_redirect_fail(self, session_class):
        cases = [
            self.response(status_code=503),
            self.response(headers={"Content-Type": "application/json"}),
            self.response(chunks=[]),
            self.response(url="https://example.com/resultats"),
        ]
        for response in cases:
            with self.subTest(status=response.status_code, url=response.url):
                session_class.return_value.get.return_value = response
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertFalse(diagnose.probe_requests(diagnose.DEFAULT_URL, "jqcv.gva.es"))

    @patch.object(diagnose.requests, "Session")
    def test_request_failures_are_classified_without_exception_text(self, session_class):
        cases = [
            (requests.ConnectTimeout("secret-connect"), "connect_timeout"),
            (requests.ReadTimeout("secret-read"), "read_timeout"),
            (requests.exceptions.SSLError("secret-tls"), "tls_error"),
        ]
        for error, expected in cases:
            with self.subTest(error=expected):
                session_class.return_value.get.side_effect = error
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertFalse(diagnose.probe_requests(diagnose.DEFAULT_URL, "jqcv.gva.es"))
                self.assertIn(expected, output.getvalue())
                self.assertNotIn("secret-", output.getvalue())


class OrchestrationTests(unittest.TestCase):
    @patch.object(diagnose, "probe_requests", return_value=True)
    @patch.object(diagnose, "probe_curl", return_value=False)
    @patch.object(diagnose, "probe_tls")
    @patch.object(diagnose, "probe_tcp", return_value=None)
    @patch.object(diagnose, "resolve")
    @patch.object(diagnose, "runtime_metadata")
    def test_python_success_is_operational_even_when_ipv6_is_not_applicable(
        self, runtime, resolve, tcp, tls, curl, python_http
    ):
        resolve.return_value = {socket.AF_INET: [], socket.AF_INET6: []}
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertTrue(diagnose.run_diagnostics(diagnose.DEFAULT_URL))
        self.assertIn('"source_usable_by_python": true', output.getvalue())
        python_http.assert_called_once()

    @patch.object(diagnose, "run_diagnostics", return_value=False)
    def test_main_returns_failure_when_source_is_not_usable(self, run):
        with patch.object(sys, "argv", ["diagnose_access.py"]):
            self.assertEqual(diagnose.main(), 1)


if __name__ == "__main__":
    unittest.main()
