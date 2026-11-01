#!/usr/bin/env python3
"""Troostwijk deal sniper. Free, stdlib only.

Scrapes one auction, reads CPU/GPU/RAM/storage of every PC, laptop, GPU and
server lot, estimates the used market value and ranks lots by margin versus
the current bid. Only lots closing within --window hours are alerted.

  python sniper.py                 one run, writes report.html, prints top deals
  python sniper.py --serve 8765    run forever + phone web app
  python sniper.py --once          single run

Optional free phone push: set NTFY_TOPIC (ntfy.sh app) and new top deals are sent.
"""
import argparse, datetime as dt, html, json, os, re, statistics, sys, threading, time
import urllib.parse, urllib.request

AUCTION = "faillissement-gamestudio-playerunknown-productions-b-v-A1-50800"
BASE = "https://www.troostwijkauctions.com"
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "details_cache.json")
SEEN = os.path.join(HERE, "alerted.json")

# Buyer's premium + VAT on the premium. CHECK the auction's terms and edit.
BUYER_PREMIUM = 0.19  # "Veilingkosten" 19% of the bid, confirmed in a logged-in account
VAT = 0.0  # you reclaim the VAT as a business buyer, so it is not a cost (set 0.21 if you can't)
OVERHEAD = BUYER_PREMIUM * (1 + VAT)  # extra cost on top of the hammer price
# You want to pay at most this share of the used resale value, all costs in.
TARGET_SHARE = 0.75

WANTED = re.compile(
    r"desktop|laptop|\bpc\b|notebook|thinkpad|thinkbook|macbook|workstation|server|"
    r"grafische|\bgpu\b|rtx|gtx|radeon|geforce|\brx ?\d|nvidia|omen|titan|aorus", re.I)

# --- used market values in EUR (Dutch second hand, rough; edit freely) -------
GPU = {
    "4090": 1500, "4080 super": 900, "4080": 850, "4070 ti super": 700, "4070 ti": 600,
    "4070 super": 520, "4070": 450, "4060 ti": 330, "4060": 250,
    "3090 ti": 750, "3090": 650, "3080 ti": 450, "3080": 380, "3070 ti": 280,
    "3070": 250, "3060 ti": 210, "3060": 170, "3050": 120,
    "2080 ti": 220, "2080 super": 180, "2080": 160, "2070 super": 150, "2070": 130,
    "2060 super": 120, "2060": 100,
    "1660 ti": 85, "1660 super": 90, "1660": 75, "1650 super": 70, "1650": 55,
    "1080 ti": 150, "1080": 100, "1070 ti": 85, "1070": 75, "1060": 55, "1050 ti": 40,
    "7900 xtx": 700, "7900 xt": 550, "7800 xt": 350, "7700 xt": 280, "7600": 180,
    "6950 xt": 330, "6900 xt": 300, "6800 xt": 260, "6800": 220, "6750 xt": 190,
    "6700 xt": 170, "6700": 150, "6650 xt": 140, "6600 xt": 120, "6600": 105, "6500 xt": 70,
    "5700 xt": 110, "5700": 95, "5600 xt": 80, "580": 40,
    "a770": 160, "a750": 130,
}
LAPTOP_GPU_FACTOR = 0.75  # mobile GPUs are worth less than desktop cards

CPU = [  # (regex, desktop value)
    (r"i9[- ]1[234]\d{3}", 260), (r"i9[- ]9\d{3}|i9[- ]10\d{3}", 160),
    (r"i7[- ]1[34]\d{3}", 220), (r"i7[- ]12\d{3}", 170), (r"i7[- ]11\d{3}", 130),
    (r"i7[- ]10\d{3}", 110), (r"i7[- ]9\d{3}", 85), (r"i7[- ]8\d{3}", 70), (r"i7[- ]7\d{3}", 45),
    (r"i5[- ]1[34]\d{3}", 150), (r"i5[- ]12\d{3}", 110), (r"i5[- ]11\d{3}", 85),
    (r"i5[- ]10\d{3}", 70), (r"i5[- ]9\d{3}", 55), (r"i5[- ]8\d{3}", 45), (r"i5[- ]7\d{3}", 30),
    (r"i3[- ]1[234]\d{3}", 70), (r"i3[- ]10\d{3}|i3[- ]9\d{3}", 40), (r"i3[- ]8\d{3}", 35),
    (r"ryzen 9 7\d{3}", 280), (r"ryzen 9 5\d{3}", 220), (r"ryzen 9 3\d{3}", 150),
    (r"ryzen 7 7\d{3}", 200), (r"ryzen 7 5\d{3}", 130), (r"ryzen 7 3\d{3}", 90), (r"ryzen 7 [12]\d{3}", 60),
    (r"ryzen 5 7\d{3}", 150), (r"ryzen 5 5\d{3}", 90), (r"ryzen 5 3\d{3}", 65), (r"ryzen 5 [12]\d{3}", 40),
    (r"ryzen 3", 40), (r"xeon", 60), (r"threadripper", 250),
]
LAPTOP_BASE = 90  # screen, chassis, battery, keyboard


def get(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            return urllib.request.urlopen(req, timeout=30).read().decode("utf8", "replace")
        except Exception as e:
            if i == tries - 1:
                print(f"  ! {url[-60:]}: {e}", file=sys.stderr)
                return None
            time.sleep(2)


def next_data(page):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    return json.loads(m.group(1))["props"]["pageProps"] if m else None


def load_json(path, default):
    try:
        with open(path, encoding="utf8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf8") as f:
        json.dump(data, f)


# --- live second hand prices (Marktplaats public search, no key) --------------
PRICES = os.path.join(HERE, "prices_cache.json")
PRICE_TTL = 12 * 3600
HAGGLE = 0.92  # asking price -> typical paid price
BAD = re.compile(r"defect|kapot|onderdelen|niet werkend|gezocht|ruil|gebroken|artefact|"
                 r"\bbox\b|doos leeg|koeler|waterblok", re.I)
NOT_CARD = re.compile(r"\bpc\b|computer|laptop|desktop|gaming ?pc|\bset\b|bundle|systeem|"
                      r"\bi[3579][- ]|ryzen|moederbord|\bcpu\b", re.I)
_price_cache = None


def _token_rx(token):
    """'2080 ti' -> regex; plain '2080' must not match '2080 ti/super'."""
    parts = token.lower().split()
    rx = r"(?<![\d])" + r"[\s-]?".join(re.escape(p) for p in parts) + r"(?![\d])"
    if len(parts) == 1 and parts[0].isdigit():
        rx += r"(?![\s-]?(ti|super|xt)\b)"
    return re.compile(rx, re.I)


def mp_search(query):
    url = ("https://www.marktplaats.nl/lrp/api/search?limit=100&offset=0&query="
           + urllib.parse.quote(query))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        data = json.loads(urllib.request.urlopen(req, timeout=30).read().decode("utf8"))
    except Exception as e:
        print(f"  ! marktplaats '{query}': {e}", file=sys.stderr)
        return None
    time.sleep(0.8)
    return [(x["priceInfo"]["priceCents"] / 100, x["title"]) for x in data.get("listings", [])
            if x.get("priceInfo", {}).get("priceType") in ("FIXED", "MIN_BID", "NEGOTIABLE")
            and x["priceInfo"].get("priceCents", 0) >= 2000]


def live_price(query, must, card_only=False, lo=20, hi=4000):
    """Median asking price (cheaper half) of matching ads, or None if too few. -> (eur, n)."""
    global _price_cache
    if _price_cache is None:
        _price_cache = load_json(PRICES, {})
    key = query + "|" + must + str(card_only)
    c = _price_cache.get(key)
    if c and time.time() - c["t"] < PRICE_TTL:
        return (c["v"], c["n"]) if c["v"] else None
    res = mp_search(query)
    if res is None:
        return (c["v"], c["n"]) if c and c["v"] else None
    rx = _token_rx(must)
    prices = sorted(p for p, t in res if lo <= p <= hi and rx.search(t) and not BAD.search(t)
                    and not (card_only and NOT_CARD.search(t)))
    out = None
    if len(prices) >= 4:
        half = prices[: max(4, (len(prices) + 1) // 2 + 1)]
        out = (round(statistics.median(half) * HAGGLE), len(prices))
    _price_cache[key] = {"t": time.time(), "v": out[0] if out else 0, "n": out[1] if out else 0}
    save_json(PRICES, _price_cache)
    return out


def canon_gpu(k):
    if k.startswith("a7"):
        return "arc " + k
    if re.match(r"(40|30|20)\d0", k):
        return "rtx " + k
    if re.match(r"(16|10)\d0", k):
        return "gtx " + k
    return "rx " + k


def gpu_price(k, table_value):
    lp = live_price(canon_gpu(k) + " videokaart", k, card_only=True, lo=25)
    return (lp[0], f"live n={lp[1]}") if lp else (table_value, "tabel")


def cpu_price(cpu_text, table_value):
    m = (re.search(r"(i[3579][- ]\d{4,5}[A-Z]{0,2})", cpu_text)
         or re.search(r"(ryzen [3579] \d{4}[A-Z0-9]{0,2})", cpu_text, re.I))
    if not m:
        return table_value, "tabel"
    name = m.group(1)
    lp = live_price(name + " processor", name.replace("-", " "), card_only=False, lo=15, hi=1200)
    return (lp[0], f"live n={lp[1]}") if lp else (table_value, "tabel")


def laptop_price(title):
    m = re.search(r"LENOVO - (\w*\d\w{3,})", title, re.I)
    if not m:
        return None
    code = m.group(1)[:6]
    lp = live_price("lenovo " + code, code, card_only=False, lo=100, hi=3000)
    return (lp[0], f"live n={lp[1]}") if lp else None


def fetch_lots():
    lots, page = [], 1
    while True:
        pp = next_data(get(f"{BASE}/a/{AUCTION}?page={page}") or "")
        if not pp:
            break
        res = pp["lots"]
        lots += res["results"]
        if not res["hasNext"]:
            break
        page += 1
        time.sleep(0.7)
    return lots


def fetch_detail(lot, cache):
    key = lot["displayId"]
    c = cache.get(key)
    # refresh remarks/condition cheaply: attributes never change, so cache forever
    if c:
        return c
    pp = next_data(get(f"{BASE}/l/{lot['urlSlug']}") or "")
    time.sleep(0.7)
    if not pp:
        return None
    L = pp["lot"]
    d = {
        "attrs": {a["name"]: a["value"] for a in L.get("attributes", [])},
        "remarks": L.get("remarks") or "",
        "desc": (L.get("description") or {}).get("description") or "",
        "qty": L.get("quantity") or 1,
    }
    cache[key] = d
    return d


def gpu_value(text):
    t = text.lower().replace("-", " ")
    best = None
    for k, v in GPU.items():
        # model number must appear as its own token, longest key wins
        if re.search(r"(?<![\d])" + re.escape(k) + r"(?![\d])", t):
            if best is None or len(k) > len(best[0]):
                best = (k, v)
    return best


def cpu_value(text):
    t = text.lower()
    for rx, v in CPU:
        if re.search(rx, t):
            return v
    return None


def size_gb(txt, unit_rx):
    return sum(float(n) * (1000 if u.lower() == "tb" else 1)
               for n, u in re.findall(r"(\d+(?:\.\d+)?)\s*(GB|TB)", txt, re.I))


def kind_of(title):
    if re.search(r"laptop|notebook|thinkpad|thinkbook|macbook|omen|titan", title, re.I):
        return "laptop"
    if re.search(r"server", title, re.I):
        return "server"
    if re.search(r"grafische|\bgpu\b|videokaart", title, re.I):
        return "gpu"
    return "pc"


def estimate(lot, det):
    """Return (value, spec, confidence, source) or None."""
    a = det["attrs"] if det else {}
    title = lot["title"]
    kind = kind_of(title)
    is_laptop = kind == "laptop"
    gpu_txt = a.get("Type videokaart", "")
    gpu = gpu_value(gpu_txt) or gpu_value(title)
    cpu_txt = a.get("Processor type", "")
    cpu = cpu_value(cpu_txt)
    ram = re.search(r"Total:\s*(\d+)\s*GB", a.get("RAM Geheugen Type", ""))
    ram = int(ram.group(1)) if ram else 0
    ssd = hdd = 0
    for part in a.get("Type datadrager", "").split("+"):
        if re.search(r"HDD", part, re.I):
            hdd += size_gb(part, "")
        else:
            ssd += size_gb(part.split("(")[0], "")
    spec = f"{cpu_txt[:34]} | {ram}GB | {int(ssd)}GB SSD"
    if gpu_txt:
        spec += f" | {gpu_txt[:44]}"

    if is_laptop:  # same model code on Marktplaats is the best comparable
        lp = laptop_price(title)
        if lp:
            return lp[0], spec, "high", "markt " + lp[1]

    qty = (det or {}).get("qty") or 1
    if not cpu and gpu and kind == "gpu":
        v, src = gpu_price(*gpu)
        return v * qty, f"GPU {gpu[0]} x{qty}", "high", src
    if not cpu and not gpu:
        return None

    val, srcs = 0, []
    if cpu:
        v, s = cpu_price(cpu_txt, cpu)
        val += v * (0.8 if is_laptop else 1)
        srcs.append("cpu " + s)
    if gpu:
        v, s = gpu_price(*gpu)
        val += v * (LAPTOP_GPU_FACTOR if is_laptop else 1)
        srcs.append("gpu " + s)
    val += ram * 2.0 + ssd * 0.07 + hdd * 0.02 + (LAPTOP_BASE if is_laptop else 90)
    if kind == "server":
        val = max(val, 150)
    if not det:
        conf = "low"
    else:
        conf = "high" if (cpu and (gpu or is_laptop) and ram) else "medium"
    return val, spec, conf, ", ".join(srcs)


def short_desc(lot, det):
    a = det["attrs"] if det else {}
    cpu = re.search(r"(i[3579][- ]\d{4,5}[A-Z]{0,2}|Ryzen [3579] \d{4}[A-Z0-9]{0,2}|Xeon\S*(?: \S+)?)",
                    a.get("Processor type", ""), re.I)
    g = gpu_value(a.get("Type videokaart", "")) or gpu_value(lot["title"])
    ram = re.search(r"Total:\s*(\d+)\s*GB", a.get("RAM Geheugen Type", ""))
    gb = size_gb(re.sub(r"\([^)]*\)", "", a.get("Type datadrager", "")), "")
    parts = [cpu.group(1) if cpu else None, canon_gpu(g[0]).upper() if g else None,
             f"{ram.group(1)}GB RAM" if ram else None,
             (f"{gb / 1000:g}TB" if gb >= 1000 else f"{gb:g}GB") if gb else None]
    return " · ".join(p for p in parts if p) or lot["title"]


def analyse():
    print(time.strftime("%H:%M"), "loading lots ...", flush=True)
    lots = fetch_lots()
    now = time.time()
    cache = load_json(CACHE, {})
    rows = []
    cand = [l for l in lots if WANTED.search(l["title"])]
    for l in cand:
        if l.get("biddingStatus") != "BIDDING_OPEN":
            continue
        det = fetch_detail(l, cache)
        est = estimate(l, det)
        bid = l["currentBidAmount"]["cents"] / 100
        r = dict(lot=l, bid=bid, kind=kind_of(l["title"]), short=short_desc(l, det), value=None, spec=l["title"], conf="none",
                 src="", max_bid=None, margin=None, ratio=None, warn=(det or {}).get("remarks", ""),
                 end=l["endDate"])
        if est:
            value, spec, conf, src = est
            all_in = bid * (1 + OVERHEAD)
            r.update(value=value, spec=spec, conf=conf, src=src,
                     max_bid=value * TARGET_SHARE / (1 + OVERHEAD),
                     margin=value - all_in, ratio=all_in / value if value else None)
        rows.append(r)
    save_json(CACHE, cache)
    rows.sort(key=lambda r: (r["ratio"] is None, r["ratio"] if r["ratio"] is not None else 9))
    return rows


def fmt_end(ts):
    left = ts - time.time()
    d = dt.datetime.fromtimestamp(ts).strftime("%a %d-%m %H:%M")
    return f"{d} (over {int(left // 3600)}u{int(left % 3600 // 60):02d})"


def link(l):
    return f"{BASE}/l/{l['urlSlug']}"


def print_top(rows, n):
    good = [r for r in rows if r["ratio"] is not None]
    print(f"\n{'#':>2} {'ratio':>5} {'bid':>6} {'value':>6} {'max':>6}  lot / spec")
    for i, r in enumerate(good[:n], 1):
        l = r["lot"]
        print(f"{i:>2} {r['ratio']*100:>4.0f}% {r['bid']:>6.0f} {r['value']:>6.0f} {r['max_bid']:>6.0f}  "
              f"{l['displayId'].split('-')[-1]} {l['title'][:30]} [{r['conf']}] {r['src']} | closes {fmt_end(l['endDate'])}")


# --- web app -----------------------------------------------------------------
PAGE = """<!doctype html><html lang=nl><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Sniper</title>
<style>
:root{--bg:#f4f5f7;--card:#fff;--tx:#14171c;--mut:#66707d;--g:#16a34a;--o:#d97706;--r:#dc2626;--bd:#e3e6ea}
@media(prefers-color-scheme:dark){:root{--bg:#0e1116;--card:#171b22;--tx:#e8ebef;--mut:#8b95a3;--bd:#262c36}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:15px/1.4 system-ui,sans-serif}
header{position:sticky;top:0;background:var(--bg);padding:12px 16px 8px;border-bottom:1px solid var(--bd);z-index:2}
h1{font-size:17px;margin:0 0 2px}small{color:var(--mut)}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.chips button{border:1px solid var(--bd);background:var(--card);color:var(--tx);border-radius:99px;padding:5px 12px;font:inherit}
.chips button.on{background:var(--tx);color:var(--bg)}
main{padding:12px 16px;display:grid;gap:10px;max-width:760px;margin:auto}
.c{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:12px;border-left:5px solid var(--bd)}
.c.g{border-left-color:var(--g)}.c.o{border-left-color:var(--o)}.c.r{border-left-color:var(--r)}
.t{display:flex;justify-content:space-between;gap:8px;align-items:baseline}
.t a{color:inherit;font-weight:600;text-decoration:none}.rt{font-weight:700;white-space:nowrap}
.g .rt{color:var(--g)}.o .rt{color:var(--o)}.r .rt{color:var(--r)}
.n{display:flex;gap:14px;margin:6px 0;flex-wrap:wrap}.n b{display:block;font-size:17px}
.n span{font-size:12px;color:var(--mut)}.sp{color:var(--mut);font-size:12.5px}
.soon{color:var(--r);font-weight:600}
</style>
<header><h1>Troostwijk sniper</h1><small id=meta>__META__</small>
<div class=chips id=chips></div></header><main id=list></main>
<script>
const rows=__ROWS__;let kind="all",win=true;
const now=Date.now()/1e3;
function left(e){let s=e-Date.now()/1e3;if(s<0)return"gesloten";let h=Math.floor(s/3600),m=Math.floor(s%3600/60);
return h>=24?Math.floor(h/24)+"d "+h%24+"u":h+"u "+String(m).padStart(2,"0")+"m"}
function draw(){const L=document.getElementById("list");L.innerHTML="";
rows.filter(r=>(kind=="all"||r.kind==kind)&&(!win||r.end-now<=172800)&&r.ratio!=null).forEach(r=>{
const cl=r.ratio<.6?"g":r.ratio<.85?"o":"r";const soon=r.end-Date.now()/1e3<3600;
L.insertAdjacentHTML("beforeend",`<div class="c ${cl}"><div class=t><a href="${r.url}" target=_blank>${r.title}</a>
<span class=rt>${Math.round(r.ratio*100)}%</span></div>
<div class=n><div><b>€${Math.round(r.bid)}</b><span>bod (${r.bids}x)</span></div>
<div><b>€${Math.round(r.value)}</b><span>waarde</span></div>
<div><b>€${Math.round(r.max)}</b><span>max bod</span></div>
<div><b class="${soon?"soon":""}">${left(r.end)}</b><span>sluit</span></div></div>
<div class=sp>${r.spec}<br>${r.src} · zekerheid ${r.conf}${r.warn?"<br>⚠ "+r.warn:""}</div></div>`)})}
const K={all:"Alles",pc:"PC",laptop:"Laptop",gpu:"GPU",server:"Server"};
const C=document.getElementById("chips");
function chips(){C.innerHTML="";for(const k in K)C.insertAdjacentHTML("beforeend",
`<button class="${k==kind?"on":""}" onclick="kind='${k}';chips();draw()">${K[k]}</button>`);
C.insertAdjacentHTML("beforeend",`<button class="${win?"on":""}" onclick="win=!win;chips();draw()">laatste 2 dagen</button>`)}
chips();draw();setTimeout(()=>location.reload(),60000);
</script></html>"""

STATE = {"html": "<p>Nog aan het laden, ververs over een minuut.", "rows": []}


def build_page(rows):
    data = [dict(title=r["lot"]["displayId"].split('-')[-1] + " " + r["lot"]["title"], url=link(r["lot"]),
                 kind=r["kind"], ratio=r["ratio"], bid=r["bid"], bids=r["lot"]["bidsCount"],
                 value=r["value"], max=r["max_bid"], end=r["end"], spec=html.escape(r["spec"]),
                 src=html.escape(r["src"]), conf=r["conf"], warn=html.escape((r["warn"] or "")[:160]))
            for r in rows if r["ratio"] is not None]
    meta = (f"bijgewerkt {dt.datetime.now():%d-%m %H:%M} · kosten = bod + {BUYER_PREMIUM:.0%} opgeld, excl. btw "
            f"· waarde = Marktplaats mediaan x{HAGGLE}")
    return (PAGE.replace("__META__", html.escape(meta))
            .replace("__ROWS__", json.dumps(data).replace("</", "<\\/")))


def serve(port):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = STATE["html"].encode("utf8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()


def lan_ip():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "localhost"
    finally:
        s.close()


# --- alerts --------------------------------------------------------------------
CONFIG = os.path.join(HERE, "config.json")


def topic():
    t = os.environ.get("NTFY_TOPIC")
    if t:
        return t
    cfg = load_json(CONFIG, {})
    if "ntfy_topic" not in cfg:
        import secrets
        cfg["ntfy_topic"] = "snipe-" + secrets.token_hex(6)
        save_json(CONFIG, cfg)
    return cfg["ntfy_topic"]


ICON_URL = "https://raw.githubusercontent.com/bilalramzipxl/troostwijk-sniper/main/assets/white-death.png"
DAYS = ["ma", "di", "wo", "do", "vr", "za", "zo"]
ICON = {"pc": "🖥️", "laptop": "💻", "gpu": "🎮", "server": "🗄️"}


def nl_time(ts):
    d = dt.datetime.fromtimestamp(ts)
    return f"{DAYS[d.weekday()]} {d:%H:%M}"


def nl_left(sec):
    sec = max(0, int(sec))
    if sec >= 86400:
        return f"{sec // 86400}d {sec % 86400 // 3600}u"
    if sec >= 3600:
        return f"{sec // 3600}u{sec % 3600 // 60:02d}"
    return f"{sec // 60} min"


def hdr(s):
    """HTTP headers must be latin-1; ntfy accepts RFC 2047 for anything else."""
    try:
        s.encode("ascii")
        return s
    except UnicodeEncodeError:
        import base64
        return "=?UTF-8?B?" + base64.b64encode(s.encode("utf8")).decode() + "?="


def push(title, msg, url, prio="default", actions=()):
    print("PUSH:", title, "\n" + msg, flush=True)
    h = {"Title": hdr(title), "Click": url, "Priority": prio, "Icon": ICON_URL}
    if actions:
        h["Actions"] = "; ".join(f"view, {n}, {u}" for n, u in actions[:3])
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"https://ntfy.sh/{topic()}", data=msg.encode("utf8"), headers=h), timeout=15)
    except Exception as e:
        print("  ntfy failed:", e)
    discord(title, msg, url, actions)


def discord(title, msg, url, actions=()):
    """Post to a Discord channel webhook (DISCORD_WEBHOOK env, kept as a secret)."""
    hook = os.environ.get("DISCORD_WEBHOOK")
    if not hook:
        return
    links = " · ".join(f"[{n}]({u})" for n, u in actions) or f"[Open lot]({url})"
    content = f"**{title}**\n{msg}\n{links}"[:1900]
    try:
        urllib.request.urlopen(urllib.request.Request(
            hook, data=json.dumps({"content": content, "flags": 4,  # flags 4: no link previews
                             "username": "The White Death", "avatar_url": ICON_URL}).encode("utf8"),
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}), timeout=15)
    except Exception as e:
        print("  discord failed:", e)


def lot_no(r):
    return r["lot"]["displayId"].split("-")[-1]


def block(r, now):
    """Four short lines per lot."""
    l = r["lot"]
    name = l["title"].replace("Laptop - ", "").replace("LENOVO", "Lenovo")
    return (f"{ICON.get(r['kind'], '')} #{lot_no(r)} {name}\n"
            f"{r['short']}\n"
            f"Bod €{r['bid']:.0f} → max €{r['max_bid']:.0f} (waarde €{r['value']:.0f}) | betaalprijs €{r['bid'] * (1 + OVERHEAD):.0f}\n"
            f"Sluit {nl_time(r['end'])} · nog {nl_left(r['end'] - now)}")


def digest_gap(soonest):
    """Seconds between digests; faster as the first lot gets closer."""
    h = soonest / 3600
    if h < 1:
        return 10 * 60
    if h < 3:
        return 30 * 60
    if h < 24:
        return 60 * 60
    return 3 * 3600


def notify(rows, window_h, each_run=False, top_n=6):
    """each_run: the scheduler decides the cadence, so every run sends one digest and nothing else."""
    seen = load_json(SEEN, {})
    now = time.time()
    live = [r for r in rows if r["ratio"] is not None and r["conf"] not in ("low", "none")
            and 0 < r["end"] - now <= window_h * 3600]
    if not live:
        return
    soonest = min(r["end"] for r in live) - now
    quiet = dt.datetime.now().hour < 7 and soonest > 6 * 3600
    if each_run or (not quiet and now - seen.get("_digest", 0) >= digest_gap(soonest)):
        good = sorted((r for r in live if r["bid"] < r["max_bid"]), key=lambda r: -r["margin"])[:top_n]
        if good:
            push(f"🔥 Top {len(good)} deals · eerste sluit over {nl_left(soonest)}",
                 "\n\n".join(block(r, now) for r in good), link(good[0]["lot"]),
                 actions=[(f"#{lot_no(r)}", link(r["lot"])) for r in good])
        elif each_run or soonest < 24 * 3600:
            push("Geen deals onder je max bod", f"Nog {len(live)} lots open, alles is nu te duur.",
                 link(live[0]["lot"]))
        seen["_digest"] = now
    if each_run:
        save_json(SEEN, seen)
        return
    # one push per lot, 60 and 15 minutes before it closes, while it is still under your max bid
    for r in sorted(live, key=lambda r: r["end"]):
        l = r["lot"]
        left = r["end"] - now
        s = seen.setdefault(l["displayId"], {})
        if r["bid"] >= r["max_bid"] or r["ratio"] > 0.7:
            continue
        for mins, key, icon, prio in ((15, "t15", "🚨", "urgent"), (60, "t60", "⏰", "high")):
            if left <= mins * 60 and not s.get(key):
                if key == "t15":
                    s["t60"] = True
                s[key] = True
                push(f"{icon} Nog {nl_left(left)} · #{lot_no(r)} bod €{r['bid']:.0f} (max €{r['max_bid']:.0f})",
                     block(r, now), link(l), prio, actions=[("Open lot", link(l))])
                break
    save_json(SEEN, seen)


def cycle(a):
    rows = analyse()
    print_top(rows, a.top)
    STATE["html"] = build_page(rows)
    notify(rows, a.window, a.each_run)
    soonest = min((r["end"] - time.time() for r in rows if r["end"] > time.time()), default=9e9)
    return 120 if soonest < 3600 else 300 if soonest < 86400 else a.every * 60


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=float, default=48, help="alert only for lots closing within N hours")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--every", type=int, default=10, help="minutes between runs")
    ap.add_argument("--serve", type=int, default=0, metavar="PORT", help="run forever + web app on PORT")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--each-run", action="store_true", help="send one digest per run (cron sets the cadence)")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf8", errors="replace")
    if a.serve:
        threading.Thread(target=serve, args=(a.serve,), daemon=True).start()
        print(f"web app:  http://{lan_ip()}:{a.serve}   (phone on same wifi)")
        print(f"push:     install the ntfy app, subscribe to topic  {topic()}", flush=True)
    while True:
        try:
            wait = cycle(a)
        except Exception as e:
            print("cycle failed:", e, flush=True)
            wait = 120
        with open(os.path.join(HERE, "report.html"), "w", encoding="utf8") as f:
            f.write(STATE["html"])
        if a.once:
            break
        time.sleep(wait)


if __name__ == "__main__":
    main()
