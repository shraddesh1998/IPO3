"""Free IPO data lookup: reads public pages on ipoji.com. No API key needed.

Selectors are based on the page layout seen in October 2026. If the site
changes its layout, parsing may return gaps; the app lists what is missing.
Respect the site: this makes 1-3 requests per lookup. Data is for personal use.
"""
import difflib
import re

import requests
from bs4 import BeautifulSoup

BASE = "https://www.ipoji.com"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}


def slugify(name):
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s if s.endswith("-ipo") else s + "-ipo"


def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=20)
    return r.text if r.status_code == 200 else None


def find_slug(name):
    """Try the obvious slug first, then fuzzy-match names on the IPO list pages."""
    slug = slugify(name)
    if fetch(f"{BASE}/ipo/{slug}"):
        return slug
    best, best_score = None, 0.0
    for page in ("ipo/current-ipo", "ipo/upcoming-ipo", "ipo/listed-ipo"):
        html = fetch(f"{BASE}/{page}")
        if not html:
            continue
        for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
            m = re.search(r"/ipo/([a-z0-9-]+-ipo)$", a["href"])
            if m:
                score = difflib.SequenceMatcher(None, slug, m.group(1)).ratio()
                if score > best_score:
                    best, best_score = m.group(1), score
    return best if best_score >= 0.6 else None


def to_num(s):
    try:
        return float(str(s).replace(",", "").replace("₹", "").replace("%", "").strip())
    except ValueError:
        return None


def first(pattern, text, group=1):
    m = re.search(pattern, text, re.I | re.S)
    return to_num(m.group(group)) if m else None


def tables(soup):
    out = []
    for t in soup.find_all("table"):
        out.append([[c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])] for tr in t.find_all("tr")])
    return out


def growth_from_financials(rows_list):
    """Return (revenue_growth, pat_growth, latest_pat) from the annual columns."""
    for rows in rows_list:
        if not rows or not any(r and r[0].lower().startswith("profit after tax") for r in rows):
            continue
        header = rows[0]
        cols = [(int(m.group(1)), i) for i, h in enumerate(header)
                if (m := re.search(r"31-Mar-(\d\d)", h))]
        cols.sort(reverse=True)  # latest financial year first
        if len(cols) < 2:
            continue
        (_, a), (_, b) = cols[0], cols[1]

        def row(label):
            for r in rows:
                if r and r[0].lower().startswith(label):
                    return to_num(r[a]), to_num(r[b])
            return None, None

        rev, prev_rev = row("revenue")
        pat, prev_pat = row("profit after tax")
        g = lambda x, y: round((x / y - 1) * 100, 1) if x is not None and y and y > 0 else None
        return g(rev, prev_rev), g(pat, prev_pat), pat
    return None, None, None


def peer_pe(rows_list):
    """Average P/E of peers (first data row is the IPO company itself, so skip it)."""
    for rows in rows_list:
        if rows and any("P/E" in h for h in rows[0]):
            idx = next(i for i, h in enumerate(rows[0]) if "P/E" in h)
            vals = [to_num(r[idx]) for r in rows[2:] if len(r) > idx]
            vals = [v for v in vals if v and v > 0]
            return round(sum(vals) / len(vals), 1) if vals else None
    return None


def parse_ipo_page(html):
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)
    tbls = tables(soup)

    m = re.search(r"Price band[^\d]*(\d[\d,]*)\s*[-–]\s*₹?\s*(\d[\d,]*)", text, re.I)
    price = to_num(m.group(2)) if m else first(r"Issue price[^\d]*(\d[\d,]*)", text)

    eps_pre = first(r"EPS Pre IPO[^₹]*₹\s*(-?[\d.]+)", text)
    market_cap = first(r"Market Cap[^₹]*₹\s*([\d,.]+)", text)
    rev_g, pat_g, pat = growth_from_financials(tbls)

    ipo_pe = None
    if price and eps_pre and eps_pre > 0:
        ipo_pe = round(price / eps_pre, 1)
    elif market_cap and pat and pat > 0:
        ipo_pe = round(market_cap / pat, 1)

    h1 = soup.find("h1")
    dates = re.search(r"IPO Dates\s*\n\s*([^\n]+)", text)
    return {
        "company": h1.get_text(strip=True).replace(" IPO", "") if h1 else None,
        "dates": dates.group(1) if dates else None,
        "price": price,
        "lot_size": first(r"Lot size\s*\n\s*(\d+)", text),
        "gmp": first(r"GMP:\s*(?:₹|Rs\.?)\s*(-?\d+(?:\.\d+)?)", text),
        "ipo_pe": ipo_pe,
        "peer_pe": peer_pe(tbls),
        "rev_growth": rev_g,
        "pat_growth": pat_g,
        "roe": first(r"\bROE\b[^%\d]*?(\d+\.?\d*)%", text) or first(r"\bRoNW\b[^%\d]*?(\d+\.?\d*)%", text),
        "debt_equity": first(r"Debt\s*/\s*Equity[^\d]*?(\d+\.\d+)", text),
    }


def parse_subscription(html):
    text = BeautifulSoup(html, "html.parser").get_text("\n", strip=True)
    def times(label):
        v = first(rf"(?:{label})[^\d\n]*\n?[^\d\n]*?(\d+(?:\.\d+)?)\s*x", text)
        return v if v is not None else 0.0
    return {"sub_qib": times("QIB"), "sub_nii": times("NII|HNI"), "sub_retail": times("Retail")}


def lookup_ipo(name):
    slug = find_slug(name)
    if not slug:
        raise ValueError(f"Could not find '{name}' on ipoji.com. Check the spelling or enter numbers by hand.")
    url = f"{BASE}/ipo/{slug}"
    data = parse_ipo_page(fetch(url))
    sub_url = f"{BASE}/ipo-subscription/{slug}"
    sub_html = fetch(sub_url)
    data.update(parse_subscription(sub_html) if sub_html else {"sub_qib": 0.0, "sub_nii": 0.0, "sub_retail": 0.0})
    data["sources"] = [url, sub_url]
    return data
