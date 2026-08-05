"""
app.py — Flask web application for the Content Audit Tool.

Wraps the existing CLI pipeline (crawler, extractor, scoring, report) as a
web service deployable on Render (or any WSGI host).

Routes:
  GET  /                  — Web UI
  POST /run               — Start an audit job; returns {job_id, total}
  GET  /status/<job_id>   — Poll job progress; returns JSON
  GET  /download/<job_id> — Stream the finished .xlsx to the browser

All heavy work runs in a daemon thread so /run returns immediately.
Job state lives in an in-memory dict (no DB / Redis needed).
Files are written to /tmp and auto-cleaned after 2 hours.
"""
import os
import uuid
import threading
import time
import concurrent.futures as cf
from urllib.parse import urlparse

from flask import Flask, request, jsonify, send_file, render_template
from werkzeug.utils import secure_filename

from crawler import urls_from_sitemap, urls_from_csv
from extractor import extract_page
from scoring import score_page
from report import build_site_report, export_xlsx

# ── App setup ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB max CSV upload

UPLOAD_FOLDER = "/tmp"
DEFAULT_WORKERS = 6

# ── In-memory job store ────────────────────────────────────────────────────────
# Structure: job_id → {status, progress, total, file_path, error, site_label, created_at}
jobs: dict = {}
jobs_lock = threading.Lock()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _cleanup_old_jobs():
    """Remove completed/errored jobs older than 2 hours to avoid memory leaks."""
    cutoff = time.time() - 7200
    with jobs_lock:
        stale = [
            jid for jid, j in jobs.items()
            if j.get("created_at", 0) < cutoff
        ]
        for jid in stale:
            fp = jobs[jid].get("file_path")
            if fp and os.path.exists(fp):
                try:
                    os.remove(fp)
                except OSError:
                    pass
            del jobs[jid]


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
    and updates the job status dict throughout.
    """
    try:
        total = len(urls)
        results = []
        completed = 0

        with jobs_lock:
            jobs[job_id].update({"total": total, "status": "running"})

        with cf.ThreadPoolExecutor(max_workers=workers) as ex:
            future_map = {ex.submit(_audit_one, u, site_type): u for u in urls}
            for fut in cf.as_completed(future_map):
                try:
                    res = fut.result()
                except Exception as e:
                    url = future_map[fut]
                    res = {"url": url, "error": str(e),
                           "category_scores": {}, "overall_score": 0, "issues": []}
                # Drop large HTML text after scoring — not needed for the report
                # and saves significant RAM on Render's free 512 MB tier.
                res.pop("main_text", None)
                results.append(res)
                completed += 1
                with jobs_lock:
                    jobs[job_id]["progress"] = completed

        # Signal step 3 — building the Excel report
        with jobs_lock:
            jobs[job_id]["status"] = "building_report"

        report = build_site_report(results)
        xlsx_path = os.path.join(UPLOAD_FOLDER, f"{job_id}.xlsx")
        export_xlsx(report, xlsx_path)

        with jobs_lock:
            jobs[job_id].update({
                "status": "done",
                "file_path": xlsx_path,
                "site_label": site_label,
                "overall_score": report.get("overall_health_score"),
                "pages_analyzed": report.get("successfully_analyzed"),
            })

    except Exception as e:
        with jobs_lock:
            jobs[job_id].update({"status": "error", "error": str(e)})


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/run", methods=["POST"])
def run():
    _cleanup_old_jobs()

    input_type = request.form.get("input_type", "sitemap")
    site_type  = request.form.get("site_type", "auto")
    max_urls   = min(int(request.form.get("max_urls", 500)), 8000)
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
            # Save the upload, parse it, then delete it
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
            "Check that the sitemap URL is publicly accessible, or that the CSV contains valid http(s) URLs."
        )}), 400

    job_id = uuid.uuid4().hex
    with jobs_lock:
        jobs[job_id] = {
            "status":     "queued",
            "progress":   0,
            "total":      len(urls),
            "file_path":  None,
            "error":      None,
            "site_label": site_label,
            "created_at": time.time(),
        }

    t = threading.Thread(
        target=_run_audit,
        args=(job_id, urls, site_label, site_type, workers),
        daemon=True,
    )
    t.start()

    return jsonify({"job_id": job_id, "total": len(urls), "site_label": site_label})


@app.route("/status/<job_id>")
def status(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found."}), 404
    return jsonify({
        "status":        job["status"],
        "progress":      job["progress"],
        "total":         job["total"],
        "error":         job.get("error"),
        "site_label":    job.get("site_label", ""),
        "overall_score": job.get("overall_score"),
        "pages_analyzed":job.get("pages_analyzed"),
    })


@app.route("/download/<job_id>")
def download(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found."}), 404
    if job["status"] != "done":
        return jsonify({"error": "Job is not complete yet."}), 400
    fp = job.get("file_path")
    if not fp or not os.path.exists(fp):
        return jsonify({"error": "Output file not found — it may have been cleaned up."}), 404

    label    = (job.get("site_label") or "audit").replace(" ", "_").replace("/", "_")
    filename = f"content_audit_{label}.xlsx"

    return send_file(
        fp,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
