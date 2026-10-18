"""Pruebas sintéticas: no acreditan acceso real a la web ni envío SMTP."""

from __future__ import annotations

import contextlib
import io
import json
import os
import smtplib
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import requests

import check_results as monitor

HTML = (Path(__file__).parent / "fixtures/results.html").read_text(encoding="utf-8")
CONFIG = monitor.MonitorConfig()
NOW = datetime(2026, 10, 18, 12, 30, tzinfo=timezone.utc)


def add_item(html: str, item: str) -> str:
    return html.replace("</ul>", item + "</ul>", 1)


def synthetic_page(items: str, *, year: str = "2026") -> str:
    return f"""<!doctype html>
<!-- Fixture sintética generada por la prueba. -->
<html><body><main><h2>Convocatòria {year}</h2>
<ul class="list-group show-quick-actions-on-hover" data-year="{year}">{items}</ul>
</main></body></html>"""


def result_item(
    title: str = "Resultats provisionals C1 - convocatòria 2026",
    href: str = "/documents/123/resultats-c1.pdf?version=1.0",
) -> str:
    return f'<li><a href="{href}">{title}</a></li>'


class ConfigurationTests(unittest.TestCase):
    def test_default_scope_is_c1_2026_and_does_not_follow_current_year(self):
        with patch.dict(os.environ, {}, clear=True):
            config = monitor.MonitorConfig.from_environment()
        self.assertEqual(config.scope_key, "C1:2026")

    def test_scope_is_configurable_and_validated(self):
        with patch.dict(os.environ, {"TARGET_LEVEL": "C2", "TARGET_YEAR": "2027"}, clear=True):
            self.assertEqual(monitor.MonitorConfig.from_environment().scope_key, "C2:2027")
        for environment in ({"TARGET_LEVEL": "C-1"}, {"TARGET_YEAR": "26"}, {"RESULTS_URL": "http://x"}):
            with self.subTest(environment=environment), patch.dict(os.environ, environment, clear=True):
                with self.assertRaises(monitor.MonitorError):
                    monitor.MonitorConfig.from_environment()

    def test_email_configuration_does_not_depend_on_source_selectors(self):
        with patch.dict(os.environ, {
            "RESULTS_URL": "not-a-url", "RESULTS_SELECTOR": "", "RESULTS_ITEM_SELECTOR": ""
        }, clear=True):
            config = monitor.MonitorConfig.from_environment(validate_source=False)
        self.assertEqual(config.target_level, "C1")


class ExtractionTests(unittest.TestCase):
    def test_extracts_only_configured_level_and_year_with_metadata(self):
        result = monitor.extract_publications(HTML, CONFIG)
        self.assertEqual(result.candidate_count, 4)
        self.assertEqual(len(result.publications), 1)
        publication = result.publications[0]
        self.assertEqual(publication.level, "C1")
        self.assertEqual(publication.year, "2026")
        self.assertEqual(publication.phase, "provisional")
        self.assertEqual(publication.kind, "results")
        self.assertEqual(publication.official_published_at, "2026-10-16T09:00:00+02:00")
        self.assertIn("version=1.0", publication.url)
        self.assertNotIn("p_auth", publication.url)

    def test_other_levels_years_and_generic_c1_mentions_are_ignored(self):
        titles = [publication.title for publication in monitor.extract_publications(HTML, CONFIG).publications]
        self.assertEqual(titles, ["Resultats provisionals C1"])

    def test_valid_block_with_only_other_levels_returns_empty_result(self):
        page = synthetic_page(result_item("Resultados finales C2 2026", "/c2.pdf"))
        result = monitor.extract_publications(page, CONFIG)
        self.assertEqual(result.candidate_count, 1)
        self.assertEqual(result.publications, ())

    def test_new_c1_publication_is_extracted(self):
        changed = add_item(
            HTML,
            result_item("Resultados finales C1 - convocatoria 2026", "/documents/124/final-c1.pdf"),
        )
        result = monitor.extract_publications(changed, CONFIG)
        self.assertEqual(len(result.publications), 2)
        self.assertIn("final", {publication.phase for publication in result.publications})

    def test_multilevel_publication_is_kept_when_it_explicitly_includes_c1(self):
        page = synthetic_page(result_item(
            "Resultados definitivos niveles B2, C1 y C2 - convocatoria 2026",
            "/documents/all-levels.pdf",
        ))
        result = monitor.extract_publications(page, CONFIG)
        self.assertEqual(len(result.publications), 1)

    def test_same_official_resource_is_deduplicated(self):
        page = synthetic_page(
            result_item(href="/documents/123/resultats-c1.pdf?utm_source=a")
            + result_item(href="/documents/123/resultats-c1.pdf?utm_source=b")
        )
        result = monitor.extract_publications(page, CONFIG)
        self.assertEqual(len(result.publications), 1)

    def test_date_announcement_is_distinct_and_keeps_official_wording(self):
        page = synthetic_page("""
          <li><a href="/es/comunicat-c1">Comunicació oficial C1 2026</a>
          <p>Els resultats del C1 es publicaran el 30 d'octubre de 2026.</p></li>
        """)
        publication = monitor.extract_publications(page, CONFIG).publications[0]
        self.assertEqual(publication.kind, "date_announcement")
        self.assertIn("30 d'octubre de 2026", publication.announced_timing)
        self.assertIsNone(publication.official_published_at)

    def test_unlabelled_time_is_not_called_an_official_publication_date(self):
        page = synthetic_page("""
          <li><time datetime="2026-10-16">Data de la prova</time>
          <a href="/c1.pdf">Resultados finales C1 2026</a></li>
        """)
        publication = monitor.extract_publications(page, CONFIG).publications[0]
        self.assertIsNone(publication.official_published_at)

    def test_generic_date_or_c1_reference_is_not_an_announcement(self):
        page = synthetic_page("""
          <li><a href="/requisits">El C1 es requisito para las pruebas de octubre de 2026</a></li>
        """)
        self.assertEqual(monitor.extract_publications(page, CONFIG).publications, ())

    def test_generic_reference_does_not_inherit_result_status_from_heading(self):
        page = synthetic_page(result_item("C1 es requisito para otras pruebas 2026", "/requisitos"))
        page = page.replace("Convocatòria 2026</h2>", "Resultados convocatoria 2026</h2>")
        self.assertEqual(monitor.extract_publications(page, CONFIG).publications, ())

    def test_future_publication_without_concrete_timing_is_not_available_results(self):
        for text in (
            "Los resultados C1 2026 se publicarán próximamente.",
            "Los resultados C1 2026 se publicarán durante el plazo previsto.",
        ):
            with self.subTest(text=text):
                page = synthetic_page(result_item(text))
                self.assertEqual(monitor.extract_publications(page, CONFIG).publications, ())

    def test_call_year_is_distinct_from_future_publication_year(self):
        page = synthetic_page(result_item(
            "Resultados C1 convocatoria 2026: se publicarán el 12 de enero de 2027."
        ))
        publication = monitor.extract_publications(page, CONFIG).publications[0]
        self.assertEqual(publication.year, "2026")
        self.assertEqual(publication.kind, "date_announcement")
        self.assertIn("2027", publication.announced_timing)

    def test_foreign_links_are_not_presented_as_official_results(self):
        page = synthetic_page(result_item(href="https://example.com/unverified.pdf"))
        self.assertEqual(monitor.extract_publications(page, CONFIG).publications, ())

    def test_previous_entry_heading_does_not_leak_level_to_next_entry(self):
        page = synthetic_page(
            '<li><h3>Resultados C1 2026</h3><a href="/c1">Consulta</a></li>'
            '<li><a href="/general">Resultados 2026</a></li>'
        )
        self.assertEqual(len(monitor.extract_publications(page, CONFIG).publications), 1)

    def test_phase_variants_in_spanish_and_valencian(self):
        cases = {
            "Resultats parcials C1 2026": "partial",
            "Qualificacions provisionals C1 2026": "provisional",
            "Resultados finales C1 2026": "final",
            "Resultats definitius C1 2026": "definitive",
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                publication = monitor.extract_publications(
                    synthetic_page(result_item(title)), CONFIG
                ).publications[0]
                self.assertEqual(publication.phase, expected)

    def test_cosmetic_changes_and_volatile_tokens_do_not_change_fingerprint(self):
        changed = HTML.replace("TOKEN1", "TOKEN2").replace(
            "Resultats provisionals C1", "  RESULTATS\n provisionals   C1 "
        ).replace("Último acceso: 09:00", "Último acceso: 17:35")
        original = monitor.extract_publications(HTML, CONFIG).publications[0]
        current = monitor.extract_publications(changed, CONFIG).publications[0]
        self.assertEqual(original.stable_id, current.stable_id)
        self.assertEqual(
            monitor._publication_fingerprint(original),
            monitor._publication_fingerprint(current),
        )

    def test_document_version_is_significant_but_identity_is_stable(self):
        changed = HTML.replace("version=1.0", "version=2.0")
        original = monitor.extract_publications(HTML, CONFIG).publications[0]
        current = monitor.extract_publications(changed, CONFIG).publications[0]
        self.assertEqual(original.stable_id, current.stable_id)
        self.assertNotEqual(
            monitor._publication_fingerprint(original),
            monitor._publication_fingerprint(current),
        )

    def test_url_normalization_removes_only_known_volatile_parameters(self):
        self.assertEqual(
            monitor.canonical_url(
                "/documents/file;jsessionid=ABC?b=2&p_auth=TOKEN&a=1&utm_source=x&version=3#c1"
            ),
            "https://jqcv.gva.es/documents/file?a=1&b=2&version=3#c1",
        )

    def test_missing_ambiguous_empty_and_invalid_selectors_fail_closed(self):
        cases = [
            "<h1>Mantenimiento temporal</h1>",
            HTML + HTML,
            '<ul class="list-group show-quick-actions-on-hover"></ul>',
        ]
        for page in cases:
            with self.subTest(page=page[:40]), self.assertRaises(monitor.MonitorError):
                monitor.extract_publications(page, CONFIG)
        with self.assertRaises(monitor.MonitorError):
            monitor.extract_publications(HTML, monitor.MonitorConfig(selector="["))
        with self.assertRaises(monitor.MonitorError):
            monitor.extract_publications(HTML, monitor.MonitorConfig(item_selector="["))

    def test_custom_selector_can_disambiguate_two_blocks(self):
        page = '<div id="target">' + HTML + "</div>" + HTML
        config = monitor.MonitorConfig(selector="#target " + monitor.DEFAULT_SELECTOR)
        self.assertEqual(len(monitor.extract_publications(page, config).publications), 1)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state = Path(self.temporary.name) / "last_content.txt"
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.fetch = patch.object(monitor, "fetch_html", return_value=HTML).start()
        self.email = patch.object(monitor, "send_email").start()
        self.addCleanup(patch.stopall)

    def run_monitor(self, **kwargs):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            monitor.run_check(self.state, config=CONFIG, now=NOW, **kwargs)
        return output.getvalue()

    def initialize(self):
        self.run_monitor()
        self.email.reset_mock()

    def read_state(self):
        return json.loads(self.state.read_text(encoding="utf-8"))

    def test_first_run_sends_clear_initialization_before_saving(self):
        self.email.side_effect = lambda *_: self.assertFalse(self.state.exists())
        self.run_monitor()
        subject, body = self.email.call_args.args
        self.assertIn("monitor inicializado", subject)
        self.assertIn("situación ya existente", body)
        self.assertIn("2026-10-18 14:30:00 CEST", body)
        self.assertIn("2026-10-18T12:30:00+00:00", body)
        state = self.read_state()
        self.assertEqual(state["schema_version"], 2)
        self.assertIn("C1:2026", state["scopes"])

    def test_first_run_smtp_failure_does_not_create_state(self):
        self.email.side_effect = monitor.MonitorError("Fallo SMTP simulado")
        with self.assertRaises(monitor.MonitorError):
            self.run_monitor()
        self.assertFalse(self.state.exists())

    def test_no_change_sends_nothing_and_does_not_rewrite_state(self):
        self.initialize()
        before_bytes = self.state.read_bytes()
        before_time = self.state.stat().st_mtime_ns
        self.assertIn("Sin cambios relevantes", self.run_monitor())
        self.email.assert_not_called()
        self.assertEqual(self.state.read_bytes(), before_bytes)
        self.assertEqual(self.state.stat().st_mtime_ns, before_time)

    def test_new_c1_publication_sends_results_notice_and_is_persisted(self):
        self.initialize()
        self.fetch.return_value = add_item(
            HTML,
            result_item("Resultados finales C1 - convocatoria 2026", "/documents/124/final-c1.pdf"),
        )
        self.run_monitor()
        subject, body = self.email.call_args.args
        self.assertIn("resultados disponibles", subject)
        self.assertIn("Nueva publicación", body)
        items = self.read_state()["scopes"]["C1:2026"]["items"]
        self.assertEqual(len(items), 2)

    def test_relevant_link_version_change_sends_update(self):
        self.initialize()
        self.fetch.return_value = HTML.replace("version=1.0", "version=2.0")
        self.run_monitor()
        _, body = self.email.call_args.args
        self.assertIn("Cambio relevante", body)
        self.assertIn("version=2.0", body)

    def test_phase_transition_is_identified(self):
        self.initialize()
        self.fetch.return_value = HTML.replace(
            "Resultats provisionals C1", "Resultats definitius C1"
        ).replace(
            "/documents/123/resultats-c1.pdf?p_auth=TOKEN1&amp;version=1.0&amp;utm_source=test",
            "/documents/456/resultats-definitius-c1.pdf?version=1.0",
        )
        self.run_monitor()
        _, body = self.email.call_args.args
        self.assertIn("Cambio de fase", body)
        self.assertIn("provisional -> definitiva", body)

    def test_date_announcement_has_its_own_subject(self):
        empty = synthetic_page(result_item("Resultados finales C2 2026", "/c2.pdf"))
        self.fetch.return_value = empty
        self.run_monitor()
        self.email.reset_mock()
        self.fetch.return_value = add_item(empty, """
          <li><a href="/es/comunicat-c1">Comunicació oficial C1 2026</a>
          Els resultats del C1 es publicaran el 30 d'octubre de 2026.</li>
        """)
        self.run_monitor()
        subject, body = self.email.call_args.args
        self.assertIn("anuncio oficial", subject)
        self.assertIn("Fecha o plazo anunciado", body)
        self.assertIn("30 d'octubre", body)

    def test_smtp_failure_keeps_pending_change_and_previous_state(self):
        self.initialize()
        previous = self.state.read_bytes()
        self.fetch.return_value = HTML.replace("version=1.0", "version=2.0")
        self.email.side_effect = monitor.MonitorError("Fallo SMTP simulado")
        with self.assertRaises(monitor.MonitorError):
            self.run_monitor()
        self.assertEqual(self.state.read_bytes(), previous)
        self.assertEqual(list(self.state.parent.glob(".last_content.txt.*")), [])

    def test_empty_extraction_does_not_replace_nonempty_state(self):
        self.initialize()
        previous = self.state.read_bytes()
        self.fetch.return_value = synthetic_page(result_item("Resultados finales C2 2026", "/c2.pdf"))
        with self.assertRaisesRegex(monitor.MonitorError, "no contiene publicaciones C1"):
            self.run_monitor()
        self.assertEqual(self.state.read_bytes(), previous)
        self.email.assert_not_called()

    def test_preview_has_no_email_or_state_side_effect(self):
        output = self.run_monitor(preview=True)
        self.assertIn("Publicaciones pertinentes: 1", output)
        self.assertIn("no se ha enviado correo", output)
        self.assertFalse(self.state.exists())
        self.email.assert_not_called()

    def test_legacy_state_is_migrated_silently_and_fingerprinted(self):
        legacy = monitor._legacy_snapshot(HTML, CONFIG)
        self.state.write_text(legacy, encoding="utf-8")
        output = self.run_monitor()
        self.assertIn("migrado", output)
        self.email.assert_not_called()
        scope = self.read_state()["scopes"]["C1:2026"]
        self.assertIn("migrated_legacy_sha256", scope)
        self.assertEqual(len(scope["migrated_legacy_sha256"]), 64)
        self.assertEqual(scope["legacy_snapshot"], legacy)

    def test_different_legacy_state_is_preserved_for_review(self):
        legacy = "TEXTO\nReferencia antigua\n\nENLACES\nConsulta -> https://jqcv.gva.es/old\n"
        self.state.write_text(legacy, encoding="utf-8")
        with self.assertRaisesRegex(monitor.MonitorError, "difiere"):
            self.run_monitor()
        self.assertEqual(self.state.read_text(), legacy)
        self.email.assert_not_called()

    def test_publication_body_change_is_relevant_even_when_link_and_title_are_identical(self):
        self.initialize()
        self.fetch.return_value = HTML.replace(
            "<span>Convocatòria 2026</span>",
            "<span>Convocatòria 2026. Corrección de las calificaciones publicadas.</span>",
        )
        self.run_monitor()
        self.email.assert_called_once()

    def test_old_version_reappearing_does_not_send_duplicate_notice(self):
        self.initialize()
        self.fetch.return_value = HTML.replace("version=1.0", "version=2.0")
        self.run_monitor()
        self.email.reset_mock()
        previous = self.state.read_bytes()
        self.fetch.return_value = HTML
        self.run_monitor()
        self.email.assert_not_called()
        self.assertEqual(self.state.read_bytes(), previous)

    def test_reordering_entries_has_no_effect(self):
        self.initialize()
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(HTML, "html.parser")
        block = soup.select_one(CONFIG.selector)
        entries = [entry.extract() for entry in block.select(":scope > li")]
        for entry in reversed(entries):
            block.append(entry)
        self.fetch.return_value = str(soup)
        previous = self.state.read_bytes()
        self.run_monitor()
        self.email.assert_not_called()
        self.assertEqual(self.state.read_bytes(), previous)

    def test_empty_or_unknown_state_is_never_reset(self):
        for content in ("", "contenido desconocido"):
            with self.subTest(content=content):
                self.state.write_text(content, encoding="utf-8")
                with self.assertRaises(monitor.MonitorError):
                    self.run_monitor()
                self.assertEqual(self.state.read_text(encoding="utf-8"), content)
                self.email.assert_not_called()

    def test_existing_other_year_scope_is_preserved(self):
        state = {"schema_version": 2, "scopes": {"C1:2025": {
            "target_level": "C1", "target_year": "2025", "items": {}
        }}}
        self.state.write_text(json.dumps(state), encoding="utf-8")
        self.run_monitor()
        scopes = self.read_state()["scopes"]
        self.assertIn("C1:2025", scopes)
        self.assertIn("C1:2026", scopes)


class HttpTests(unittest.TestCase):
    def response(self, **overrides):
        values = {
            "status_code": 200,
            "url": monitor.URL,
            "headers": {"Content-Type": "text/html; charset=utf-8"},
            "content": b"<html><body>ok</body></html>",
        }
        values.update(overrides)
        response = Mock(**values)
        response.raise_for_status.return_value = None
        return response

    @patch.object(monitor.requests, "Session")
    def test_http_error_keeps_last_status(self, session_class):
        response = self.response(status_code=503)
        response.raise_for_status.side_effect = requests.HTTPError(response=response)
        session_class.return_value.__enter__.return_value.get.return_value = response
        with self.assertRaisesRegex(monitor.MonitorError, "HTTP 503"):
            monitor.fetch_html(CONFIG)

    @patch.object(monitor.requests, "Session")
    def test_connect_read_and_tls_failures_are_distinguished(self, session_class):
        cases = [
            (requests.ConnectTimeout("x"), "conexión"),
            (requests.ReadTimeout("x"), "lectura"),
            (requests.exceptions.SSLError("x"), "TLS"),
        ]
        getter = session_class.return_value.__enter__.return_value.get
        for error, message in cases:
            with self.subTest(error=type(error).__name__):
                getter.side_effect = error
                with self.assertRaisesRegex(monitor.MonitorError, message):
                    monitor.fetch_html(CONFIG)

    @patch.object(monitor.requests, "Session")
    def test_non_html_empty_and_external_redirects_are_rejected(self, session_class):
        getter = session_class.return_value.__enter__.return_value.get
        cases = [
            (self.response(headers={"Content-Type": "application/json"}), "HTML"),
            (self.response(content=b"  "), "vacía"),
            (self.response(url="https://example.com/results"), "host oficial"),
        ]
        for response, message in cases:
            with self.subTest(message=message):
                getter.side_effect = None
                getter.return_value = response
                with self.assertRaisesRegex(monitor.MonitorError, message):
                    monitor.fetch_html(CONFIG)


class SmtpTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {
            "EMAIL_USER": "sender@example.com",
            "EMAIL_PASSWORD": "fake-test-password",
            "EMAIL_TO": "recipient@example.com",
        }, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    @patch.object(monitor.smtplib, "SMTP")
    def test_starttls_precedes_login_and_message(self, smtp_class):
        smtp = smtp_class.return_value.__enter__.return_value
        smtp.send_message.return_value = {}
        monitor.send_email("Prueba", "Contenido")
        methods = [entry[0] for entry in smtp.method_calls]
        self.assertLess(methods.index("starttls"), methods.index("login"))
        self.assertLess(methods.index("login"), methods.index("send_message"))
        self.assertEqual(smtp_class.call_args.args, ("smtp.gmail.com", 587))

    @patch.object(monitor.smtplib, "SMTP_SSL")
    def test_tls_implicit_port_465_is_supported(self, smtp_class):
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
    def test_smtp_error_never_exposes_password(self, smtp_class):
        smtp = smtp_class.return_value.__enter__.return_value
        smtp.login.side_effect = smtplib.SMTPAuthenticationError(535, b"fake-test-password")
        with self.assertRaises(monitor.MonitorError) as caught:
            monitor.send_email("Prueba", "Contenido")
        self.assertNotIn("fake-test-password", str(caught.exception))

    @patch.object(monitor, "send_email")
    def test_test_message_is_clearly_labelled(self, send):
        with contextlib.redirect_stdout(io.StringIO()):
            monitor.send_test_email(CONFIG)
        subject, body = send.call_args.args
        self.assertTrue(subject.startswith("[PRUEBA]"))
        self.assertIn("no anuncia resultados", body)


if __name__ == "__main__":
    unittest.main()
