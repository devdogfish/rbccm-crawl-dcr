# Pages containing legacy DCR links

Python 3.10+, no dependencies or options. Run:

```sh
python3 crawler.py
```

The code follows the crawl's flow:

1. `decode_reference` / `has_dcr`: recognize encoded DCR mentions in source.
2. `normalize_url`, `PageLinks`, `sitemap_urls`: discover and scope URLs.
3. `Network`: fetch pages with shared pacing, limits, and redirect checks.
4. `crawl`: manage the queue, avoid duplicate pages, and save matching URLs.
5. `main`: start the crawl and return its exit status.

Prints **source-page URLs**, one per line, and saves them beside the script in `pages-with-dcr.txt`. These are pages whose returned HTML contains `?dcr=` or `&dcr=` after entity, percent, and common JavaScript escape decoding; matching is case-insensitive. Mentions in scripts/comments also count. Output is source pages, not the old link targets.

Discovers pages through RBC's nested/wrapped sitemap indexes and recursive same-origin links/inline URLs. Optional `seeds.txt` beside the script adds known URLs, one per line (`#` comments allowed).

Set `REQUEST_DELAY_MS` near the top of `crawler.py` in milliseconds. It defaults to 1000 ms, and remote requests are never spaced closer than 1000 ms, including sitemap and redirect requests. The script uses the sitemap directly for this authorized maintenance audit, including paths excluded by robots; it does not enforce robots rules.

Errors and coverage summary go to stderr. Stops on 429/503, Ctrl-C, or finite guards (25,000 requests, 50,000 discovered URLs, 50 sitemaps; 2 MB/response and 15-second socket-operation timeout). Findings are saved incrementally. Honor a reported `Retry-After` before rerunning. Each run starts fresh and overwrites the output.

Coverage includes discovered public HTML only; unlinked pages need seeds. No JavaScript execution, authenticated pages, or external assets. Exact-origin scope excludes HTTP variants, other hostnames/subdomains, and external redirects. Exhausting discovery does not prove every site page was inspected. Large runs can take hours.

Local tests, no live requests:

```sh
python3 -m unittest discover -s . -p 'test_crawler.py' -v
```
