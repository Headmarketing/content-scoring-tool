# Scoring Criteria — How the Content Health Score Is Calculated

Every score is 100% rule-based (see `scoring.py`) — no ML model, no paid API.
Each page starts each category at 100 points, and specific point deductions
fire when a rule condition is true. This document lists every rule and its
exact deduction, so the scoring is fully auditable.

## Overall Score = Weighted Average

| Category | Weight |
|---|---|
| Relevance | 25% |
| SEO | 20% |
| Quality | 20% |
| Usefulness | 25% |
| Readability | 10% |

`Overall = (Relevance×0.25) + (SEO×0.20) + (Quality×0.20) + (Usefulness×0.25) + (Readability×0.10)`

---

## SEO Score (starts at 100)

| Rule | Deduction | Severity |
|---|---|---|
| Title tag **missing** entirely | −25 | High |
| Title tag **too long** (> 65 characters) | −10 | Medium |
| Title tag **too short** (< 55 characters) | −10 | Medium |
| Meta description missing | −20 | High |
| Meta description shorter than 70 characters | −8 | Low |
| No H1 tag on the page | −20 | High |
| More than one H1 tag | −8 | Medium |
| Fewer than 2 internal links | −15 | High |
| Images present but **`alt` attribute entirely absent** (not `alt=""`) | −3 per missing image (capped at −15) | Medium |
| No Article/BlogPosting schema (JSON-LD or microdata) | −15 | High |
| Page has FAQ content but no FAQPage schema | −5 | Medium |
| No breadcrumb (markup or breadcrumb schema) | −5 | Low |
| No Open Graph tags | −5 | Low |

## Quality Score (starts at 100)

| Rule | Deduction | Severity |
|---|---|---|
| Word count < 800 | −25 | High |
| Word count 800–1199 | −8 | — |
| No author byline detected | −15 | Medium |
| No publish/updated date found at all | −5 | — |
| Last updated > 540 days ago (~18 months) | −15 | Medium |
| No lists or tables anywhere on the page | −8 | Low |

## Usefulness Score (starts at 100)

| Rule | Deduction | Severity |
|---|---|---|
| No FAQ section detected | −20 | High |
| No bullet/numbered lists | −10 | — |
| No images at all | −10 | Medium |
| No call-to-action phrase detected (e.g. "sign up", "learn more", "download", "subscribe", **"submit"**) | −12 | Medium |
| Word count < 600 | −15 | — |

## Readability Score (starts at 100)

| Rule | Basis |
|---|---|
| Flesch Reading Ease score | Score of 60+ (plain English) = 100 points. Below 60, points drop 1.5 per point below 60. |
| Average paragraph length **> 100 words** | −15, flagged as Medium (paragraphs over ~100 words create walls of text) |
| Fewer than 1 subheading per ~400 words | −10 |

## Relevance Score (starts at 100)
*(No paid NLP — rule-based proxy for topical relevance)*

| Rule | Deduction |
|---|---|
| No H1 at all | −20 |
| Title and H1 share <30% of significant words (likely topic mismatch) | −15 |
| Fewer than 2 H2/H3 subheadings (unlikely to cover the topic in depth) | −15 |
| No FAQ section | −10 |
| Word count < 800 | −15 |

---

## Notes on accuracy

- **Schema detection** normalizes full URLs (`schema.org/Article` → `Article`), recovers from malformed JSON-LD, and falls back to microdata (`itemtype=`) if no JSON-LD is found — so valid schema isn't missed due to formatting differences.
- **FAQ detection** looks for an explicit "Frequently Asked Questions" heading, or 2+ subheadings phrased as questions.
- **Author detection** checks for an `author`-classed element, `rel="author"`, or an `author` field inside schema.org JSON-LD.
- **Image alt text** is only flagged when the `alt` attribute is **entirely absent** from a content image. `alt=""` (correct for decorative images) is never flagged. Tracking pixels and `role="presentation"` images are also skipped.
- **Title tag issues** produce three distinct, specific issue codes: *Title Tag Missing*, *Title Tag Too Short (< 55 chars)*, or *Title Tag Too Long (> 65 chars)* — never a generic combined flag.
- **ContactPage schema** (`missing_contact_schema`) is only flagged for pages classified as **contact** pages (by URL slug or HTML signals). It is never raised on non-contact pages.
- **Organization schema** (`missing_org_schema`) is only flagged for pages classified as **about** pages (URL slug contains `/about`, `/about-us`, `/company`, etc.). It is never raised on arbitrary pages.
- **Long paragraph** detection is based on the average word count across all `<p>` tags in the main content area. The threshold is **100 words per paragraph on average** (reduced from 130) to reliably catch wall-of-text formatting before it severely impacts readability.
- **Multiple issues per page** are all reported together: the Issues by Page sheet lists every distinct issue for a URL in a single cell (semicolon-separated), not one row per issue.
- All thresholds (800 words, 65-char title max, 55-char title min, 540-day staleness, etc.) are plain constants in `scoring.py` — edit them directly to match your team's editorial standards. No retraining needed.
- Every issue a page triggers is deduplicated (a rule fires at most once per page) and tagged High/Medium/Low severity, which is what drives the Recommendations and Priority Pages tabs.
