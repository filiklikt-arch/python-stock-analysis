import html
import http.cookiejar
import json
import re
import threading
import time
import urllib.error
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

UA = {"User-Agent": "Mozilla/5.0"}
WIKI_UA = {"User-Agent": "market-dashboard/1.0 (learning project)"}
SP500_WIKI = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote?symbols={}&crumb={}"
CRUMB_URL = "https://query1.finance.yahoo.com/v1/test/getcrumb"
QUOTE_CHUNK = 200

# Built-in lists. The S&P 500 is fetched live from Wikipedia; this fallback is used
# only if that fails.
DOW = {
    "MMM": "3M", "AXP": "American Express", "AMGN": "Amgen", "AMZN": "Amazon",
    "AAPL": "Apple", "BA": "Boeing", "CAT": "Caterpillar", "CVX": "Chevron",
    "CSCO": "Cisco", "KO": "Coca-Cola", "DIS": "Disney", "GS": "Goldman Sachs",
    "HD": "Home Depot", "HON": "Honeywell", "IBM": "IBM", "JNJ": "Johnson & Johnson",
    "JPM": "JPMorgan Chase", "MCD": "McDonald's", "MRK": "Merck", "MSFT": "Microsoft",
    "NKE": "Nike", "NVDA": "Nvidia", "PG": "Procter & Gamble", "CRM": "Salesforce",
    "SHW": "Sherwin-Williams", "TRV": "Travelers", "UNH": "UnitedHealth",
    "VZ": "Verizon", "V": "Visa", "WMT": "Walmart",
}
SP500_FALLBACK = {
    s: s for s in "NVDA MSFT AAPL AMZN GOOGL META AVGO TSLA BRK-B JPM LLY V XOM MA NFLX WMT "
    "COST ORCL JNJ PG HD BAC ABBV KO CVX CRM AMD UNH CSCO MRK".split()
}
# Nasdaq-100 (approximate; names come from Yahoo). Symbols Yahoo doesn't recognise are dropped.
NASDAQ100 = {s: None for s in (
    "AAPL MSFT NVDA AMZN GOOGL GOOG META AVGO TSLA NFLX COST PLTR ASML AMD CSCO TMUS AZN LIN PEP "
    "ISRG ADBE QCOM TXN INTU AMGN BKNG HON AMAT PDD ARM GILD CMCSA PANW ADP VRTX MU LRCX ADI KLAC "
    "APP MELI SBUX CRWD INTC MDLZ CEG CDNS REGN ORLY PYPL MAR SNPS CTAS MRVL ABNB FTNT ADSK DASH "
    "WDAY ROP NXPI PCAR CPRT MNST CSX AEP TTWO CHTR FAST KDP ROST PAYX DDOG BKR IDXX ODFL EA XEL "
    "VRSK EXC FANG LULU CCEP TEAM CTSH GEHC KHC ZS ON CDW TTD BIIB MDB GFS WBD CSGP MCHP DXCM "
    "TRI FER CSX LIN SHOP"
).split()}

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


_session = {"opener": None, "crumb": None}
_session_lock = threading.Lock()


def yahoo_session(force=False):
    with _session_lock:
        if _session["crumb"] and not force:
            return _session["opener"], _session["crumb"]
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        try:
            opener.open(urllib.request.Request("https://fc.yahoo.com", headers=UA), timeout=10)
        except urllib.error.HTTPError:
            pass  # a 404 is expected; the point is the cookie it sets
        crumb = opener.open(urllib.request.Request(CRUMB_URL, headers=UA), timeout=10).read().decode()
        _session.update(opener=opener, crumb=crumb)
        return opener, crumb


def quote_chunk(symbols):
    for attempt in (0, 1):
        opener, crumb = yahoo_session(force=attempt == 1)
        url = QUOTE_URL.format(urllib.parse.quote(",".join(symbols), safe=","),
                               urllib.parse.quote(crumb, safe=""))
        try:
            with opener.open(urllib.request.Request(url, headers=UA), timeout=15) as resp:
                return json.load(resp)["quoteResponse"]["result"]
        except urllib.error.HTTPError as e:
            if attempt == 1 or e.code not in (401, 403):
                raise


def batch_quotes(symbols):
    chunks = [symbols[i:i + QUOTE_CHUNK] for i in range(0, len(symbols), QUOTE_CHUNK)]
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = pool.map(quote_chunk, chunks)
    return {q["symbol"]: q for chunk in results for q in chunk}


def sp500_companies():
    def produce():
        req = urllib.request.Request(SP500_WIKI, headers=WIKI_UA)
        page = urllib.request.urlopen(req, timeout=15).read().decode()
        table = re.search(r'<table[^>]*id="constituents"[^>]*>(.*?)</table>', page, re.S).group(1)
        out = {}
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S)[1:]:
            cells = [html.unescape(re.sub(r"<[^>]+>", "", c)).strip()
                     for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
            if len(cells) >= 2:
                out[cells[0].replace(".", "-")] = cells[1]
        if len(out) < 400:
            raise ValueError("S&P 500 table looks incomplete")
        return out

    try:
        return cached(("sp500-list",), 6 * 3600, produce)
    except Exception:
        return SP500_FALLBACK


def companies_for(index):
    if index == "DJI":
        return DOW
    if index == "GSPC":
        return sp500_companies()
    return NASDAQ100


def fetch_stocks(index):
    def produce():
        companies = companies_for(index)
        quotes = batch_quotes(list(companies))
        rows = []
        for sym, name in companies.items():
            q = quotes.get(sym)
            if not q or q.get("regularMarketPrice") is None:
                continue
            rows.append({
                "symbol": sym,
                "name": name or q.get("shortName") or q.get("longName") or sym,
                "price": q["regularMarketPrice"],
                "change": q.get("regularMarketChange") or 0,
                "changePct": q.get("regularMarketChangePercent") or 0,
            })
        if not rows:
            raise ValueError("no quotes returned")
        return rows

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
