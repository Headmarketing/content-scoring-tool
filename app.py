"""
app.py — Flask web application for the Content Audit Tool.

Wraps the existing CLI pipeline (crawler, extractor, scoring, report) as a
web service deployable on Render (or any WSGI host).

Routes:
  GET  /                  — Web UI
  POST /run               — Start an audit job; returns {job_id, total}
  GET  /status/<job_id>   — Poll job progress; returns JSON
  GET  /download/<job_id> — Stream the finished .xlsx to the browser

Job state is stored in SQLite (/tmp/audit_jobs.db).
This means:
  - Jobs survive gunicorn worker restarts
  - Works correctly with any number of workers (no more 404s)
  - No external Redis / PostgreSQL service needed
"""
import os
import uuid
import threading
import time
import sqlite3
import concurrent.futures as cf
from typing import Optional
from urllib.parse import urlparse

from flask import Flask, request, jsonify, send_file, render_template

from crawler import urls_from_sitemap, urls_from_csv
from extractor import extract_page
from scoring import score_page
from report import build_site_report, export_xlsx

# ── App setup ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB max CSV upload

UPLOAD_FOLDER = "/tmp"
DB_PATH       = "/tmp/audit_jobs.db"
DEFAULT_WORKERS = 6


# ── SQLite job store ───────────────────────────────────────────────────────────
# SQLite is file-based and thread-safe, solving the multi-worker 404 problem
# that occurs with in-memory dicts.

def _get_db() -> sqlite3.Connection:
    """Open a SQLite connection (thread-safe, with a 10-second busy timeout)."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db():
    """Create the jobs table if it doesn't exist yet."""
    with _get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id              TEXT PRIMARY KEY,
                status          TEXT    DEFAULT 'queued',
                progress        INTEGER DEFAULT 0,
                total           INTEGER DEFAULT 0,
                file_path       TEXT,
                error           TEXT,
                site_label      TEXT,
                overall_score   REAL,
                pages_analyzed  INTEGER,
                created_at      REAL
            )
        """)
        conn.commit()


_init_db()  # run once at import time


def _create_job(job_id: str, total: int, site_label: str):
    with _get_db() as conn:
        conn.execute(
            "INSERT INTO jobs "
            "(id, status, progress, total, site_label, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, "queued", 0, total, site_label, time.time()),
        )
        conn.commit()


def _update_job(job_id: str, **kwargs):
    """Update any number of columns for a job in one call."""
    if not kwargs:
        return
    cols = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [job_id]
    with _get_db() as conn:
        conn.execute(f"UPDATE jobs SET {cols} WHERE id = ?", vals)
        conn.commit()


def _get_job(job_id: str) -> Optional[dict]:
    with _get_db() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return dict(row) if row else None


def _cleanup_old_jobs():
    """Delete jobs (and their output files) older than 2 hours."""
    cutoff = time.time() - 7200
    with _get_db() as conn:
        rows = conn.execute(
            "SELECT id, file_path FROM jobs WHERE created_at < ?", (cutoff,)
        ).fetchall()
        for row in rows:
            fp = row["file_path"]
            if fp and os.path.exists(fp):
                try:
                    os.remove(fp)
                except OSError:
                    pass
        conn.execute("DELETE FROM jobs WHERE created_at < ?", (cutoff,))
        conn.commit()


# ── Audit pipeline ─────────────────────────────────────────────────────────────

def _audit_one(url: str, site_type: str = "auto") -> dict:
    """Fetch + extract + score a single URL."""
    page = extract_page(url)
    if page.get("error"):
        return {**page, "category_scores": {}, "overall_score": 0, "issues": []}
    result = score_page(page, site_type=site_type)
    return {**page, **result}


def _run_audit(job_id: str, urls: list, site_label: str, site_type: str, workers: int):
    """
    Background worker: runs the full audit pipeline, writes the Excel file,
    and updates the SQLite job record throughout.
    """
    try:
        total = len(urls)
        results = []

        _update_job(job_id, status="running", total=total)

        with cf.ThreadPoolExecutor(max_workers=workers) as ex:
            future_map = {ex.submit(_audit_one, u, site_type): u for u in urls}
            completed = 0
            for fut in cf.as_completed(future_map):
                try:
                    res = fut.result()
                except Exception as e:
                    res = {
                        "url": future_map[fut], "error": str(e),
                        "category_scores": {}, "overall_score": 0, "issues": [],
                    }
                # Drop full HTML text after scoring — not needed for the report
                # and saves significant RAM on Render's free 512 MB tier.
                res.pop("main_text", None)
                results.append(res)
                completed += 1
                _update_job(job_id, progress=completed)

        # Step 3 — build the Excel report
        _update_job(job_id, status="building_report", progress=total)

        report    = build_site_report(results)
        xlsx_path = os.path.join(UPLOAD_FOLDER, f"{job_id}.xlsx")
        export_xlsx(report, xlsx_path)

        _update_job(
            job_id,
            status         = "done",
            file_path      = xlsx_path,
            overall_score  = report.get("overall_health_score"),
            pages_analyzed = report.get("successfully_analyzed"),
        )

    except Exception as e:
        _update_job(job_id, status="error", error=str(e))


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/run", methods=["POST"])
def run():
    _cleanup_old_jobs()

    input_type = request.form.get("input_type", "sitemap")
    site_type  = request.form.get("site_type", "auto")
    max_urls   = min(int(request.form.get("max_urls", 200)), 8000)
    workers    = min(int(request.form.get("workers", DEFAULT_WORKERS)), 20)

    urls: list = []
    site_label = "Website"

    try:
        if input_type == "sitemap":
            sitemap_url = (request.form.get("sitemap_url") or "").strip()
            if not sitemap_url:
                return jsonify({"error": "Sitemap URL is required."}), 400
            if not sitemap_url.startswith(("http://", "https://")):
                return jsonify({"error": "Please enter a full URL starting with http:// or https://"}), 400
            site_label = urlparse(sitemap_url).netloc or sitemap_url
            urls = urls_from_sitemap(sitemap_url, max_urls=max_urls)

        elif input_type == "csv":
            if "csv_file" not in request.files:
                return jsonify({"error": "CSV file is required."}), 400
            f = request.files["csv_file"]
            if not f or not f.filename:
                return jsonify({"error": "No file selected."}), 400
            tmp_id   = uuid.uuid4().hex
            csv_path = os.path.join(UPLOAD_FOLDER, f"{tmp_id}_input.csv")
            f.save(csv_path)
            try:
                urls = urls_from_csv(csv_path)[:max_urls]
            finally:
                try:
                    os.remove(csv_path)
                except OSError:
                    pass
            if urls:
                site_label = urlparse(urls[0]).netloc or "Website"

        else:
            return jsonify({"error": "Invalid input_type — must be 'sitemap' or 'csv'."}), 400

    except Exception as e:
        return jsonify({"error": f"Failed to discover URLs: {e}"}), 500

    if not urls:
        return jsonify({"error": (
            "No URLs were found. "
            "Check that the sitemap URL is publicly accessible, "
            "or that the CSV contains valid http(s) URLs."
        )}), 400

    job_id = uuid.uuid4().hex
    _create_job(job_id, total=len(urls), site_label=site_label)

    t = threading.Thread(
        target=_run_audit,
        args=(job_id, urls, site_label, site_type, workers),
        daemon=True,
    )
    t.start()

    return jsonify({"job_id": job_id, "total": len(urls), "site_label": site_label})


@app.route("/status/<job_id>")
def status(job_id: str):
    job = _get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found."}), 404
    return jsonify({
        "status":         job["status"],
        "progress":       job["progress"],
        "total":          job["total"],
        "error":          job["error"],
        "site_label":     job["site_label"],
        "overall_score":  job["overall_score"],
        "pages_analyzed": job["pages_analyzed"],
    })


@app.route("/download/<job_id>")
def download(job_id: str):
    job = _get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found."}), 404
    if job["status"] != "done":
        return jsonify({"error": "Job is not complete yet."}), 400
    fp = job.get("file_path")
    if not fp or not os.path.exists(fp):
        return jsonify({"error": "Output file not found — it may have been cleaned up."}), 404

    label    = (job["site_label"] or "audit").replace(" ", "_").replace("/", "_")
    filename = f"content_audit_{label}.xlsx"

    return send_file(
        fp,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
