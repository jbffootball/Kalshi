import os
from flask import Flask, abort, send_file

app = Flask(__name__)

ROOT = Path("/data").resolve()

def safe_path(rel_path: str) -> Path:
    p = (ROOT / rel_path).resolve()
    if ROOT != p and ROOT not in p.parents:
        abort(403)
    return p

@app.route("/")
def index():
    rows = []

    if not ROOT.exists():
        return "<h1>/data not found</h1>", 404

    for p in sorted(ROOT.rglob("*")):
        if not p.is_file():
            continue

        rel = p.relative_to(ROOT).as_posix()

        # Show useful backtest/export files only.
        if p.suffix.lower() not in {".csv", ".txt", ".json"}:
            continue

        size_mb = p.stat().st_size / (1024 * 1024)
        rows.append(
            f'<li style="margin:12px 0">'
            f'<a href="/download/{rel}">{rel}</a>'
            f' &nbsp; <span style="color:#666">({size_mb:.2f} MB)</span>'
            f'</li>'
        )

    body = "\n".join(rows) if rows else "<li>No CSV/TXT/JSON files found under /data.</li>"

    return f"""
    <!doctype html>
    <html>
    <head>
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Railway /data files</title>
    </head>
    <body style="font-family:-apple-system,BlinkMacSystemFont,Arial,sans-serif;
                 max-width:900px;margin:30px auto;padding:0 18px">
      <h1>Railway /data files</h1>
      <p>Tap a file to download it.</p>
      <ul>{body}</ul>
    </body>
    </html>
    """

@app.route("/download/<path:rel_path>")
def download(rel_path):
    p = safe_path(rel_path)
    if not p.exists() or not p.is_file():
        abort(404)
    return send_file(p, as_attachment=True, download_name=p.name)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
