"""IPO Buy-or-Not Analyzer: Flask backend.

Run:  pip install -r requirements.txt && python app.py
Open: http://127.0.0.1:5000

IMPORTANT: this is a rules-based scoring model, not a prediction engine.
No tool can guarantee IPO outcomes. GMP and subscription numbers are typed in
by you because they change hourly and have no reliable official API.
"""
import math
import os
from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__, static_folder="static")

RETAIL_LIMIT = 200000  # max retail bid in rupees (SEBI rule)

# Weights sum to 100. Edit these to match your own strategy.
WEIGHTS = {
    "valuation": 20,
    "growth": 15,
    "roe": 10,
    "debt": 10,
    "subscription": 20,
    "gmp": 15,
    "sentiment": 10,
}


def clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


def num(data, key, default=0.0):
    try:
        return float(data.get(key, default))
    except (TypeError, ValueError):
        raise ValueError(f"'{key}' must be a number")


def analyze(d):
    price = num(d, "price")
    lot = num(d, "lot_size")
    if price <= 0 or lot <= 0:
        raise ValueError("Price and lot size must be greater than 0")

    ipo_pe, peer_pe = num(d, "ipo_pe"), num(d, "peer_pe")
    pat_growth, rev_growth = num(d, "pat_growth"), num(d, "rev_growth")
    roe, de = num(d, "roe"), num(d, "debt_equity")
    gmp = num(d, "gmp")
    qib, nii, retail = num(d, "sub_qib"), num(d, "sub_nii"), num(d, "sub_retail")
    sentiment = clamp(num(d, "sentiment", 3), 1, 5)

    # Each factor is scored 0-100.
    ratio = ipo_pe / peer_pe if peer_pe > 0 else 1.0
    scores = {
        "valuation": clamp((1.5 - ratio) / 0.8 * 100),  # <=0.7x peers = 100
        "growth": clamp(0.6 * clamp(pat_growth, 0, 50) * 2 + 0.4 * clamp(rev_growth, 0, 25) * 4),
        "roe": clamp(roe / 25 * 100),
        "debt": clamp((2 - de) / 2 * 100),
        "gmp": clamp(gmp / price * 100 / 30 * 100),  # 30% GMP = 100
        "sentiment": (sentiment - 1) / 4 * 100,
    }
    weighted_sub = 0.4 * qib + 0.3 * nii + 0.3 * retail
    scores["subscription"] = clamp(math.log10(weighted_sub + 1) / math.log10(51) * 100)  # 50x = 100

    total = sum(scores[k] * WEIGHTS[k] for k in WEIGHTS) / 100

    if total >= 70:
        verdict, tone = "APPLY", "good"
    elif total >= 50:
        verdict, tone = "APPLY ONLY FOR LISTING GAIN (RISKY)", "mid"
    else:
        verdict, tone = "AVOID", "bad"

    # Expected listing gain: haircut the GMP when subscription is weak.
    gmp_pct = gmp / price * 100
    haircut = 0.5 + 0.5 * scores["subscription"] / 100
    base_pct = gmp_pct * haircut
    bull_pct = gmp_pct * 1.3
    bear_pct = -(15 - total * 0.1)  # strong IPOs fall less in bad cases

    per_lot = lot * price
    max_lots = max(1, int(RETAIL_LIMIT // per_lot))
    # Retail lottery: if oversubscribed N times, roughly 1/N chance of one lot.
    p_allot = 1.0 if retail <= 1 else min(1.0, 1 / retail)

    def scenario(pct):
        gain_lot = per_lot * pct / 100
        return {"pct": round(pct, 1), "per_lot": round(gain_lot), "max_lots": round(gain_lot * max_lots)}

    base = scenario(base_pct)
    return {
        "score": round(total, 1),
        "verdict": verdict,
        "tone": tone,
        "factors": {k: round(v) for k, v in scores.items()},
        "weights": WEIGHTS,
        "per_lot_cost": round(per_lot),
        "max_lots": max_lots,
        "allotment_chance_pct": round(p_allot * 100, 1),
        "scenarios": {"bear": scenario(bear_pct), "base": base, "bull": scenario(bull_pct)},
        "expected_profit": round(p_allot * base["per_lot"]),
        "notes": [
            "GMP is unofficial and can vanish before listing day.",
            "Allotment chance is a rough estimate (retail lottery), not a promise.",
            "Educational tool, not financial advice.",
        ],
    }


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.post("/api/analyze")
def api_analyze():
    try:
        return jsonify(analyze(request.get_json(force=True) or {}))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


FIELDS = ["price", "lot_size", "ipo_pe", "peer_pe", "pat_growth", "rev_growth",
          "roe", "debt_equity", "gmp", "sentiment", "sub_qib", "sub_nii", "sub_retail"]

def lookup(name):
    from scraper import lookup_ipo
    data = lookup_ipo(name)
    data["sentiment"] = 3  # no free source for this: set it yourself (1-5)
    data["missing"] = [k for k in FIELDS if data.get(k) is None]
    data["notes"] = ("GMP is unofficial. Market mood is set to 3: change it yourself. "
                     "Subscription shows 0 until bidding data is published.")
    return data


@app.post("/api/lookup")
def api_lookup():
    name = ((request.get_json(force=True) or {}).get("name") or "").strip()
    if not name:
        return jsonify({"error": "Enter an IPO name"}), 400
    try:
        return jsonify(lookup(name))
    except Exception as e:  # network, key, parse errors
        return jsonify({"error": str(e)}), 502


if __name__ == "__main__":
    app.run(debug=True)
