from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
import requests
from bs4 import BeautifulSoup
from fastapi.responses import HTMLResponse
import time



app = FastAPI()

from fastapi.staticfiles import StaticFiles

# Serve index.html and any static files (like CSS, JS)
app.mount("/", StaticFiles(directory=".", html=True), name="static")


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"] ,
    allow_headers=["*"] ,
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
}

CACHE_TTL = 6 * 60 * 60  # 6 hours

financials_cache = {}


# -------------------------
# Screener Fetch
# -------------------------

def fetch_screener(ticker: str):
    url = f"https://www.screener.in/company/{ticker}/"
    r = requests.get(url, headers=HEADERS, timeout=10)
    r.raise_for_status()
    return r.text


def parse_table(soup, section_id):
    section = soup.find("section", {"id": section_id})
    if not section:
        return {}

    table = section.find("table")
    if not table:
        return {}

    rows = table.find_all("tr")
    if len(rows) < 2:
        return {}

    header_cells = rows[0].find_all("th")
    if len(header_cells) < 2:
        return {}

    headers = [th.text.strip() for th in header_cells][1:]
    data = {}

    for row in rows[1:]:
        cells = row.find_all(["td", "th"])
        if len(cells) < 2:
            continue

        key = cells[0].text.strip()
        values = []

        for c in cells[1:]:
            v = c.text.strip().replace(",", "")
            try:
                values.append(float(v))
            except:
                values.append(0.0)

        data[key] = dict(zip(headers, values))

    return data

def latest_value(row: dict):
    if not row:
        return 0
    if "TTM" in row:
        return row["TTM"]
    return list(row.values())[-1]



def build_dcf_inputs(pl, bs, cf):
    revenue = latest_value(pl.get("Sales +", {}))
    ebit = latest_value(pl.get("Operating Profit", {}))
    depreciation = latest_value(pl.get("Depreciation", {}))

    pbt = latest_value(pl.get("Profit before tax", {}))
    pat = latest_value(pl.get("Net Profit +", {}))

    tax_paid = pbt - pat
    tax_rate = tax_paid / pbt if pbt > 0 else 0.25


    fa = bs.get("Fixed Assets +", {})
    years = list(fa.keys())

    if len(years) >= 2:
        capex = fa[years[-1]] - fa[years[-2]] + depreciation
    else:
        capex = depreciation

    capex_pct = capex / revenue if revenue else 0

    oa = bs.get("Other Assets +", {})
    ol = bs.get("Other Liabilities +", {})

    oa_years = list(oa.keys())
    ol_years = list(ol.keys())

    if len(oa_years) >= 2 and len(ol_years) >= 2:
        delta_nwc = (
            (oa[oa_years[-1]] - oa[oa_years[-2]])
            - (ol[ol_years[-1]] - ol[ol_years[-2]])
        )
        nwc_pct = delta_nwc / revenue if revenue else 0
    else:
        nwc_pct = 0



    borrowings = latest_value(bs.get("Borrowings +", {}))
    cash = latest_value(bs.get("Investments", {}))
    net_debt = borrowings - cash

    ebit_margin = ebit / revenue if revenue else 0

    return {
        "revenue": revenue,
        "ebit_margin": round(ebit_margin, 4),
        "tax_rate": round(tax_rate, 4),
        "capex_pct": round(capex_pct, 4),
        "nwc_pct": round(nwc_pct, 4),   # 👈 ADD THIS
        "net_debt": net_debt
    }




@app.get("/api/financials")
def get_financials(ticker: str = Query(...)):
    ticker = ticker.upper()
    now = time.time()

    # ✅ Return cached data if valid
    if ticker in financials_cache:
        cached = financials_cache[ticker]
        if now - cached["timestamp"] < CACHE_TTL:
            return {
                "source": "cache",
                **cached["data"]
            }

    # ❌ Else fetch fresh
    html = fetch_screener(ticker)
    soup = BeautifulSoup(html, "html.parser")

    pl = parse_table(soup, "profit-loss")
    bs = parse_table(soup, "balance-sheet")
    cf = parse_table(soup, "cash-flow")

    data = {
        "profit_loss": pl,
        "balance_sheet": bs,
        "cash_flow": cf
    }

    # ✅ Store in cache
    financials_cache[ticker] = {
        "timestamp": now,
        "data": data
    }

    return {
        "source": "live",
        **data
    }



# -------------------------
# DCF Engine (FCFF)
# -------------------------

@app.post("/api/dcf")
def run_dcf(payload: dict):
    revenue = payload['revenue']
    growth = payload['growth']
    ebit_margin = payload['ebit_margin']
    tax = payload['tax_rate']
    capex_pct = payload['capex_pct']
    nwc_pct = payload['nwc_pct']
    wacc = payload['wacc']
    terminal_growth = payload['terminal_growth']
    net_debt = payload['net_debt']
    shares = payload['shares']

    fcffs = []
    rev = revenue

    for g in growth:
        rev *= (1 + g)
        ebit = rev * ebit_margin
        nopat = ebit * (1 - tax)
        capex = rev * capex_pct
        nwc = rev * nwc_pct
        fcff = nopat - capex - nwc
        fcffs.append(fcff)

    tv = (fcffs[-1] * (1 + terminal_growth)) / (wacc - terminal_growth)

    ev = 0
    for i, f in enumerate(fcffs):
        ev += f / ((1 + wacc) ** (i + 1))

    ev += tv / ((1 + wacc) ** len(fcffs))

    equity = ev - net_debt
    fair_value = equity / shares

    return {
        "enterprise_value": round(ev, 2),
        "equity_value": round(equity, 2),
        "fair_value_per_share": round(fair_value, 2)
    }



