import json
import time
from concurrent.futures import ThreadPoolExecutor
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = 8000
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/%5EDJI?interval=1m&range=1d"
CACHE_SECONDS = 1

_cache = {"time": 0, "data": None}
_stocks_cache = {"time": 0, "data": None}

STOCK_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}?interval=1d&range=1d"
COMPONENTS = {
    "MMM": "3M", "AXP": "American Express", "AMGN": "Amgen", "AMZN": "Amazon",
    "AAPL": "Apple", "BA": "Boeing", "CAT": "Caterpillar", "CVX": "Chevron",
    "CSCO": "Cisco", "KO": "Coca-Cola", "DIS": "Disney", "GS": "Goldman Sachs",
    "HD": "Home Depot", "HON": "Honeywell", "IBM": "IBM", "JNJ": "Johnson & Johnson",
    "JPM": "JPMorgan Chase", "MCD": "McDonald's", "MRK": "Merck", "MSFT": "Microsoft",
    "NKE": "Nike", "NVDA": "Nvidia", "PG": "Procter & Gamble", "CRM": "Salesforce",
    "SHW": "Sherwin-Williams", "TRV": "Travelers", "UNH": "UnitedHealth",
    "VZ": "Verizon", "V": "Visa", "WMT": "Walmart",
}


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


def fetch_stock(symbol):
    req = urllib.request.Request(STOCK_URL.format(symbol), headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            meta = json.load(resp)["chart"]["result"][0]["meta"]
        price = meta["regularMarketPrice"]
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        return {"symbol": symbol, "name": COMPONENTS[symbol], "price": price,
                "change": price - prev, "changePct": (price - prev) / prev * 100}
    except Exception:
        return {"symbol": symbol, "name": COMPONENTS[symbol], "price": None,
                "change": None, "changePct": None}


def fetch_stocks():
    now = time.time()
    if _stocks_cache["data"] and now - _stocks_cache["time"] < 5:
        return _stocks_cache["data"]
    with ThreadPoolExecutor(max_workers=10) as pool:
        data = list(pool.map(fetch_stock, COMPONENTS))
    _stocks_cache.update(time=now, data=data)
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
        elif self.path == "/api/stocks":
            body = json.dumps(fetch_stocks()).encode()
            self.send_response(200)
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
