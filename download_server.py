#!/usr/bin/env python3
import os
from pathlib import Path
from flask import Flask, send_from_directory, render_template_string, abort

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "eth_data"

FILES = [
    ("eth_15m_sessions_15k.csv", "ETH sessions"),
    ("eth_15m_candles_15k.csv", "ETH 1-minute candles"),
    ("eth_15m_candle_failures.csv", "Skipped candle markets"),
    ("eth_15m_download_report.txt", "Download report"),
]

app = Flask(__name__)

PAGE = """
<!doctype html>
<html>
<head>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ETH Kalshi Downloads</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 24px; }
    h1 { font-size: 24px; }
    p { line-height: 1.4; }
    a { display:block; padding:16px; margin:12px 0; border:1px solid #ccc;
        border-radius:10px; text-decoration:none; font-size:18px; }
    .missing { opacity:.5; margin:12px 0; }
  </style>
</head>
<body>
  <h1>ETH Kalshi Downloads</h1>
  <p>Tap a file to download it to your phone.</p>
  {% for item in files %}
    {% if item.exists %}
      <a href="/download/{{ item.name }}">{{ item.label }}<br><small>{{ item.name }}</small></a>
    {% else %}
      <div class="missing">{{ item.label }} â missing</div>
    {% endif %}
  {% endfor %}
</body>
</html>
"""

@app.route("/")
def index():
    items = []
    for name, label in FILES:
        p = DATA_DIR / name
        items.append({"name": name, "label": label, "exists": p.exists()})
    return render_template_string(PAGE, files=items)

@app.route("/download/<path:filename>")
def download(filename):
    allowed = {name for name, _ in FILES}
    if filename not in allowed:
        abort(404)
    path = DATA_DIR / filename
    if not path.exists():
        abort(404)
    return send_from_directory(DATA_DIR, filename, as_attachment=True)

@app.route("/health")
def health():
    return {"ok": True}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
