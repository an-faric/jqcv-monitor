"""Pruebas sin peticiones a la JQCV y sin envío de correos reales."""

import contextlib
import io
import os
from pathlib import Path
import smtplib
import tempfile
import unittest
from unittest.mock import Mock, call, patch

import requests

import check_results as monitor


HTML = (Path(__file__).parent / "fixtures/results.html").read_text(encoding="utf-8")
NEW_HTML = HTML.replace(
    "</ul>", '<li><a href="/documents/123/final-c1.pdf">Resultados finales C1</a></li></ul>'
)


class ExtractionTests(unittest.TestCase):
    def test_only_results_are_monitored(self):
        changed = HTML.replace("Novedad fuera de resultados", "Otro menú").replace("09:00", "17:35")
        self.assertEqual(monitor.extract_content(HTML), monitor.extract_content(changed))

    def test_whitespace_and_auth_tokens_do_not_trigger_alerts(self):
        changed = HTML.replace("TOKEN1", "TOKEN2").replace("Resultados C1", "Resultados\n   C1")
        changed = changed.replace("Resultados C2", "Resultados&nbsp;C2")
        self.assertEqual(monitor.extract_content(HTML), monitor.extract_content(changed))

    def test_new_result_link_changes_the_snapshot(self):
        self.assertNotEqual(monitor.extract_content(HTML), monitor.extract_content(NEW_HTML))

    def test_changed_result_text_is_detected(self):
        changed = HTML.replace("Convocatoria de octubre", "Resultados definitivos de octubre")
        self.assertNotEqual(monitor.extract_content(HTML), monitor.extract_content(changed))

    def test_document_version_is_not_discarded(self):
        changed = HTML.replace("version=1.0", "version=2.0")
        self.assertNotEqual(monitor.extract_content(HTML), monitor.extract_content(changed))

    def test_urls_ignore_only_known_volatile_parameters(self):
        self.assertEqual(
            monitor.canonical_url("/documents/file;jsessionid=ABC?b=2&p_auth=TOKEN&a=1&utm_source=x#c1"),
            "https://jqcv.gva.es/documents/file?a=1&b=2#c1",
        )

    def test_missing_ambiguous_empty_and_invalid_selectors_fail(self):
        invalid_pages = [
            "<h1>Mantenimiento temporal</h1>",
            HTML + HTML,
            '<ul class="list-group show-quick-actions-on-hover"></ul>',
        ]
        for page in invalid_pages:
            with self.subTest(page=page[:40]), self.assertRaises(monitor.MonitorError):
                monitor.extract_content(page)
        with self.assertRaises(monitor.MonitorError):
            monitor.extract_content(HTML, "[")

    def test_custom_selector_can_disambiguate_two_lists(self):
        page = '<div id="resultados">' + HTML + '</div><div>' + HTML + '</div>'
        self.assertEqual(
            monitor.extract_content(page, "#resultados " + monitor.DEFAULT_SELECTOR),
            monitor.extract_content(HTML),
        )


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state = Path(self.temporary.name) / "last_content.txt"
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.fetch = patch.object(monitor, "fetch_html", return_value=HTML).start()
        self.email = patch.object(monitor, "send_email").start()
        self.addCleanup(patch.stopall)

    def run_monitor(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            monitor.run_check(self.state, **kwargs)

    def test_first_run_sends_activation_before_saving(self):
        self.email.side_effect = lambda *args: self.assertFalse(self.state.exists())
        self.run_monitor()
        self.email.assert_called_once()
        self.assertIn("monitor activado", self.email.call_args.args[0])
        self.assertEqual(self.state.read_text(encoding="utf-8"), monitor.extract_content(HTML))

    def test_no_change_sends_nothing_and_does_not_rewrite_file(self):
        self.state.write_text(monitor.extract_content(HTML), encoding="utf-8")
        before = self.state.stat().st_mtime_ns
        self.run_monitor()
        self.email.assert_not_called()
        self.assertEqual(self.state.stat().st_mtime_ns, before)

    def test_change_sends_diff_and_updates_state(self):
        self.state.write_text(monitor.extract_content(HTML), encoding="utf-8")
        self.fetch.return_value = NEW_HTML
        self.run_monitor()
        self.email.assert_called_once()
        self.assertIn("Resultados finales C1", self.email.call_args.args[1])
        self.assertEqual(self.state.read_text(encoding="utf-8"), monitor.extract_content(NEW_HTML))

    def test_smtp_failure_keeps_previous_state_for_retry(self):
        original = monitor.extract_content(HTML)
        self.state.write_text(original, encoding="utf-8")
        self.fetch.return_value = NEW_HTML
        self.email.side_effect = monitor.MonitorError("Fallo SMTP simulado")
        with self.assertRaises(monitor.MonitorError):
            self.run_monitor()
        self.assertEqual(self.state.read_text(encoding="utf-8"), original)
        self.assertEqual(list(self.state.parent.glob(".last_content.txt.*")), [])

    def test_first_run_smtp_failure_does_not_create_baseline(self):
        self.email.side_effect = monitor.MonitorError("Fallo SMTP simulado")
        with self.assertRaises(monitor.MonitorError):
            self.run_monitor()
        self.assertFalse(self.state.exists())

    def test_http_and_selector_errors_do_not_change_state_or_send_email(self):
        self.state.write_text("Estado anterior", encoding="utf-8")
        self.fetch.side_effect = monitor.MonitorError("HTTP 503 simulado")
        with self.assertRaises(monitor.MonitorError):
            self.run_monitor()
        self.fetch.side_effect = None
        self.fetch.return_value = "<h1>Servicio temporalmente no disponible</h1>"
        with self.assertRaises(monitor.MonitorError):
            self.run_monitor()
        self.assertEqual(self.state.read_text(encoding="utf-8"), "Estado anterior")
        self.email.assert_not_called()

    def test_preview_does_not_save_state_or_send_email(self):
        self.run_monitor(dry_run=True)
        self.assertFalse(self.state.exists())
        self.email.assert_not_called()

    def test_empty_state_fails_instead_of_resetting_silently(self):
        self.state.touch()
        with self.assertRaises(monitor.MonitorError):
            self.run_monitor()
        self.email.assert_not_called()
        self.assertEqual(self.state.read_text(), "")


class HttpTests(unittest.TestCase):
    @patch.object(monitor.requests, "Session")
    def test_http_error_is_reported(self, session_class):
        response = Mock(status_code=503)
        response.raise_for_status.side_effect = requests.HTTPError(response=response)
        session_class.return_value.__enter__.return_value.get.return_value = response
        with self.assertRaisesRegex(monitor.MonitorError, "503"):
            monitor.fetch_html()

    @patch.object(monitor.requests, "Session")
    def test_non_html_response_is_rejected(self, session_class):
        response = Mock(url=monitor.URL, headers={"Content-Type": "application/json"})
        session_class.return_value.__enter__.return_value.get.return_value = response
        with self.assertRaisesRegex(monitor.MonitorError, "HTML"):
            monitor.fetch_html()


class SmtpTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {
            "EMAIL_USER": "sender@example.com", "EMAIL_PASSWORD": "fake-test-password",
            "EMAIL_TO": "recipient@example.com",
        }, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    @patch.object(monitor.smtplib, "SMTP")
    def test_starttls_precedes_login_and_message(self, smtp_class):
        smtp = smtp_class.return_value.__enter__.return_value
        smtp.send_message.return_value = {}
        monitor.send_email("Prueba", "Contenido de prueba")
        methods = [entry[0] for entry in smtp.method_calls]
        self.assertLess(methods.index("starttls"), methods.index("login"))
        self.assertLess(methods.index("login"), methods.index("send_message"))
        self.assertEqual(smtp_class.call_args.args, ("smtp.gmail.com", 587))

    @patch.object(monitor.smtplib, "SMTP_SSL")
    def test_ssl_port_465_is_supported(self, smtp_class):
        os.environ["SMTP_SECURITY"] = "ssl"
        smtp = smtp_class.return_value.__enter__.return_value
        smtp.send_message.return_value = {}
        monitor.send_email("Prueba", "Contenido")
        self.assertEqual(smtp_class.call_args.args, ("smtp.gmail.com", 465))
        smtp.starttls.assert_not_called()

    @patch.object(monitor.smtplib, "SMTP")
    def test_missing_credentials_fail_before_connecting(self, smtp_class):
        del os.environ["EMAIL_PASSWORD"]
        with self.assertRaisesRegex(monitor.MonitorError, "EMAIL_PASSWORD"):
            monitor.send_email("Prueba", "Contenido")
        smtp_class.assert_not_called()

    @patch.object(monitor.smtplib, "SMTP")
    def test_smtp_error_does_not_expose_password(self, smtp_class):
        smtp = smtp_class.return_value.__enter__.return_value
        smtp.login.side_effect = smtplib.SMTPAuthenticationError(535, b"fake-test-password")
        with self.assertRaises(monitor.MonitorError) as caught:
            monitor.send_email("Prueba", "Contenido")
        self.assertNotIn("fake-test-password", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
