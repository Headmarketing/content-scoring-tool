"""
scoring.py — rule-based, page-type-aware scoring engine. No paid APIs, no ML models.
Every score is a transparent, explainable function of extracted page data.

Pages are routed to a type-specific scorer so that, e.g., product pages are
not penalised for missing an author or FAQ, and contact pages are not flagged
for thin content. Universal SEO checks (title, meta desc, H1, internal links,
OG tags, alt text) always run regardless of page type.

CATEGORY_WEIGHTS: Relevance 25%  |  SEO 20%  |  Quality 20%  |
                  Usefulness 25%  |  Readability 10%
"""
import re
import textstat
from datetime import datetime, timezone

CATEGORY_WEIGHTS = {
    "relevance":   0.25,
    "seo":         0.20,
    "quality":     0.20,
    "usefulness":  0.25,
    "readability": 0.10,
}

# ── Issue registry ────────────────────────────────────────────────────────────

SEVERITY: dict[str, str] = {
    # ── Universal (all page types) ──
    "title_missing":            "High",
    "title_too_short":          "Medium",
    "title_too_long":           "Medium",
    "missing_meta_description": "High",
    "short_meta_description":   "Low",
    "missing_h1":               "High",
    "multiple_h1":              "Medium",
    "no_internal_links":        "High",
    "missing_alt_text":         "Medium",
    "missing_images":           "Medium",
    "no_og_tags":               "Low",
    "missing_breadcrumb":       "Low",
    # ── Blog / Article ──
    "thin_content":             "High",
    "missing_article_schema":   "High",
    "missing_faq_schema":       "Medium",
    "missing_faq":              "High",
    "no_author":                "Medium",
    "stale_content":            "Medium",
    "few_lists_tables":         "Low",
    "long_paragraphs":          "Medium",
    "no_cta":                   "Medium",
    # ── Product (e-commerce) ──
    "missing_product_schema":        "High",
    "missing_price_markup":          "Medium",
    "missing_add_to_cart":           "High",
    "thin_product_description":      "Medium",
    # ── Service / Service-based product ──
    "missing_service_schema":        "Medium",
    "missing_service_page_schema":   "Medium",
    "thin_service_description":      "Medium",
    "service_no_cta":                "High",
    # ── Home ──
    "weak_home_cta":            "High",
    "missing_website_schema":   "Low",
    # ── About ──
    "missing_org_schema":       "Medium",
    # ── Contact ──
    "missing_contact_schema":   "Medium",
    "no_contact_form":          "High",
    "missing_contact_info":     "Medium",
    # ── Community / Forum ──
    "thin_community_content":   "Medium",
    # ── Generic / Landing ──
    "generic_no_cta":           "Medium",
}

ISSUE_LABELS: dict[str, str] = {
    # ── Universal ──
    "title_missing":            "Title Tag Missing",
    "title_too_short":          "Title Tag Too Short (< 55 characters)",
    "title_too_long":           "Title Tag Too Long (> 65 characters)",
    "missing_meta_description": "Missing Meta Description",
    "short_meta_description":   "Meta Description Too Short (< 70 chars)",
    "missing_h1":               "Missing H1 Heading",
    "multiple_h1":              "Multiple H1 Tags (broken heading hierarchy)",
    "no_internal_links":        "Poor Internal Linking (< 2 internal links)",
    "missing_alt_text":         "Image SEO Issue — Missing Alt Text",
    "missing_images":           "No Images Found in Content",
    "no_og_tags":               "Missing Open Graph Tags",
    "missing_breadcrumb":       "Missing Breadcrumb Navigation / Markup",
    # ── Blog / Article ──
    "thin_content":             "Thin Content — Word Count Below 800",
    "missing_article_schema":   "Missing Article / BlogPosting Schema",
    "missing_faq_schema":       "FAQ Content Present but No FAQPage Schema Markup",
    "missing_faq":              "No FAQ Section Detected",
    "no_author":                "Missing E-E-A-T Signal — No Author Information",
    "stale_content":            "Stale Content — Not Updated in 18+ Months",
    "few_lists_tables":         "Minimal Formatting — No Lists or Tables",
    "long_paragraphs":          "Long Paragraphs Hurting Readability (avg > 100 words/paragraph)",
    "no_cta":                   "Weak or Missing Call-to-Action",
    # ── Product (e-commerce) ──
    "missing_product_schema":        "Missing Product Schema Markup (e-commerce)",
    "missing_price_markup":          "Price / Offer Not Marked Up (schema or visible)",
    "missing_add_to_cart":           "No Add-to-Cart or Buy Button Detected",
    "thin_product_description":      "Thin Product Description (< 150 words)",
    # ── Service / Service-based product ──
    "missing_service_schema":        "Missing Service Schema Markup",
    "missing_service_page_schema":   "Service Product Page Missing Schema (Service / FinancialProduct)",
    "thin_service_description":      "Thin Service Page Description (< 300 words)",
    "service_no_cta":                "Service Page Missing Call-to-Action",
    # ── Home ──
    "weak_home_cta":            "Home Page Has No Clear Call-to-Action",
    "missing_website_schema":   "Missing WebSite / Organization Schema",
    # ── About ──
    "missing_org_schema":       "About Page Missing Organization Schema",
    # ── Contact ──
    "missing_contact_schema":   "Missing ContactPage Schema",
    "no_contact_form":          "No Contact Form Detected",
    "missing_contact_info":     "Missing Contact Information (phone or email)",
    # ── Community ──
    "thin_community_content":   "Thin Community Content (< 100 words)",
    # ── Generic / Landing ──
    "generic_no_cta":           "Page Has No Call-to-Action",
}

# General CTA patterns (blog, home, generic, landing pages)
CTA_PATTERNS = re.compile(
    r"\b(sign[\s-]?up|get started|learn more|contact us|book a|try (it|for free)|"
    r"download|subscribe|request a demo|read more|shop now|buy now|"
    r"get a quote|start free|free trial|schedule a call|talk to us|"
    r"get in touch|see pricing|view plans|explore plans|start today|claim your|"
    r"view prices|renew policy|file a claim|check premium|calculate premium|"
    r"download brochure|download pdf|call us|email us|locate us|"
    r"raise a query|chat with us|whatsapp us|find a branch|compare plans|submit)\b",
    re.I,
)

# E-commerce-specific purchase CTAs — buy / add-to-cart actions
ECOMMERCE_CTA_PATTERNS = re.compile(
    r"\b(add to cart|add to bag|add to basket|buy now|purchase|order now|"
    r"checkout|buy today|get it now|shop now|add to wishlist|"
    r"view prices|compare plans)\b",
    re.I,
)

# Service-business CTAs — lead generation, enquiry, consultation, support flows.
# Includes insurance / financial-services specific phrases.
SERVICE_CTA_PATTERNS = re.compile(
    r"\b("
    # ── Contact & support ──
    r"contact us|get in touch|call us|email us|locate us|find a branch|"
    r"raise a query|chat with us|whatsapp us|reach out|drop us a line|send a message|"
    # ── Consultation / demo ──
    r"book a demo|request a demo|schedule a call|book a consultation|"
    r"book a meeting|schedule a meeting|speak to an expert|ask an expert|"
    r"get help|get advice|request a callback|"
    # ── Pricing & plans ──
    r"get a quote|get quote|view prices|see pricing|view plans|explore plans|"
    r"compare plans|check premium|calculate premium|"
    # ── Purchase / renewal (insurance / service context) ──
    r"buy now|buy health insurance|get health insurance quote|"
    r"buy car insurance|renew car insurance|renew policy|"
    # ── General service actions ──
    r"get started|start free|free trial|learn more|read more|"
    r"explore|discover|file a claim|download brochure|download pdf|"
    r"view all press releases|"
    # ── Misc lead-gen ──
    r"enquire|enquire now|connect with us|talk to us|"
    r"get in touch|start today|claim your"
    r")\b",
    re.I,
)


def _clamp(v: float, lo: float = 0, hi: float = 100) -> float:
    return max(lo, min(hi, v))


# ═══════════════════════════════════════════════════════════════════════════════
# Universal SEO — runs for EVERY page type
# ═══════════════════════════════════════════════════════════════════════════════

def _score_universal_seo(page: dict, issues: list) -> float:
    """
    Checks that apply to every page regardless of type:
    title, meta description, H1, internal links, image alt text, OG tags.
    Returns a score 0–100.
    """
    def flag(code):
        issues.append({"code": code, "label": ISSUE_LABELS[code], "severity": SEVERITY[code]})

    seo = 100.0

    # ── Title ──────────────────────────────────────────────────────────────
    # Criteria: Missing = no title; Too Short = < 55 chars; Too Long = > 65 chars
    title = page.get("title") or ""
    title_len = len(title)
    if not title:
        seo -= 25; flag("title_missing")
    elif title_len > 65:
        seo -= 10; flag("title_too_long")
    elif title_len < 55:
        seo -= 10; flag("title_too_short")

    # ── Meta description ───────────────────────────────────────────────────
    meta_desc = page.get("meta_description") or ""
    if not meta_desc:
        seo -= 20; flag("missing_meta_description")
    elif len(meta_desc) < 70:
        seo -= 8; flag("short_meta_description")

    # ── H1 ─────────────────────────────────────────────────────────────────
    h1 = page.get("h1_count", 0)
    if h1 == 0:
        seo -= 20; flag("missing_h1")
    elif h1 > 1:
        seo -= 8; flag("multiple_h1")

    # ── Internal links ─────────────────────────────────────────────────────
    if page.get("internal_link_count", 0) < 2:
        seo -= 15; flag("no_internal_links")

    # ── Image alt text ─────────────────────────────────────────────────────
    # Only flagged when page has images AND some of those images are missing the
    # alt attribute entirely (alt="" for decorative images is NOT flagged).
    if page.get("image_count", 0) > 0 and page.get("images_missing_alt_count", 0) > 0:
        penalty = min(15, page["images_missing_alt_count"] * 3)
        seo -= penalty
        flag("missing_alt_text")

    # ── Open Graph ─────────────────────────────────────────────────────────
    if not page.get("og_tags_present"):
        seo -= 5; flag("no_og_tags")

    return _clamp(seo)


# ═══════════════════════════════════════════════════════════════════════════════
# Shared helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _score_readability(page: dict, issues: list, penalize_long_paras: bool = True) -> float:
    """Flesch reading ease + long-paragraph penalty (optional) + heading density."""
    def flag(code):
        issues.append({"code": code, "label": ISSUE_LABELS[code], "severity": SEVERITY[code]})

    text = page.get("main_text") or ""
    readability = 100.0

    if text.strip():
        try:
            flesch = textstat.flesch_reading_ease(text)
        except Exception:
            flesch = 50.0
        # Flesch 60-70 = plain English (ideal for web); penalise below 60
        readability = 100.0 if flesch >= 60 else _clamp(100.0 - (60 - flesch) * 1.5)
    else:
        readability = 0.0

    # Long paragraph check: flag when average paragraph exceeds 100 words.
    # This threshold is based on readability best practices — paragraphs over
    # ~100 words create dense walls of text that reduce scannability.
    if penalize_long_paras and page.get("avg_paragraph_word_count", 0) > 100:
        readability -= 15
        flag("long_paragraphs")

    # Too few headings relative to content length is a readability issue
    heading_count = len(page.get("heading_sequence") or [])
    if heading_count > 0 and page.get("word_count", 0) > 0:
        if page["word_count"] / heading_count > 400:
            readability -= 10   # wall of text with few signposts

    return _clamp(readability)


def _score_relevance_blog(page: dict) -> float:
    """Title ↔ H1 alignment, subheading depth, FAQ presence, word count depth."""
    relevance = 100.0

    title_words = set(re.findall(r"[a-z]{4,}", (page.get("title") or "").lower()))
    h1_words    = set(re.findall(r"[a-z]{4,}", (page.get("h1_text") or "").lower()))
    overlap = len(title_words & h1_words) / max(1, len(title_words)) if title_words else 0

    if page.get("h1_count", 0) == 0:
        relevance -= 20
    elif overlap < 0.3:
        relevance -= 15     # title and H1 address different topics

    subheadings = len(page.get("headings", {}).get("h2", [])) + len(page.get("headings", {}).get("h3", []))
    if subheadings < 2:
        relevance -= 15     # unlikely to cover the topic with sub-sections

    if not page.get("has_faq"):
        relevance -= 10
    if page.get("word_count", 0) < 800:
        relevance -= 15

    return _clamp(relevance)


def _score_relevance_generic(page: dict) -> float:
    """Basic relevance for non-blog pages: title ↔ H1 alignment and non-empty content."""
    relevance = 100.0

    title_words = set(re.findall(r"[a-z]{4,}", (page.get("title") or "").lower()))
    h1_words    = set(re.findall(r"[a-z]{4,}", (page.get("h1_text") or "").lower()))
    overlap = len(title_words & h1_words) / max(1, len(title_words)) if title_words else 0

    if page.get("h1_count", 0) == 0:
        relevance -= 20
    elif overlap < 0.2:
        relevance -= 10

    if page.get("word_count", 0) == 0:
        relevance -= 20

    return _clamp(relevance)


def _days_since(date_str: str):
    """Return number of days since date_str, or None if unparseable."""
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(
                date_str[: len(fmt.replace("%z", ""))] if "%z" not in fmt else date_str,
                fmt,
            )
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt).days
        except Exception:
            continue
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# Type-specific scorers
# ═══════════════════════════════════════════════════════════════════════════════

def _score_blog(page: dict, issues: list) -> dict:
    """
    Full blog / article checks.
    Expects: author, FAQ section, 800+ words, Article schema, freshness,
    lists/tables for formatting, CTA, readability.
    """
    def flag(code):
        issues.append({"code": code, "label": ISSUE_LABELS[code], "severity": SEVERITY[code]})

    # SEO = universal + article-specific additions
    seo = _score_universal_seo(page, issues)
    if not page.get("has_article_schema"):
        seo -= 15; flag("missing_article_schema")
    if page.get("has_faq") and not page.get("has_faq_schema"):
        seo -= 5;  flag("missing_faq_schema")
    if not page.get("has_breadcrumb"):
        seo -= 5;  flag("missing_breadcrumb")
    seo = _clamp(seo)

    # Quality
    quality = 100.0
    wc = page.get("word_count", 0)
    if wc < 800:
        quality -= 25; flag("thin_content")
    elif wc < 1200:
        quality -= 8

    if not page.get("author_present"):
        quality -= 15; flag("no_author")

    updated = page.get("updated_date") or page.get("publish_date")
    if updated:
        age = _days_since(updated)
        if age is not None and age > 540:   # ~18 months
            quality -= 15; flag("stale_content")
    else:
        quality -= 5    # no date is itself a minor trust gap

    if page.get("list_count", 0) == 0 and page.get("table_count", 0) == 0:
        quality -= 8; flag("few_lists_tables")
    quality = _clamp(quality)

    # Usefulness
    usefulness = 100.0
    if not page.get("has_faq"):
        usefulness -= 20; flag("missing_faq")
    if page.get("list_count", 0) == 0:
        usefulness -= 10
    if page.get("image_count", 0) == 0:
        usefulness -= 10; flag("missing_images")
    if not CTA_PATTERNS.search(page.get("main_text") or ""):
        usefulness -= 12; flag("no_cta")
    if page.get("word_count", 0) < 600:
        usefulness -= 15
    usefulness = _clamp(usefulness)

    readability = _score_readability(page, issues, penalize_long_paras=True)
    relevance   = _score_relevance_blog(page)

    return {"relevance": relevance, "seo": seo, "quality": quality,
            "usefulness": usefulness, "readability": readability}


def _score_product(page: dict, issues: list) -> dict:
    """
    Product page checks — handles both e-commerce and service-based product pages.

    E-commerce products (has price markup OR add-to-cart button, OR site_type=ecommerce):
      - Require Product/Offer schema, visible price, add-to-cart button
      - CTA check uses e-commerce purchase patterns

    Service-based products (no price / no cart button — e.g. SaaS, consulting,
    insurance) OR site_type=service:
      - Require Service / FinancialProduct schema instead
      - CTA check uses service lead-generation patterns (get a quote, book a demo…)
      - NOT penalised for missing price or add-to-cart
    """
    def flag(code):
        issues.append({"code": code, "label": ISSUE_LABELS[code], "severity": SEVERITY[code]})

    # Determine product category: e-commerce vs service-based.
    # --site-type flag (injected as _site_type) takes priority over per-page heuristics
    # so the user can override automatic detection for the whole crawl.
    _site_type = page.get("_site_type", "auto")
    if _site_type == "ecommerce":
        is_ecommerce = True
    elif _site_type == "service":
        is_ecommerce = False
    else:  # "auto" — infer from page-level signals
        is_ecommerce = bool(page.get("has_price") or page.get("has_add_to_cart"))

    # ── SEO ──────────────────────────────────────────────────────────────────
    seo = _score_universal_seo(page, issues)

    if is_ecommerce:
        # E-commerce: expect Product / ProductGroup / Offer schema
        if not page.get("has_product_schema"):
            seo -= 20; flag("missing_product_schema")
    else:
        # Service-based product page: accept Service / FinancialProduct / LocalBusiness schema
        has_any_schema = page.get("has_service_schema") or page.get("has_org_schema")
        if not has_any_schema:
            seo -= 10; flag("missing_service_page_schema")

    if not page.get("has_breadcrumb"):
        seo -= 5; flag("missing_breadcrumb")
    seo = _clamp(seo)

    # ── Quality ───────────────────────────────────────────────────────────────
    quality = 100.0

    if is_ecommerce:
        # E-commerce quality checks
        if not page.get("has_price"):
            quality -= 20; flag("missing_price_markup")
        if not page.get("has_add_to_cart"):
            quality -= 15; flag("missing_add_to_cart")
    # Both types: penalise thin description
    if page.get("word_count", 0) < 150:
        quality -= 20; flag("thin_product_description")
    if page.get("image_count", 0) == 0:
        quality -= 15; flag("missing_images")
    quality = _clamp(quality)

    # ── Usefulness ───────────────────────────────────────────────────────────
    usefulness = 100.0
    if page.get("image_count", 0) == 0:
        usefulness -= 15

    if is_ecommerce:
        # E-commerce: must have an add-to-cart / buy mechanism
        if not page.get("has_add_to_cart") and not ECOMMERCE_CTA_PATTERNS.search(page.get("main_text") or ""):
            usefulness -= 20; flag("missing_add_to_cart")
    else:
        # Service product page: must have a lead-generation CTA
        if not SERVICE_CTA_PATTERNS.search(page.get("main_text") or ""):
            usefulness -= 20; flag("service_no_cta")

    if page.get("list_count", 0) == 0:     # features/specs/benefits usually in lists
        usefulness -= 8
    usefulness = _clamp(usefulness)

    # Readability lighter for product pages (short bullet-heavy pages score well)
    readability = _score_readability(page, issues, penalize_long_paras=False)
    relevance   = _score_relevance_generic(page)

    return {"relevance": relevance, "seo": seo, "quality": quality,
            "usefulness": usefulness, "readability": readability}


def _score_service(page: dict, issues: list) -> dict:
    """
    Service page checks.
    Expects: 300+ words describing the service, CTA, lists/tables, images.
    No author or FAQ requirement.
    """
    def flag(code):
        issues.append({"code": code, "label": ISSUE_LABELS[code], "severity": SEVERITY[code]})

    seo = _score_universal_seo(page, issues)
    if not page.get("has_service_schema"):
        seo -= 10; flag("missing_service_schema")
    if not page.get("has_breadcrumb"):
        seo -= 5;  flag("missing_breadcrumb")
    seo = _clamp(seo)

    quality = 100.0
    wc = page.get("word_count", 0)
    if wc < 300:
        quality -= 25; flag("thin_service_description")
    elif wc < 500:
        quality -= 10
    if page.get("image_count", 0) == 0:
        quality -= 10; flag("missing_images")
    if page.get("list_count", 0) == 0 and page.get("table_count", 0) == 0:
        quality -= 8;  flag("few_lists_tables")
    quality = _clamp(quality)

    usefulness = 100.0
    # Service pages use service-specific lead-generation CTAs
    if not SERVICE_CTA_PATTERNS.search(page.get("main_text") or ""):
        usefulness -= 25; flag("service_no_cta")
    if page.get("list_count", 0) == 0:
        usefulness -= 10
    if page.get("image_count", 0) == 0:
        usefulness -= 8
    usefulness = _clamp(usefulness)

    readability = _score_readability(page, issues, penalize_long_paras=True)
    relevance   = _score_relevance_generic(page)

    return {"relevance": relevance, "seo": seo, "quality": quality,
            "usefulness": usefulness, "readability": readability}


def _score_home(page: dict, issues: list) -> dict:
    """
    Home page checks.
    Expects: clear CTA, schema (WebSite/Organization), OG tags, images,
    several internal links to key site sections. Does NOT require 800 words.
    """
    def flag(code):
        issues.append({"code": code, "label": ISSUE_LABELS[code], "severity": SEVERITY[code]})

    seo = _score_universal_seo(page, issues)
    if not (page.get("has_website_schema") or page.get("has_org_schema")):
        seo -= 10; flag("missing_website_schema")
    seo = _clamp(seo)

    quality = 100.0
    if page.get("word_count", 0) < 100:     # home pages can be concise — only flag very thin
        quality -= 15
    if page.get("image_count", 0) == 0:
        quality -= 10; flag("missing_images")
    quality = _clamp(quality)

    usefulness = 100.0
    if not CTA_PATTERNS.search(page.get("main_text") or ""):
        usefulness -= 30; flag("weak_home_cta")
    if page.get("internal_link_count", 0) < 3:     # home should link to key sections
        usefulness -= 15
    usefulness = _clamp(usefulness)

    readability = _score_readability(page, issues, penalize_long_paras=False)
    relevance   = _score_relevance_generic(page)

    return {"relevance": relevance, "seo": seo, "quality": quality,
            "usefulness": usefulness, "readability": readability}


def _score_about(page: dict, issues: list) -> dict:
    """
    About / company page checks.
    Expects: Organization schema, trust signals (team, mission), CTA to contact.
    No author, FAQ, or high word-count requirement.
    """
    def flag(code):
        issues.append({"code": code, "label": ISSUE_LABELS[code], "severity": SEVERITY[code]})

    seo = _score_universal_seo(page, issues)
    seo = _clamp(seo)

    quality = 100.0
    if not page.get("has_org_schema"):
        quality -= 20; flag("missing_org_schema")
    if page.get("word_count", 0) < 150:
        quality -= 15
    if page.get("image_count", 0) == 0:
        quality -= 10; flag("missing_images")
    quality = _clamp(quality)

    usefulness = 100.0
    if not CTA_PATTERNS.search(page.get("main_text") or ""):
        usefulness -= 15; flag("no_cta")
    if page.get("internal_link_count", 0) < 2:
        usefulness -= 10
    usefulness = _clamp(usefulness)

    readability = _score_readability(page, issues, penalize_long_paras=False)
    relevance   = _score_relevance_generic(page)

    return {"relevance": relevance, "seo": seo, "quality": quality,
            "usefulness": usefulness, "readability": readability}


def _score_contact(page: dict, issues: list) -> dict:
    """
    Contact page checks.
    Expects: ContactPage schema, a working contact form, and visible
    phone/email information. Word count is naturally low — NOT flagged as thin.
    """
    def flag(code):
        issues.append({"code": code, "label": ISSUE_LABELS[code], "severity": SEVERITY[code]})

    seo = _score_universal_seo(page, issues)
    if not page.get("has_contact_schema"):
        seo -= 15; flag("missing_contact_schema")
    seo = _clamp(seo)

    quality = 100.0
    if not page.get("has_contact_form"):
        quality -= 30; flag("no_contact_form")
    if not (page.get("has_phone") or page.get("has_email_address")):
        quality -= 20; flag("missing_contact_info")
    # Contact pages are intentionally concise — no word-count penalty
    quality = _clamp(quality)

    usefulness = 100.0
    if not page.get("has_contact_form"):
        usefulness -= 25
    if not (page.get("has_phone") or page.get("has_email_address")):
        usefulness -= 15
    usefulness = _clamp(usefulness)

    # Mostly a form — readability of prose text is not the primary concern
    readability = 80.0 if page.get("word_count", 0) > 50 else 60.0
    relevance   = _score_relevance_generic(page)

    return {"relevance": relevance, "seo": seo, "quality": quality,
            "usefulness": usefulness, "readability": readability}


def _score_community(page: dict, issues: list) -> dict:
    """
    Community / forum page checks.
    Expects: reasonable content length, internal links, OG tags.
    No author, FAQ, or high word-count requirement.
    """
    def flag(code):
        issues.append({"code": code, "label": ISSUE_LABELS[code], "severity": SEVERITY[code]})

    seo = _score_universal_seo(page, issues)
    seo = _clamp(seo)

    quality = 100.0
    if page.get("word_count", 0) < 100:
        quality -= 20; flag("thin_community_content")
    quality = _clamp(quality)

    usefulness = 100.0
    if page.get("internal_link_count", 0) < 2:
        usefulness -= 15
    usefulness = _clamp(usefulness)

    readability = _score_readability(page, issues, penalize_long_paras=True)
    relevance   = _score_relevance_generic(page)

    return {"relevance": relevance, "seo": seo, "quality": quality,
            "usefulness": usefulness, "readability": readability}


def _score_category(page: dict, issues: list) -> dict:
    """
    Category / archive / tag listing pages.
    These are thin by design — quality starts neutral.
    Internal links matter (the whole point is to distribute link equity).
    """
    seo = _score_universal_seo(page, issues)
    seo = _clamp(seo)

    # Category pages are templates — start quality at 70 (not 100)
    quality = 70.0
    quality = _clamp(quality)

    usefulness = 100.0
    if page.get("internal_link_count", 0) < 5:
        usefulness -= 20    # listing page with few links defeats its purpose
    usefulness = _clamp(usefulness)

    readability = 70.0
    relevance   = _score_relevance_generic(page)

    return {"relevance": relevance, "seo": seo, "quality": quality,
            "usefulness": usefulness, "readability": readability}


def _score_landing(page: dict, issues: list) -> dict:
    """
    Landing / campaign page checks.
    CTA is critical. Shorter content is acceptable.
    No breadcrumb or author requirement.
    """
    def flag(code):
        issues.append({"code": code, "label": ISSUE_LABELS[code], "severity": SEVERITY[code]})

    seo = _score_universal_seo(page, issues)
    seo = _clamp(seo)

    quality = 100.0
    if page.get("word_count", 0) < 200:
        quality -= 15
    if page.get("image_count", 0) == 0:
        quality -= 10; flag("missing_images")
    quality = _clamp(quality)

    usefulness = 100.0
    if not CTA_PATTERNS.search(page.get("main_text") or ""):
        usefulness -= 35; flag("generic_no_cta")   # CTA is the whole point of a landing page
    usefulness = _clamp(usefulness)

    readability = _score_readability(page, issues, penalize_long_paras=False)
    relevance   = _score_relevance_generic(page)

    return {"relevance": relevance, "seo": seo, "quality": quality,
            "usefulness": usefulness, "readability": readability}


def _score_generic(page: dict, issues: list) -> dict:
    """
    Generic / other page checks.
    Universal SEO + basic content quality. No type-specific requirements.
    """
    def flag(code):
        issues.append({"code": code, "label": ISSUE_LABELS[code], "severity": SEVERITY[code]})

    seo = _score_universal_seo(page, issues)
    if not page.get("has_breadcrumb"):
        seo -= 5; flag("missing_breadcrumb")
    seo = _clamp(seo)

    quality = 100.0
    if page.get("word_count", 0) < 200:
        quality -= 15
    if page.get("image_count", 0) == 0:
        quality -= 8; flag("missing_images")
    quality = _clamp(quality)

    usefulness = 100.0
    if not CTA_PATTERNS.search(page.get("main_text") or ""):
        usefulness -= 12; flag("generic_no_cta")
    if page.get("list_count", 0) == 0:
        usefulness -= 5
    usefulness = _clamp(usefulness)

    readability = _score_readability(page, issues, penalize_long_paras=True)
    relevance   = _score_relevance_generic(page)

    return {"relevance": relevance, "seo": seo, "quality": quality,
            "usefulness": usefulness, "readability": readability}


# ═══════════════════════════════════════════════════════════════════════════════
# Dispatcher
# ═══════════════════════════════════════════════════════════════════════════════

_SCORER_MAP = {
    "blog":      _score_blog,
    "product":   _score_product,
    "service":   _score_service,
    "home":      _score_home,
    "about":     _score_about,
    "contact":   _score_contact,
    "community": _score_community,
    "category":  _score_category,
    "landing":   _score_landing,
    "generic":   _score_generic,
}


def score_page(page: dict, site_type: str = "auto") -> dict:
    """
    Main entry point.
    Returns {'category_scores': {...}, 'overall_score': float, 'issues': [...]}.
    Routes to a type-specific scorer based on page['page_type'].

    site_type  Controls how product pages are scored:
      'ecommerce' — treat ALL product pages as e-commerce (price + add-to-cart expected)
      'service'   — treat ALL product pages as service offerings (lead-gen CTA expected)
      'auto'      — infer per page from has_price / has_add_to_cart signals (default)
    """
    if page.get("error"):
        return {
            "category_scores": {},
            "overall_score": 0,
            "issues": [{
                "code": "fetch_error",
                "label": f"Could not fetch page: {page['error']}",
                "severity": "High",
            }],
        }

    issues: list = []
    page_type = page.get("page_type", "generic")
    scorer = _SCORER_MAP.get(page_type, _score_generic)

    # Inject site_type into a working copy of the page dict so scorers can read
    # it via page.get('_site_type') without changing every function signature.
    page_ctx = {**page, "_site_type": site_type}
    raw_scores = scorer(page_ctx, issues)

    category_scores = {k: round(v, 1) for k, v in raw_scores.items()}
    overall = sum(category_scores[c] * w for c, w in CATEGORY_WEIGHTS.items())

    # De-duplicate by issue CODE only — every distinct issue type fires at most
    # once per page, but a page can (and does) have many different issue codes.
    # Example: a blog page can simultaneously have thin_content + no_author +
    # missing_h1 + no_cta — all four are reported.
    seen: set = set()
    deduped: list = []
    for issue in issues:
        if issue["code"] not in seen:
            seen.add(issue["code"])
            deduped.append(issue)

    return {
        "category_scores": category_scores,
        "overall_score":   round(overall, 1),
        "issues":          deduped,
    }
