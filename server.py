import json
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = 8000
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}?interval={}&range={}"

INDICES = {
    "DJI": {"symbol": "^DJI", "name": "Dow Jones"},
    "GSPC": {"symbol": "^GSPC", "name": "S&P 500"},
    "IXIC": {"symbol": "^IXIC", "name": "Nasdaq"},
}

# range key -> (yahoo range, yahoo interval, cache seconds)
RANGES = {
    "1d": ("1d", "1m", 1),
    "5d": ("5d", "5m", 30),
    "1mo": ("1mo", "30m", 60),
    "6mo": ("6mo", "1d", 300),
    "1y": ("1y", "1d", 300),
    "5y": ("5y", "1wk", 600),
}

# Dow 30 is the real membership as of writing. The S&P 500 and Nasdaq lists are
# the largest ~30 companies by weight, not the full index.
COMPANIES = {
    "DJI": {
        "MMM": "3M", "AXP": "American Express", "AMGN": "Amgen", "AMZN": "Amazon",
        "AAPL": "Apple", "BA": "Boeing", "CAT": "Caterpillar", "CVX": "Chevron",
        "CSCO": "Cisco", "KO": "Coca-Cola", "DIS": "Disney", "GS": "Goldman Sachs",
        "HD": "Home Depot", "HON": "Honeywell", "IBM": "IBM", "JNJ": "Johnson & Johnson",
        "JPM": "JPMorgan Chase", "MCD": "McDonald's", "MRK": "Merck", "MSFT": "Microsoft",
        "NKE": "Nike", "NVDA": "Nvidia", "PG": "Procter & Gamble", "CRM": "Salesforce",
        "SHW": "Sherwin-Williams", "TRV": "Travelers", "UNH": "UnitedHealth",
        "VZ": "Verizon", "V": "Visa", "WMT": "Walmart",
    },
    "GSPC": {
        "NVDA": "Nvidia", "MSFT": "Microsoft", "AAPL": "Apple", "AMZN": "Amazon",
        "GOOGL": "Alphabet", "META": "Meta", "AVGO": "Broadcom", "TSLA": "Tesla",
        "BRK-B": "Berkshire Hathaway", "JPM": "JPMorgan Chase", "LLY": "Eli Lilly",
        "V": "Visa", "XOM": "Exxon Mobil", "MA": "Mastercard", "NFLX": "Netflix",
        "WMT": "Walmart", "COST": "Costco", "ORCL": "Oracle", "JNJ": "Johnson & Johnson",
        "PG": "Procter & Gamble", "HD": "Home Depot", "BAC": "Bank of America",
        "ABBV": "AbbVie", "KO": "Coca-Cola", "CVX": "Chevron", "CRM": "Salesforce",
        "AMD": "AMD", "UNH": "UnitedHealth", "CSCO": "Cisco", "MRK": "Merck",
    },
    "IXIC": {
        "NVDA": "Nvidia", "MSFT": "Microsoft", "AAPL": "Apple", "AMZN": "Amazon",
        "GOOGL": "Alphabet", "META": "Meta", "AVGO": "Broadcom", "TSLA": "Tesla",
        "NFLX": "Netflix", "COST": "Costco", "PLTR": "Palantir", "AMD": "AMD",
        "CSCO": "Cisco", "TMUS": "T-Mobile", "ADBE": "Adobe", "PEP": "PepsiCo",
        "LIN": "Linde", "INTU": "Intuit", "QCOM": "Qualcomm", "AMGN": "Amgen",
        "TXN": "Texas Instruments", "ISRG": "Intuitive Surgical", "BKNG": "Booking",
        "AMAT": "Applied Materials", "HON": "Honeywell", "ADP": "ADP",
        "PANW": "Palo Alto Networks", "MU": "Micron", "GILD": "Gilead", "SBUX": "Starbucks",
    },
}

_cache = {}


def cached(key, ttl, producer):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = producer()
    _cache[key] = (now, value)
    return value


def yahoo_chart(symbol, interval, rng):
    url = CHART_URL.format(urllib.parse.quote(symbol, safe=""), interval, rng)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)["chart"]["result"][0]


def summarize(meta):
    price = meta["regularMarketPrice"]
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    return {
        "price": price,
        "prevClose": prev,
        "change": price - prev,
        "changePct": (price - prev) / prev * 100,
        "dayHigh": meta.get("regularMarketDayHigh"),
        "dayLow": meta.get("regularMarketDayLow"),
        "marketTime": meta.get("regularMarketTime"),
    }


def fetch_quote(index, rng):
    yrange, interval, ttl = RANGES[rng]

    def produce():
        result = yahoo_chart(INDICES[index]["symbol"], interval, yrange)
        closes = result["indicators"]["quote"][0]["close"]
        series = [
            {"t": t, "c": c}
            for t, c in zip(result.get("timestamp", []), closes)
            if c is not None
        ]
        data = summarize(result["meta"])
        data["series"] = series
        # For ranges beyond one day, compare against the first point of the range.
        if rng != "1d" and series:
            first = series[0]["c"]
            data["rangeStart"] = first
            data["rangeChange"] = data["price"] - first
            data["rangeChangePct"] = (data["price"] - first) / first * 100
        return data

    return cached(("quote", index, rng), ttl, produce)


def fetch_overview():
    def one(key):
        def produce():
            meta = yahoo_chart(INDICES[key]["symbol"], "1d", "1d")["meta"]
            data = summarize(meta)
            data.update(key=key, name=INDICES[key]["name"])
            return data

        try:
            return cached(("overview", key), 2, produce)
        except Exception:
            return {"key": key, "name": INDICES[key]["name"], "price": None}

    with ThreadPoolExecutor(max_workers=3) as pool:
        return list(pool.map(one, INDICES))


def fetch_stock(symbol, name):
    try:
        meta = yahoo_chart(symbol, "1d", "1d")["meta"]
        s = summarize(meta)
        return {"symbol": symbol, "name": name, "price": s["price"],
                "change": s["change"], "changePct": s["changePct"]}
    except Exception:
        return {"symbol": symbol, "name": name, "price": None,
                "change": None, "changePct": None}


def fetch_stocks(index):
    def produce():
        with ThreadPoolExecutor(max_workers=10) as pool:
            return list(pool.map(lambda kv: fetch_stock(*kv), COMPANIES[index].items()))

    return cached(("stocks", index), 5, produce)


class Handler(BaseHTTPRequestHandler):
    def send_json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        index = q.get("index", ["DJI"])[0]
        rng = q.get("range", ["1d"])[0]

        if url.path in ("/api/quote", "/api/stocks") and index not in INDICES:
            return self.send_json({"error": "unknown index"}, 400)
        if url.path == "/api/quote" and rng not in RANGES:
            return self.send_json({"error": "unknown range"}, 400)

        try:
            if url.path == "/api/quote":
                return self.send_json(fetch_quote(index, rng))
            if url.path == "/api/stocks":
                return self.send_json(fetch_stocks(index))
            if url.path == "/api/overview":
                return self.send_json(fetch_overview())
        except Exception as e:
            return self.send_json({"error": str(e)}, 502)

        if url.path in ("/", "/index.html"):
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
    print(f"Market dashboard running at http://localhost:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
