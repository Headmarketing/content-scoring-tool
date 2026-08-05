"""
crawler.py — discovers the list of URLs to audit.
Supports: sitemap.xml, a plain CSV/text list of URLs, or full BFS site crawl.
The BFS crawler works for any type of website (not just blogs) and can
discover up to 8,000 pages by following all internal same-domain HTML links.
"""
import csv
import time
import requests
from collections import deque
from urllib.parse import urljoin, urlparse, urlunparse
from xml.etree import ElementTree as ET
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT = 20

# Non-HTML file extensions to skip during BFS crawl
_SKIP_EXTS = {
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".avif",
    ".mp4", ".mp3", ".avi", ".mov", ".wmv", ".flv", ".ogg", ".wav",
    ".zip", ".tar", ".gz", ".rar", ".7z", ".bz2",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".css", ".js", ".json", ".xml", ".rss", ".atom", ".txt",
    ".ico", ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".map", ".ts", ".jsx", ".tsx",
}

# URL path substrings that indicate non-content/admin paths — skip during BFS
_SKIP_PATH_SUBSTRINGS = {
    "/wp-json/", "/wp-admin/", "/wp-login", "/wp-cron",
    "/wp-content/uploads/", "/feed/", "/cdn-cgi/",
    "/.well-known/", "/xmlrpc", "/trackback/",
    "__trashed", "/embed/", "/print/",
}


def _normalize_url(url: str) -> str:
    """Remove fragment, strip trailing slash (except root), normalise scheme/host."""
    p = urlparse(url)
    path = p.path.rstrip("/") or "/"
    # Drop default ports
    netloc = p.netloc.lower()
    if netloc.endswith(":80") and p.scheme == "http":
        netloc = netloc[:-3]
    elif netloc.endswith(":443") and p.scheme == "https":
        netloc = netloc[:-4]
    return urlunparse((p.scheme.lower(), netloc, path, p.params, p.query, ""))


def _same_domain(url: str, domain: str) -> bool:
    """True if url belongs to domain or its www/non-www twin."""
    netloc = urlparse(url).netloc.lower()
    bare = domain.lower().removeprefix("www.")
    return netloc in (bare, f"www.{bare}")


def _is_crawlable(url: str, domain: str) -> bool:
    """Return True if the URL looks like a crawlable same-domain HTML page."""
    if not _same_domain(url, domain):
        return False
    p = urlparse(url)
    # Check extension of the last path segment
    last_seg = p.path.split("/")[-1]
    if "." in last_seg:
        ext = "." + last_seg.rsplit(".", 1)[-1].lower()
        if ext in _SKIP_EXTS:
            return False
    # Check for admin/infrastructure path patterns
    full_path = p.path.lower()
    for seg in _SKIP_PATH_SUBSTRINGS:
        if seg in full_path:
            return False
    return True


# ── Sitemap parser ────────────────────────────────────────────────────────────

def urls_from_sitemap(sitemap_url: str, max_urls: int = 8000) -> list[str]:
    """Parses a sitemap.xml (or sitemap index) and returns all page URLs up to max_urls."""
    urls: list[str] = []
    try:
        resp = requests.get(sitemap_url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
    except Exception:
        return urls

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError:
        return urls

    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

    # Sitemap index → recurse into child sitemaps
    sitemap_children = root.findall("sm:sitemap/sm:loc", ns)
    if sitemap_children:
        for loc in sitemap_children:
            if len(urls) >= max_urls:
                break
            time.sleep(0.2)
            try:
                child_urls = urls_from_sitemap(loc.text.strip(), max_urls=max_urls - len(urls))
                urls.extend(child_urls)
            except Exception:
                continue
        return urls[:max_urls]

    for loc in root.findall("sm:url/sm:loc", ns):
        if loc.text:
            urls.append(loc.text.strip())
        if len(urls) >= max_urls:
            break
    return urls[:max_urls]


# ── CSV / text-file reader ────────────────────────────────────────────────────

def urls_from_csv(path: str) -> list[str]:
    """Reads a CSV or plain text file — one URL per line or first column."""
    urls: list[str] = []
    with open(path, newline="", encoding="utf-8") as f:
        sample = f.read(2048)
        f.seek(0)
        if "," in sample:
            reader = csv.reader(f)
            for row in reader:
                if row and row[0].strip().startswith("http"):
                    urls.append(row[0].strip())
        else:
            for line in f:
                line = line.strip()
                if line.startswith("http"):
                    urls.append(line)
    return urls


# ── BFS site crawler ──────────────────────────────────────────────────────────

def urls_from_site_crawl(start_url: str, max_urls: int = 8000, delay: float = 0.15) -> list[str]:
    """
    Full breadth-first site crawler.

    Starting from start_url, follows all same-domain internal HTML links up to
    max_urls pages. Handles any type of website — not limited to blogs.

    Politeness:
      - delay (seconds) between each HTTP request (default 0.15 s)
      - Non-HTML assets and admin/infrastructure paths are skipped
      - Redirects are followed and the final URL is stored
    """
    parsed_start = urlparse(start_url)
    domain = parsed_start.netloc  # e.g. "www.example.com"

    found: list[str] = []      # ordered list of discovered URLs (in crawl order)
    seen: set[str] = set()     # normalised URLs already enqueued or visited

    queue: deque[str] = deque()
    norm_start = _normalize_url(start_url)
    queue.append(norm_start)
    seen.add(norm_start)

    while queue and len(found) < max_urls:
        url = queue.popleft()
        found.append(url)

        try:
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
            content_type = resp.headers.get("Content-Type", "")
            if "text/html" not in content_type:
                continue
            resp.raise_for_status()
        except Exception:
            continue

        soup = BeautifulSoup(resp.text, "html.parser")

        for a in soup.find_all("a", href=True):
            raw_href = a["href"].split("#")[0].strip()
            if not raw_href:
                continue
            if raw_href.lower().startswith(("javascript:", "mailto:", "tel:", "data:")):
                continue

            full = urljoin(url, raw_href)
            if not full.startswith(("http://", "https://")):
                continue
            if not _is_crawlable(full, domain):
                continue

            norm = _normalize_url(full)
            if norm not in seen:
                seen.add(norm)
                # Only enqueue if there's still budget left
                if len(found) + len(queue) < max_urls:
                    queue.append(norm)

        time.sleep(delay)

    return found[:max_urls]


# ── Public entry point ────────────────────────────────────────────────────────

def discover_urls(site_url=None, sitemap_url=None, csv_path=None,
                  max_urls: int = 8000) -> list[str]:
    """
    Main discovery entry point. Provide exactly one of:
      - sitemap_url : sitemap.xml (or sitemap index) URL
      - csv_path    : path to a CSV / plain-text file of URLs
      - site_url    : any page on the site to BFS-crawl from (usually the homepage)
    """
    if csv_path:
        return urls_from_csv(csv_path)[:max_urls]
    if sitemap_url:
        return urls_from_sitemap(sitemap_url, max_urls=max_urls)
    if site_url:
        return urls_from_site_crawl(site_url, max_urls=max_urls)
    raise ValueError("Provide one of: site_url, sitemap_url, csv_path")
