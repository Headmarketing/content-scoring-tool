"""
extractor.py — fetches one URL and extracts everything the audit needs:
metadata, content structure, media, links, technical/schema signals,
and page-type classification.

Page types detected: blog | product | service | home | about | contact |
                     community | category | landing | generic

Alt-text policy:
  - Only flags images where the `alt` attribute is ENTIRELY ABSENT.
  - `alt=""` is intentionally valid for decorative images and is NOT flagged.
  - Images with role="presentation" / aria-hidden="true" are skipped.
  - 1×1 tracking / spacer images are skipped.
"""
import re
import json
import requests
from urllib.parse import urlparse
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT = 20


# ── Helpers ───────────────────────────────────────────────────────────────────

def _text(el) -> str:
    return el.get_text(" ", strip=True) if el else ""


def _word_count(text: str) -> int:
    return len(re.findall(r"\b[\w'-]+\b", text))


def _needs_alt_text(img) -> bool:
    """
    Returns True if this <img> is a content image that genuinely requires an
    alt attribute.

    Skips:
      - Images explicitly marked as decorative via role="presentation"/"none"
        or aria-hidden="true"  → alt="" is correct for these
      - 1×1 tracking/spacer pixels  → not content
      - Images already carrying alt="" intentionally (attribute IS present,
        just empty) — this is correct HTML for decorative images
    """
    role = (img.get("role") or "").lower()
    if role in ("presentation", "none"):
        return False
    if (img.get("aria-hidden") or "").lower() == "true":
        return False
    # Skip known 1×1 tracking pixels
    try:
        if int(img.get("width", 10)) <= 1 and int(img.get("height", 10)) <= 1:
            return False
    except (ValueError, TypeError):
        pass
    return True


# ── Page-type detection ───────────────────────────────────────────────────────

# URL path patterns (checked after schema-based detection)
_PAGE_TYPE_PATH_PATTERNS: dict[str, re.Pattern] = {
    "blog":      re.compile(r"/(blog|article|articles|news|post|posts|insights|resources|press-release|editorial)/", re.I),
    "product":   re.compile(r"/(product|products|shop|store|item|items|catalogue|catalog|p)/", re.I),
    "service":   re.compile(r"/(service|services|solution|solutions|offering|offerings|capabilities|expertise)/", re.I),
    "about":     re.compile(r"/(about|about-us|our-story|our-team|team|who-we-are|company|mission|vision)/", re.I),
    "contact":   re.compile(r"/(contact|contact-us|reach-us|get-in-touch|reach-out)/", re.I),
    "community": re.compile(r"/(community|forum|forums|discussion|discussions|board|qa|q-a|questions|answers)/", re.I),
    "category":  re.compile(r"/(category|categories|tag|tags|archive|topic|topics)/", re.I),
    "landing":   re.compile(r"/(lp|landing|campaign|promo|offer|deals?|trial|free-trial)/", re.I),
}

# Schema @type values that strongly indicate a page type
_PAGE_TYPE_SCHEMA: dict[str, set[str]] = {
    "blog":      {"article", "blogposting", "newsarticle", "techarticle", "scholarlyarticle", "liveblogposting"},
    "product":   {"product", "productgroup", "offer", "aggregateoffer", "vehicle", "book"},
    "service":   {"service", "financialproduct", "plumbingservice", "hvacbusiness"},
    "about":     {"aboutpage"},
    "contact":   {"contactpage"},
    "community": {"discussionforumposting", "qapage", "question", "answer"},
    "home":      {"website", "sitelinksearchbox"},
}


def detect_page_type(url: str, soup, schema_types: list) -> str:
    """
    Classifies a page into one of:
      blog | product | service | home | about | contact | community |
      category | landing | generic

    Detection priority:
      1. Root path → home  (most unambiguous)
      2. Schema @type      (explicitly declared by site owner — highest confidence)
      3. URL path patterns (reliable for well-structured sites)
      4. HTML structural signals (last resort)
    """
    path = urlparse(url).path.lower().rstrip("/") or "/"
    schema_lower = {s.lower() for s in schema_types}

    # 1. Home page: URL is the domain root
    if path in ("", "/"):
        return "home"

    # 2. Schema-based detection
    for ptype, schema_set in _PAGE_TYPE_SCHEMA.items():
        if schema_lower & schema_set:
            return ptype

    # Organization / LocalBusiness schema without a more specific match → about
    if schema_lower & {"organization", "corporation", "localbusiness", "nonprofit"}:
        # Only classify as "about" if the path also has about-like segment
        if re.search(r"/(about|company|team|mission|vision|who-we-are)", path):
            return "about"

    # 3. URL path patterns
    for ptype, pattern in _PAGE_TYPE_PATH_PATTERNS.items():
        if pattern.search(url):
            return ptype

    # Also check slug-like endings (e.g. /contact, /about-us without trailing content)
    last_seg = path.strip("/").split("/")[-1].lower()
    if last_seg in ("contact", "contact-us", "reach-us", "get-in-touch"):
        return "contact"
    if last_seg in ("about", "about-us", "our-story", "team", "company"):
        return "about"

    # 4. HTML structural fallback signals
    if _has_contact_signals(soup):
        return "contact"
    if _has_product_signals(soup):
        return "product"
    if _has_article_signals(soup):
        return "blog"

    return "generic"


def _has_contact_signals(soup) -> bool:
    """Detect contact pages via form + email/phone/message input fields."""
    for form in soup.find_all("form"):
        inputs = form.find_all(["input", "textarea", "select"])
        for inp in inputs:
            t = inp.get("type", "").lower()
            attrs = " ".join([
                inp.get("name", ""), inp.get("placeholder", ""),
                inp.get("id", ""), inp.get("class", "") if isinstance(inp.get("class"), str) else "",
            ]).lower()
            if t == "email" or "email" in attrs or "phone" in attrs or "message" in attrs:
                return True
            if inp.name == "textarea":
                return True
    return False


def _has_product_signals(soup) -> bool:
    """Detect product pages via price markup, cart buttons, or product microdata."""
    if soup.find(attrs={"itemprop": "price"}) or soup.find(attrs={"itemprop": "offers"}):
        return True
    # Heuristic: currency symbol + number anywhere on the page
    page_text = soup.get_text(" ", strip=True)
    if re.search(r"[\$£€₹¥₩]\s*\d[\d.,]*", page_text):
        return True
    # Add-to-cart buttons
    for el in soup.find_all(["button", "a", "input"]):
        text = (el.get_text(" ", strip=True) + " " + (el.get("value") or "")).lower()
        if any(kw in text for kw in ["add to cart", "add to bag", "buy now", "purchase", "order now"]):
            return True
    return False


def _has_article_signals(soup) -> bool:
    """Detect blog/article pages via structural HTML signals."""
    if soup.find("article"):
        return True
    if soup.find(attrs={"class": re.compile(r"\b(post|article|entry|blog-post)\b", re.I)}):
        return True
    if soup.find(attrs={"class": re.compile(r"\b(author|byline|posted-by)\b", re.I)}):
        return True
    return False


# ── Schema helpers ────────────────────────────────────────────────────────────

def _normalize_type(t: str) -> str:
    """'https://schema.org/Article' -> 'Article'; handles trailing slashes and case."""
    if not isinstance(t, str):
        return t
    t = t.strip().rstrip("/")
    if "schema.org/" in t.lower():
        t = t.split("/")[-1]
    return t


def _recursive_collect_types(obj, out: list):
    """Walks the entire JSON-LD structure collecting every @type value at any depth.
    Catches common nesting patterns produced by Yoast/RankMath/etc:
      {"@type":"WebPage","mainEntity":{"@type":"BlogPosting"}}
    """
    if isinstance(obj, dict):
        t = obj.get("@type")
        if t:
            if isinstance(t, list):
                out.extend(_normalize_type(x) for x in t)
            else:
                out.append(_normalize_type(t))
        for v in obj.values():
            _recursive_collect_types(v, out)
    elif isinstance(obj, list):
        for item in obj:
            _recursive_collect_types(item, out)


def _find_schema_types(soup) -> list[str]:
    types: list[str] = []
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue
        data = None
        try:
            data = json.loads(raw)
        except Exception:
            # Common breakage: trailing commas, stray control chars
            cleaned = re.sub(r",\s*([}\]])", r"\1", raw)
            cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", cleaned)
            try:
                data = json.loads(cleaned)
            except Exception:
                data = None

        if data is not None:
            _recursive_collect_types(data, types)
        else:
            # Last resort regex pull so one bad script tag doesn't kill the rest
            for m in re.finditer(r'"@type"\s*:\s*"([^"]+)"', raw):
                types.append(_normalize_type(m.group(1)))

    # Fallback: microdata itemtype (older WooCommerce/legacy themes)
    if not types:
        for tag in soup.find_all(attrs={"itemtype": True}):
            itemtype = tag.get("itemtype", "")
            if "schema.org" in itemtype:
                norm = _normalize_type(itemtype)
                if norm:
                    types.append(norm)

    return types


# ── Content helpers ───────────────────────────────────────────────────────────

def _find_date(soup, keys) -> str:
    for key in keys:
        tag = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"itemprop": key})
        if tag and tag.get("content"):
            return tag["content"]
    time_tag = soup.find("time")
    if time_tag and time_tag.get("datetime"):
        return time_tag["datetime"]
    return ""


def _detect_faq(scope) -> bool:
    text = _text(scope).lower()
    if "frequently asked question" in text:
        return True
    headings = scope.find_all(re.compile("^h[2-4]$"))
    question_headings = sum(1 for h in headings if _text(h).strip().endswith("?"))
    return question_headings >= 2


def _recursive_has_key(obj, key) -> bool:
    """Walks a nested JSON-LD structure looking for a truthy field anywhere."""
    if isinstance(obj, dict):
        if key in obj and obj[key]:
            return True
        return any(_recursive_has_key(v, key) for v in obj.values())
    if isinstance(obj, list):
        return any(_recursive_has_key(item, key) for item in obj)
    return False


def _detect_author(soup) -> bool:
    """
    Checks (in order): <meta name="author">, byline class/id patterns,
    rel="author", itemprop="author", and JSON-LD 'author' field recursively.
    """
    meta_author = soup.find("meta", attrs={"name": "author"})
    if meta_author and meta_author.get("content", "").strip():
        return True

    byline_pattern = re.compile(r"\b(author|byline|written-?by|posted-?by|post-author)\b", re.I)
    if soup.find(attrs={"class": byline_pattern}) or soup.find(attrs={"id": byline_pattern}):
        return True

    if soup.find(attrs={"rel": "author"}) or soup.find(attrs={"itemprop": "author"}):
        return True

    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except Exception:
            cleaned = re.sub(r",\s*([}\]])", r"\1", raw)
            cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", cleaned)
            try:
                data = json.loads(cleaned)
            except Exception:
                continue
        if _recursive_has_key(data, "author"):
            return True

    return False


def _detect_contact_form(soup) -> bool:
    """True if a form with an email/phone/message field or textarea exists."""
    for form in soup.find_all("form"):
        for inp in form.find_all(["input", "textarea"]):
            if inp.name == "textarea":
                return True
            t = inp.get("type", "").lower()
            attrs = " ".join([inp.get("name", ""), inp.get("placeholder", ""), inp.get("id", "")]).lower()
            if t == "email" or any(kw in attrs for kw in ("email", "phone", "message", "mobile")):
                return True
    return False


def _detect_add_to_cart(soup) -> bool:
    """True if an add-to-cart / buy button exists on the page."""
    kws = ("add to cart", "add to bag", "buy now", "purchase", "order now", "add to basket")
    for el in soup.find_all(["button", "a", "input"]):
        text = (el.get_text(" ", strip=True) + " " + (el.get("value") or "")).lower()
        if any(kw in text for kw in kws):
            return True
    return False


# ── Main extractor ────────────────────────────────────────────────────────────

def extract_page(url: str) -> dict:
    """
    Returns a flat dict of every field needed by the audit, or
    an {'url':..., 'error':...} dict if the page could not be fetched.
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
    except Exception as e:
        return {"url": url, "error": str(e)}

    soup = BeautifulSoup(resp.text, "html.parser")
    domain = urlparse(url).netloc

    # ── Metadata ────────────────────────────────────────────────────────────
    title = _text(soup.find("title"))
    meta_desc_tag = soup.find("meta", attrs={"name": "description"})
    meta_description = meta_desc_tag["content"].strip() if meta_desc_tag and meta_desc_tag.get("content") else ""
    canonical_tag = soup.find("link", attrs={"rel": "canonical"})
    canonical = canonical_tag["href"] if canonical_tag and canonical_tag.get("href") else ""

    publish_date = _find_date(soup, ["article:published_time", "datePublished"])
    updated_date = _find_date(soup, ["article:modified_time", "dateModified"])

    # ── Headings ────────────────────────────────────────────────────────────
    headings = {f"h{i}": [_text(h) for h in soup.find_all(f"h{i}")] for i in range(1, 7)}

    # ── Main content extraction ──────────────────────────────────────────────
    # Priority: <article> > role="main" > <main> > <body>
    main = soup.find("article") or soup.find(attrs={"role": "main"}) or soup.find("main") or soup.body
    for tag in (main or soup).find_all(["nav", "footer", "header", "script", "style", "aside"]):
        tag.decompose()
    main_text = _text(main) if main else _text(soup)
    word_count = _word_count(main_text)
    reading_time_min = max(1, round(word_count / 200))

    paragraphs = (main or soup).find_all("p")
    paragraph_texts = [_text(p) for p in paragraphs if _text(p)]
    lists = (main or soup).find_all(["ul", "ol"])
    tables = (main or soup).find_all("table")

    faq_present = _detect_faq(main or soup)

    # ── Media ─────────────────────────────────────────────────────────────────
    images = (main or soup).find_all("img")
    images_missing_alt = [
        img.get("src") or img.get("data-src") or img.get("data-lazy-src") or ""
        for img in images
        if _needs_alt_text(img) and "alt" not in img.attrs
    ]
    videos = (main or soup).find_all(["video", "iframe"])
    video_count = len([
        v for v in videos
        if v.name == "video"
        or "youtube" in (v.get("src") or "")
        or "vimeo" in (v.get("src") or "")
    ])

    # ── Links ─────────────────────────────────────────────────────────────────
    links = (main or soup).find_all("a", href=True)
    internal_links, external_links = 0, 0
    for a in links:
        href = a["href"]
        if href.startswith("#") or href.startswith("mailto:") or href.startswith("tel:"):
            continue
        if href.startswith("http") and domain not in href:
            external_links += 1
        else:
            internal_links += 1

    # ── Schema / technical ─────────────────────────────────────────────────
    schema_types = _find_schema_types(soup)
    schema_lower = {s.lower() for s in schema_types}

    has_article_schema  = bool(schema_lower & {"article", "blogposting", "newsarticle", "techarticle", "liveblogposting"})
    has_product_schema  = bool(schema_lower & {"product", "productgroup"})
    has_service_schema  = bool(schema_lower & {"service", "financialproduct"})
    has_contact_schema  = "contactpage" in schema_lower
    has_org_schema      = bool(schema_lower & {"organization", "corporation", "localbusiness", "nonprofit"})
    has_faq_schema      = "faqpage" in schema_lower
    has_website_schema  = bool(schema_lower & {"website", "sitelinksearchbox"})

    breadcrumb = (
        bool(soup.find(attrs={"class": re.compile("breadcrumb", re.I)}))
        or "breadcrumblist" in schema_lower
    )
    og_tags = {
        m.get("property"): m.get("content")
        for m in soup.find_all("meta", property=re.compile("^og:"))
    }

    author_present = _detect_author(soup)

    # ── Contact-specific signals ────────────────────────────────────────────
    has_contact_form  = _detect_contact_form(soup)
    has_phone         = bool(re.search(r"\+?\d[\d\s\-().]{7,}\d", main_text))
    has_email_address = bool(re.search(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", main_text, re.I))

    # ── Product-specific signals ────────────────────────────────────────────
    has_price       = (
        bool(soup.find(attrs={"itemprop": "price"}))
        or bool(re.search(r"[\$£€₹¥₩]\s*\d[\d.,]*", main_text))
    )
    has_add_to_cart = _detect_add_to_cart(soup)

    # ── Page type (must run after all signals are collected) ────────────────
    page_type = detect_page_type(url, soup, schema_types)

    return {
        "url":          url,
        "error":        None,
        "page_type":    page_type,
        # metadata
        "title":        title,
        "meta_title":   title,
        "meta_description": meta_description,
        "canonical":    canonical,
        "publish_date": publish_date,
        "updated_date": updated_date,
        # content structure
        "h1_count":     len(headings["h1"]),
        "h1_text":      headings["h1"][0] if headings["h1"] else "",
        "headings":     headings,
        "heading_sequence": [h for i in range(1, 7) for h in headings[f"h{i}"]],
        "word_count":   word_count,
        "reading_time_min": reading_time_min,
        "paragraph_count": len(paragraph_texts),
        "avg_paragraph_word_count": round(
            sum(_word_count(p) for p in paragraph_texts) / max(1, len(paragraph_texts)), 1
        ),
        "list_count":   len(lists),
        "table_count":  len(tables),
        "has_faq":      faq_present,
        "main_text":    main_text[:20000],   # capped to keep memory/report size sane
        # media
        "image_count":              len(images),
        "images_missing_alt_count": len(images_missing_alt),
        "video_count":              video_count,
        # links
        "internal_link_count": internal_links,
        "external_link_count": external_links,
        # schema / technical
        "schema_types":       schema_types,
        "has_article_schema": has_article_schema,
        "has_product_schema": has_product_schema,
        "has_service_schema": has_service_schema,
        "has_contact_schema": has_contact_schema,
        "has_org_schema":     has_org_schema,
        "has_website_schema": has_website_schema,
        "has_faq_schema":     has_faq_schema,
        "has_breadcrumb":     breadcrumb,
        "og_tags_present":    len(og_tags) > 0,
        "author_present":     author_present,
        # contact signals
        "has_contact_form":  has_contact_form,
        "has_phone":         has_phone,
        "has_email_address": has_email_address,
        # product signals
        "has_price":       has_price,
        "has_add_to_cart": has_add_to_cart,
    }
