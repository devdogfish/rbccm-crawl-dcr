#!/usr/bin/env python3
"""Offline fixtures only. Run: python3 -m unittest discover -s . -v

Checks source-page discovery; cannot establish absence of orphan/auth/JS-only pages.
"""
from collections import Counter
from contextlib import contextmanager, redirect_stdout, redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().with_name("crawler.py")


@contextmanager
def fixture_site(routes):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, time.monotonic()))
            response = routes.get(self.path, (404, {}, "missing"))
            if isinstance(response, str):
                response = (200, {}, response)
            status, headers, body = response
            self.send_response(status)
            self.send_header("Content-Type", "application/xml" if self.path.endswith(".xml") else "text/html; charset=utf-8")
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def basic_routes(body="<p>Home</p>"):
    return {"/robots.txt": "User-agent: *\nAllow: /\n", "/sitemap.xml": "<urlset/>", "/": body}


class CrawlerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("local_dcr_crawler", SCRIPT)
        cls.module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.module
        spec.loader.exec_module(cls.module)

    def crawl(self, base, **options):
        output, errors = io.StringIO(), io.StringIO()
        settings = dict(delay_ms=0, max_requests=100, seeds=["/"], stdout=output, stderr=errors)
        settings.update(options)
        with tempfile.TemporaryDirectory(prefix="dcr-local-test-") as directory:
            destination = Path(directory) / "pages-with-dcr.txt"
            result = self.module.crawl(base + "/", base + "/sitemap.xml", destination, **settings)
            self.assertTrue(destination.exists())
            saved = destination.read_text().splitlines()
            self.assertEqual(saved, output.getvalue().splitlines(), "Saved/stdout source URL lists must agree")
            self.assertEqual(saved, result["matching_pages"])
            self.assertEqual(len(saved), len(set(saved)))
            self.assertTrue(all(line.startswith(base + "/") for line in saved), output.getvalue())
            return result, saved, errors.getvalue()

    def test_recursive_unsitemapped_links_wrapped_nested_maps_redirects_and_aliases(self):
        routes = basic_routes('<a href="/linked">not mapped</a><a href="/linked#part">alias</a><a href="/./linked">dot alias</a><a href="/source-alias">source redirect</a>')
        with fixture_site(routes) as (base, requests):
            routes["/sitemap.xml"] = f'<root xmlns="urn:rbc"><sitemapindex><sitemap><loc>{base}/nested.xml</loc></sitemap></sitemapindex></root>'
            routes["/nested.xml"] = f'<sitemapindex><sitemap><loc>{base}/leaf.xml</loc></sitemap></sitemapindex>'
            routes["/leaf.xml"] = f'<urlset><url><loc>{base}/listed</loc></url></urlset>'
            routes["/listed"] = '<a href="/linked">duplicate</a><!-- /article?DCR=old -->'
            routes["/linked"] = '<a href="/legacy?dcr=old&amp;topic=energy">legacy</a><a href="/">cycle</a>'
            routes["/legacy?dcr=old&topic=energy"] = (302, {"Location": "/clean"}, "")
            routes["/clean"] = "clean destination"
            routes["/source-alias"] = (302, {"Location": "/source"}, "")
            routes["/source"] = '<p>/old?dcr=source</p>'
            result, saved, errors = self.crawl(base)
        fetched = Counter(path for path, _ in requests)
        self.assertEqual(set(saved), {base + "/linked", base + "/listed", base + "/source"})
        for path in ("/linked", "/listed", "/nested.xml", "/leaf.xml", "/clean", "/source"):
            self.assertEqual(fetched[path], 1, (path, requests))
        self.assertNotIn(base + "/legacy?dcr=old&topic=energy", saved, "Output identifies source pages to fix")
        self.assertEqual(result["pending"], 0)

    def test_raw_source_entity_percent_and_js_decoding(self):
        cases = ("<a href='/old?DCR=upper'>old</a>", "<p>/old?%64%63%72=encoded</p>",
                 "<p>/old?dcr&#61;entity</p>", "<p>/old?topic=x&amp;dcr=entity</p>",
                 r'<script>const url="\/old?topic=x\u0026dcr\u003djs";</script>',
                 r'<script>const url="/old?topic=x\x26dcr\x3djs";</script>',
                 "<!-- /old?dcr=comment -->", "<div data-url='/old?dcr=attribute'></div>")
        for source in cases:
            with self.subTest(source=source):
                self.assertTrue(self.module.has_dcr(source))
        self.assertFalse(self.module.has_dcr("<p>Ordinary clean page</p>"))
        self.assertFalse(self.module.has_dcr("const dcr = pattern;"))

    def test_meaningful_query_values_preserved(self):
        routes = basic_routes('<a href="/article?topic=one">1</a><a href="/article?topic=two">2</a>')
        routes["/article?topic=one"] = "<p>?dcr=one</p>"
        routes["/article?topic=two"] = "<p>?dcr=two</p>"
        with fixture_site(routes) as (base, requests):
            result, saved, errors = self.crawl(base)
        self.assertEqual(set(saved), {base + "/article?topic=one", base + "/article?topic=two"})
        self.assertEqual(self.module.normalize_url("/article?payload=%26x%3Dy", base), base + "/article?payload=%26x%3Dy")

    def test_html_base_href_relative_discovery(self):
        routes = basic_routes('<base href="/section/"><a href="article">article</a>')
        routes["/section/article"] = "<p>?dcr=old</p>"
        with fixture_site(routes) as (base, requests):
            result, saved, errors = self.crawl(base)
        self.assertEqual(saved, [base + "/section/article"])

    def test_external_links_maps_redirects_never_fetched(self):
        with fixture_site(basic_routes()) as (external, external_requests):
            routes = basic_routes(f'<a href="{external}/foreign?dcr=old">external</a><a href="/bounce">bounce</a>')
            routes["/bounce"] = (302, {"Location": external + "/landing"}, "")
            routes["/sitemap.xml"] = f'<urlset><url><loc>{external}/mapped</loc></url></urlset>'
            with fixture_site(routes) as (base, requests):
                result, saved, errors = self.crawl(base)
            self.assertEqual(external_requests, [])
        self.assertEqual(saved, [base + "/"])
        self.assertNotEqual(result["status"], "finished")
        self.assertTrue(errors.strip())

    def test_request_spacing_includes_sitemaps_and_redirects(self):
        routes = basic_routes('<a href="/bounce">redirect</a>')
        routes["/bounce"] = (302, {"Location": "/destination"}, "")
        routes["/destination"] = "done"
        with fixture_site(routes) as (base, requests):
            result, saved, errors = self.crawl(base, delay_ms=30)
        self.assertGreaterEqual(len(requests), 4)
        gaps = [later[1] - earlier[1] for earlier, later in zip(requests, requests[1:])]
        self.assertTrue(all(gap >= 0.02 for gap in gaps), gaps)
        self.assertEqual(result["requests"], len(requests))

    def test_remote_network_minimum_one_second(self):
        network = self.module.Network("https://example.invalid/", delay_ms=0)
        self.assertGreaterEqual(network.delay_ms, 1000)

    def test_request_guard_reports_incomplete_with_pending(self):
        with fixture_site(basic_routes()) as (base, requests):
            result, saved, errors = self.crawl(base, max_requests=1)
        self.assertLessEqual(len(requests), 1)
        self.assertEqual(result["requests"], len(requests))
        self.assertEqual(result["status"], "incomplete")
        self.assertGreater(result["pending"], 0)
        self.assertTrue(errors.strip())

    def test_429_stops_once_and_saves_existing_matches(self):
        routes = basic_routes('<p>?dcr=old</p><a href="/busy">busy</a><a href="/after">after</a>')
        routes["/busy"] = (429, {"Retry-After": "3600"}, "slow down")
        routes["/after"] = "after"
        with fixture_site(routes) as (base, requests):
            started = time.monotonic()
            result, saved, errors = self.crawl(base)
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5)
        self.assertEqual(Counter(path for path, _ in requests)["/busy"], 1)
        self.assertNotIn("/after", [path for path, _ in requests])
        self.assertEqual(saved, [base + "/"])
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("429", errors)

    def test_503_stops_without_continuing_requests(self):
        routes = basic_routes('<a href="/busy">busy</a><a href="/after">after</a>')
        routes["/busy"] = (503, {"Retry-After": "3600"}, "slow down")
        routes["/after"] = "after"
        with fixture_site(routes) as (base, requests):
            result, saved, errors = self.crawl(base)
        self.assertEqual(Counter(path for path, _ in requests)["/busy"], 1)
        self.assertNotIn("/after", [path for path, _ in requests])
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("503", errors)

    def test_url_guard_reports_partial_coverage(self):
        routes = basic_routes('<a href="/next">next</a>')
        with patch.object(self.module, "MAX_URLS", 2):
            with fixture_site(routes) as (base, requests):
                result, saved, errors = self.crawl(base)
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("URL guard", errors)
        self.assertNotIn("/next", [path for path, _ in requests])

    def test_errors_go_to_stderr_sources_still_saved(self):
        routes = basic_routes('<p>?dcr=old</p><a href="/missing">missing</a>')
        with fixture_site(routes) as (base, requests):
            result, saved, errors = self.crawl(base)
        self.assertEqual(saved, [base + "/"])
        self.assertEqual(result["status"], "incomplete")
        self.assertGreater(result["errors"], 0)
        self.assertIn("404", errors)

    def test_oversized_response_reports_incomplete(self):
        with patch.object(self.module, "MAX_BYTES", 1000):
            with fixture_site(basic_routes("<html>" + "x" * 5000 + "</html>")) as (base, requests):
                result, saved, errors = self.crawl(base)
        self.assertEqual(result["status"], "incomplete")
        self.assertTrue(errors.strip())

    def test_interrupt_saves_existing_matches(self):
        routes = basic_routes('<p>?dcr=old</p><a href="/stop">stop</a>')
        original = self.module.Network.fetch

        def interrupted(network, url, *args, **kwargs):
            if url.endswith("/stop"):
                raise KeyboardInterrupt
            return original(network, url, *args, **kwargs)

        with patch.object(self.module.Network, "fetch", interrupted):
            with fixture_site(routes) as (base, requests):
                result, saved, errors = self.crawl(base)
        self.assertEqual(saved, [base + "/"])
        self.assertEqual(result["status"], "incomplete")
        self.assertTrue(errors.strip())

    def test_zero_option_cli_rejects_arguments_before_network(self):
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(self.module, "crawl") as mocked, redirect_stdout(output), redirect_stderr(errors):
            status = self.module.main(["--full"])
        mocked.assert_not_called()
        self.assertNotEqual(status, 0)
        self.assertEqual(output.getvalue(), "")
        self.assertTrue(errors.getvalue().strip())


if __name__ == "__main__":
    unittest.main(verbosity=2)
