"""
main.py — CLI for the AI Content Audit Platform.

Usage:
  python main.py --sitemap https://example.com/sitemap.xml
  python main.py --site-url https://example.com
  python main.py --csv urls.csv

  # Specify website type for more accurate scoring:
  python main.py --sitemap https://shop.com/sitemap.xml --site-type ecommerce
  python main.py --sitemap https://agency.com/sitemap.xml --site-type service

  # Full example:
  python main.py --sitemap https://example.com/sitemap.xml \\
      --max-urls 8000 --site-type service --workers 10 --out-dir reports/

Aliases:
  --blog-url is kept as a deprecated alias for --site-url so existing scripts
  don't break.
"""
import argparse
import concurrent.futures as cf
import sys
import time
from urllib.parse import urlparse

from crawler import discover_urls
from extractor import extract_page
from scoring import score_page
from report import build_site_report, export_csv, export_xlsx, render_html


def audit_url(url: str, delay: float = 0, site_type: str = "auto") -> dict:
    if delay:
        time.sleep(delay)
    page = extract_page(url)
    if page.get("error"):
        return {**page, "category_scores": {}, "overall_score": 0, "issues": []}
    result = score_page(page, site_type=site_type)
    return {**page, **result}


def main():
    ap = argparse.ArgumentParser(
        description="AI Content Audit Platform — bulk website content & SEO analysis"
    )
    src = ap.add_mutually_exclusive_group(required=True)

    # Primary flag (general-purpose BFS crawler starting from any page)
    src.add_argument(
        "--site-url",
        help="Any URL on the target site (usually the homepage) — the crawler will "
             "discover all pages via BFS link-following (up to --max-urls).",
    )
    # Deprecated alias kept for backward compatibility
    src.add_argument(
        "--blog-url",
        help="[Deprecated — use --site-url] Alias accepted for backward compatibility.",
    )
    src.add_argument(
        "--sitemap",
        help="Sitemap.xml URL (or sitemap index). Fastest discovery method.",
    )
    src.add_argument(
        "--csv",
        help="Path to a CSV or plain-text file of URLs, one per line.",
    )

    ap.add_argument(
        "--max-urls",
        type=int,
        default=8000,
        help="Maximum number of pages to audit (default 8000).",
    )
    ap.add_argument(
        "--workers",
        type=int,
        default=6,
        help="Parallel fetch workers (default 6). Use 8–12 for large crawls on fast "
             "connections; use 1–2 for sites that aggressively rate-limit.",
    )
    ap.add_argument(
        "--delay",
        type=float,
        default=0,
        help="Per-worker delay in seconds between requests (default 0). Set to 0.5–1.0 "
             "if you're hitting rate limits on large crawls.",
    )
    ap.add_argument(
        "--out-dir",
        default="reports",
        help="Output directory (default: ./reports).",
    )
    ap.add_argument(
        "--site-label",
        default=None,
        help="Label shown on the dashboard (default: domain name from first URL).",
    )
    ap.add_argument(
        "--site-type",
        choices=["ecommerce", "service", "auto"],
        default="auto",
        help=(
            "Type of website being audited. Tells the scorer which checks apply:\n"
            "  ecommerce — online store (product + price + add-to-cart checks)\n"
            "  service   — insurance, SaaS, agency, consulting, etc. "
            "(lead-gen CTA + service schema checks)\n"
            "  auto      — detect per page from schema / URL signals (default)"
        ),
    )

    args = ap.parse_args()

    # Resolve deprecated --blog-url alias
    site_url = args.site_url or args.blog_url

    print("→ Discovering URLs...")
    urls = discover_urls(
        site_url=site_url,
        sitemap_url=args.sitemap,
        csv_path=args.csv,
        max_urls=args.max_urls,
    )
    if not urls:
        print("No URLs discovered. Check the input source.", file=sys.stderr)
        sys.exit(1)
    print(f"  found {len(urls):,} URLs")

    site_label = args.site_label or urlparse(urls[0]).netloc
    site_type  = args.site_type

    type_note = f"  [{site_type} mode]" if site_type != "auto" else "  [auto-detect mode]"
    print(f"→ Auditing {len(urls):,} pages ({args.workers} parallel workers)...{type_note}")
    results = []
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(audit_url, u, args.delay, site_type): u for u in urls}
        for i, fut in enumerate(cf.as_completed(futures), 1):
            res = fut.result()
            results.append(res)
            status = "OK" if not res.get("error") else f"FAILED ({res['error']})"
            ptype  = res.get("page_type", "?")
            score  = res.get("overall_score", 0)
            print(f"  [{i:>5}/{len(urls):,}] [{ptype:<10}] score={score:>5.1f}  {futures[fut]}  — {status}")

    print("→ Building site-level report...")
    report = build_site_report(results)

    import os
    os.makedirs(args.out_dir, exist_ok=True)
    html_path  = os.path.join(args.out_dir, "content_health_dashboard.html")
    csv_path   = os.path.join(args.out_dir, "content_inventory.csv")
    xlsx_path  = os.path.join(args.out_dir, "content_audit_report.xlsx")

    render_html(report, html_path, site_label=site_label)
    if "content_inventory" in report:
        export_csv(report, csv_path)
        export_xlsx(report, xlsx_path)

    print(f"\nDone. Overall Content Health Score: {report.get('overall_health_score', 'N/A')}")
    print(f"  Dashboard : {html_path}")
    print(f"  CSV       : {csv_path}")
    print(f"  Excel     : {xlsx_path}")


if __name__ == "__main__":
    main()
