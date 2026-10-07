import json
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = 8000
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/%5EDJI?interval=1m&range=1d"
CACHE_SECONDS = 1

_cache = {"time": 0, "data": None}


def fetch_quote():
    now = time.time()
    if _cache["data"] and now - _cache["time"] < CACHE_SECONDS:
        return _cache["data"]

    req = urllib.request.Request(YAHOO_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = json.load(resp)

    result = raw["chart"]["result"][0]
    meta = result["meta"]
    closes = result["indicators"]["quote"][0]["close"]
    points = [
        {"t": t, "c": c}
        for t, c in zip(result.get("timestamp", []), closes)
        if c is not None
    ]

    price = meta["regularMarketPrice"]
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    data = {
        "price": price,
        "prevClose": prev,
        "change": price - prev,
        "changePct": (price - prev) / prev * 100,
        "dayHigh": meta.get("regularMarketDayHigh"),
        "dayLow": meta.get("regularMarketDayLow"),
        "marketTime": meta.get("regularMarketTime"),
        "series": points,
    }
    _cache.update(time=now, data=data)
    return data


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/quote":
            try:
                body = json.dumps(fetch_quote()).encode()
                self.send_response(200)
            except Exception as e:
                body = json.dumps({"error": str(e)}).encode()
                self.send_response(502)
            self.send_header("Content-Type", "application/json")
        elif self.path in ("/", "/index.html"):
            body = (Path(__file__).parent / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        else:
            body = b"Not found"
            self.send_response(404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print(f"Dow Jones dashboard running at http://localhost:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
