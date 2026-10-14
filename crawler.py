#!/usr/bin/env python3
"""Find RBC pages whose HTML source mentions legacy ?dcr= or &dcr= URLs.

Start with the sitemap and a few known pages, then follow links from every new page.
Print/save each SOURCE page containing a DCR mention, one URL per line. Requests
are sequential and at least 1000 ms apart; Ctrl-C preserves saved findings.
Reads HTML source, including scripts/comments; cannot find unlinked or private pages.
"""
import collections
import gzip
import html
from html.parser import HTMLParser
import io
import ipaddress
from pathlib import Path
import re
import sys
import time
import urllib.error
from urllib import parse as urlparse
from urllib import request as urlrequest
from xml.etree import ElementTree

BASE_URL = 'https://www.rbccm.com/'
SITEMAP_URL = BASE_URL + 'en/sitemap.page'
OUTPUT_PATH = Path(__file__).resolve().with_name('pages-with-dcr.txt')
SEEDS_FILE = Path(__file__).resolve().with_name('seeds.txt')
REQUEST_DELAY_MS = 1000
MAX_REQUESTS = 25_000
MAX_URLS = 50_000
MAX_SITEMAPS = 50
TIMEOUT = 15
MAX_BYTES = 2_000_000
USER_AGENT = 'RBC-DCR-Audit/1.0'  # Identify this audit in server logs.
DCR_QUERY = re.compile(r'[?&]dcr=', re.I)
ASSET = re.compile(r'\.(?:pdf|png|jpe?g|gif|svg|webp|ico|avif|mp[34]|mov|zip|gz|xlsx?|docx?|pptx?|csv|woff2?|ttf|css|js|json|xml)$', re.I)
QUOTED_URL = re.compile(r'''["']((?:https?://|https?:\\/\\/|/|\\/|\./|\.\./)[^"'\s<>]+)["']''', re.I)

# Decode repeated escaping so the same DCR link is found in HTML, URLs, or JavaScript.
def decode_reference(value):
    for _ in range(3):
        previous = value
        value = re.sub(r'\\u([0-9a-fA-F]{4})|\\x([0-9a-fA-F]{2})',
                       lambda m: chr(int(m.group(1) or m.group(2), 16)), value)
        value = value.replace(r'\/', '/').replace(r'\&', '&').replace(r'\?', '?').replace(r'\=', '=')
        value = html.unescape(urlparse.unquote(value))
        if value == previous:  # No further encoding layer to unwrap.
            break
    return value


def has_dcr(source):
    return bool(DCR_QUERY.search(decode_reference(source)))  # Search all source, not just links.


def normalize_url(value, base_url, scope_url=None):
    """Resolve a discovered link, keep its query, drop its fragment, and enforce scope."""
    try:
        value = re.sub(r'\\u([0-9a-fA-F]{4})|\\x([0-9a-fA-F]{2})',
                       lambda m: chr(int(m.group(1) or m.group(2), 16)), value.strip())
        value = html.unescape(value.replace('\\/', '/'))
        if re.match(r'^(?:https?%3a|%2f)', value, re.I):  # Decode wrappers, not query values.
            value = urlparse.unquote(value)
        parts = urlparse.urlsplit(urlparse.urljoin(base_url, value))
        if (not value or parts.scheme not in ('http', 'https') or parts.username or parts.password
                or any(character in value for character in '\r\n\x00')):
            return None
        url = urlparse.urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or '/', parts.query, ''))
        if scope_url:
            def origin(parts):
                port = parts.port or (443 if parts.scheme == 'https' else 80)
                return parts.scheme.lower(), (parts.hostname or '').lower(), port
            if origin(parts) != origin(urlparse.urlsplit(scope_url)):
                return None
        return url
    except (ValueError, TypeError):
        return None

# Gather links from HTML attributes and quoted source text for recursive discovery.
class PageLinks(HTMLParser):
    def __init__(self, source_url):
        super().__init__(convert_charrefs=True)
        self.base_url = source_url
        self.references = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if not value:
                continue
            if tag == 'base' and name == 'href':  # Later relative links use this base.
                self.base_url = normalize_url(value, self.base_url) or self.base_url
            elif (name in ('href', 'action', 'data-href', 'data-url', 'data-link', 'data-target-url')
                  or (name == 'src' and tag in ('iframe', 'frame'))):
                self.references.append(value)
            elif name.startswith(('data-', 'on')):
                self.handle_data(value)

    handle_startendtag = handle_starttag

    def handle_data(self, data):
        self.references.extend(match.group(1) for match in QUOTED_URL.finditer(data))

    def urls(self, source):
        self.feed(source)
        self.close()
        resolved = (normalize_url(value, self.base_url) for value in self.references)
        return [url for url in resolved if url]


def sitemap_urls(source):
    """Separate child sitemaps from page URLs, even in nested/namespaced XML."""
    sitemaps, pages = [], []
    for node in ElementTree.fromstring(source).iter():
        kind = node.tag.rsplit('}', 1)[-1].lower()  # Strip XML namespace.
        if kind in ('sitemap', 'url'):
            for child in node:
                if child.tag.rsplit('}', 1)[-1].lower() == 'loc' and child.text:
                    (sitemaps if kind == 'sitemap' else pages).append(child.text.strip())
    return sitemaps, pages


class CrawlStopped(Exception):
    """Stop the whole crawl; ordinary failures skip only the current URL."""


class ManualRedirects(urlrequest.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Handle redirects ourselves so every hop is rate-limited.


class Network:
    """Fetch safely: one shared pace/limit for pages, sitemaps, and redirect hops."""
    def __init__(self, base_url, max_requests=MAX_REQUESTS, delay_ms=REQUEST_DELAY_MS, timeout=TIMEOUT, max_bytes=MAX_BYTES):
        self.base_url = base_url
        self.max_requests, self.timeout, self.max_bytes = max_requests, timeout, max_bytes
        try:
            loopback = ipaddress.ip_address(urlparse.urlsplit(base_url).hostname).is_loopback
        except ValueError:
            loopback = urlparse.urlsplit(base_url).hostname == 'localhost'
        self.delay_ms = delay_ms if loopback else max(REQUEST_DELAY_MS, delay_ms)  # Fast local tests only.
        self.requests = 0
        self.last_start = None
        self.opener = urlrequest.build_opener(ManualRedirects())

    def fetch(self, url):
        redirect_chain = set()  # Track this fetch's hops to stop redirect cycles.
        for _ in range(11):  # Initial request plus at most ten redirects.
            if not normalize_url(url, self.base_url, self.base_url):
                raise ValueError('external redirect skipped')
            if url in redirect_chain:
                raise ValueError('redirect cycle')
            if self.requests >= self.max_requests:
                raise CrawlStopped('request guard reached')
            if self.last_start is not None:
                time.sleep(max(0, self.delay_ms / 1000 - (time.monotonic() - self.last_start)))
            self.last_start = time.monotonic()
            self.requests += 1
            redirect_chain.add(url)
            request = urlrequest.Request(url, headers={'User-Agent': USER_AGENT, 'Accept-Encoding': 'identity'})
            try:
                response = self.opener.open(request, timeout=self.timeout)
            except urllib.error.HTTPError as exc:
                response = exc  # urllib wraps redirect/error responses in HTTPError.
            with response:
                status = response.code
                if status in (429, 503):
                    retry = response.headers.get('Retry-After')
                    raise CrawlStopped(f'HTTP {status}; Retry-After: {retry or "not supplied"}')
                if status in (301, 302, 303, 307, 308):
                    url = normalize_url(response.headers.get('Location', ''), url)
                    if not url:
                        raise ValueError('invalid redirect')
                    continue
                if status >= 400:
                    raise ValueError(f'HTTP {status}')
                body = response.read(self.max_bytes + 1)
                if len(body) > self.max_bytes:
                    raise ValueError('response byte guard reached')
                if response.headers.get('Content-Encoding', '').lower() == 'gzip' or body[:2] == b'\x1f\x8b':
                    with gzip.GzipFile(fileobj=io.BytesIO(body)) as stream:
                        body = stream.read(self.max_bytes + 1)  # Limit decompressed size too.
                    if len(body) > self.max_bytes:
                        raise ValueError('decompressed byte guard reached')
                try:
                    source = body.decode(response.headers.get_content_charset() or 'utf-8', 'replace')
                except LookupError:
                    source = body.decode('utf-8', 'replace')
                return url, response.headers.get_content_type(), source
        raise ValueError('redirect guard reached')

# Run the discovery queue: inspect every newly found page and add its links.
def crawl(base_url=BASE_URL, sitemap_url=SITEMAP_URL, output_path=OUTPUT_PATH, *,
          delay_ms=REQUEST_DELAY_MS, max_requests=MAX_REQUESTS, seeds=None, stdout=None, stderr=None):
    stdout, stderr = stdout or sys.stdout, stderr or sys.stderr
    network = Network(base_url, max_requests, delay_ms, TIMEOUT, MAX_BYTES)
    pending, scheduled, inspected_pages = collections.deque(), set(), set()  # Queue; seen links; fetched pages.
    matches, error_count, page_count, sitemap_count = [], 0, 0, 0
    stop_reason = None
    url_limit_hit = False

    def enqueue(value, kind='page', relative_to=base_url):
        """Schedule each in-scope URL once, without following static assets."""
        nonlocal url_limit_hit
        url = normalize_url(value, relative_to, base_url)
        if not url or (kind == 'page' and ASSET.search(urlparse.urlsplit(url).path)) or (url, kind) in scheduled:
            return
        if len(scheduled) >= MAX_URLS:
            url_limit_hit = True
            return
        scheduled.add((url, kind))
        pending.append((url, kind))

    # Seed known sections; the sitemap adds URLs that ordinary navigation may miss.
    for seed in seeds if seeds is not None else ('/', '/en/', '/en/insights'):
        enqueue(seed)
    if seeds is None and SEEDS_FILE.exists():
        for line in SEEDS_FILE.read_text(encoding='utf-8').splitlines():
            if line.strip() and not line.lstrip().startswith('#'):
                enqueue(line.strip())
    enqueue(sitemap_url, 'sitemap')
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    active_item = None  # Remember interrupted work for the final coverage report.
    with output_path.open('w', encoding='utf-8') as output:
        try:
            while pending:
                # Prefer sitemaps, then process linked pages until no discovered URL remains.
                item = next((item for item in pending if item[1] == 'sitemap'), pending[0])
                pending.remove(item)
                active_item = item
                url, kind = item
                if kind == 'page' and url in inspected_pages:
                    active_item = None
                    continue
                try:
                    if kind == 'sitemap' and sitemap_count >= MAX_SITEMAPS:
                        raise CrawlStopped('sitemap guard reached')
                    final_url, content_type, source = network.fetch(url)
                    if kind == 'sitemap':  # Its locations can be more sitemaps or pages.
                        sitemap_count += 1
                        child_sitemaps, listed_pages = sitemap_urls(source)
                        for child in child_sitemaps:
                            enqueue(child, 'sitemap', final_url)
                        for page in listed_pages:
                            enqueue(page, 'page', final_url)
                    elif final_url not in inspected_pages:  # Inspect each destination only once.
                        if 'html' not in content_type:
                            raise ValueError('non-HTML page skipped: ' + content_type)
                        page_count += 1
                        if has_dcr(source):  # Save this source page, not the legacy target URL.
                            matches.append(final_url)
                            output.write(final_url + '\n')
                            output.flush()  # Keep each finding even if the crawl is interrupted.
                            print(final_url, file=stdout, flush=True)
                        for link in PageLinks(final_url).urls(source):  # Feed new links back into the queue.
                            enqueue(link, relative_to=final_url)
                        inspected_pages.update((url, final_url))  # Deduplicate redirected page aliases.
                    else:
                        inspected_pages.add(url)
                except CrawlStopped:
                    pending.appendleft(item)
                    raise
                except Exception as exc:
                    error_count += 1
                    print(f'Skipped {url}: {exc}', file=stderr)
                active_item = None
        except CrawlStopped as exc:
            stop_reason = str(exc)
        except KeyboardInterrupt:
            stop_reason = 'interrupted'
            if active_item and active_item not in pending:
                pending.appendleft(active_item)
    if url_limit_hit:
        stop_reason = (stop_reason + '; ' if stop_reason else '') + 'URL guard reached'
    incomplete = bool(stop_reason or error_count or pending)  # Finished means discovery exhausted, not full-site proof.
    result = {'matching_pages': matches, 'requests': network.requests, 'pages': page_count,
              'pending': len(pending), 'errors': error_count, 'status': 'incomplete' if incomplete else 'finished', 'stop_reason': stop_reason}
    print(f'{result["status"]}: {len(matches)} matching pages; {page_count} HTML pages; '
          f'{network.requests} requests; {error_count} errors; {len(pending)} pending.'
          + (f' {stop_reason}.' if stop_reason else '')
          + ' Coverage: discovered public pages only.', file=stderr)
    return result

# Entry point: no CLI options; report partial coverage through the exit status.
def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        print('Usage: python3 crawler.py (no options)', file=sys.stderr)
        return 0 if argv in (['--help'], ['-h']) else 2
    try:
        result = crawl()
        return 1 if result['status'] == 'incomplete' else 0
    except (OSError, ValueError) as exc:
        print('Stopped: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
