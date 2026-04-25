#!/usr/bin/env python3
"""
庄家收筹雷达 v1 — 飞书 Webhook 版
"""

import json
import os
import sys
import time
import requests
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

env_file = Path(__file__).parent / ".env.oi"
if env_file.exists():
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")
FEISHU_WEBHOOK = os.getenv("FEISHU_WEBHOOK", "")
FAPI = "https://fapi.binance.com"
DB_PATH = Path(__file__).parent / "accumulation.db"

MIN_SIDEWAYS_DAYS = 45
MAX_RANGE_PCT = 80
MAX_AVG_VOL_USD = 20_000_000
MIN_DATA_DAYS = 50
MIN_OI_DELTA_PCT = 3.0
MIN_OI_USD = 2_000_000
VOL_BREAKOUT_MULT = 3.0


def api_get(endpoint, params=None):
    url = f"{FAPI}{endpoint}"
    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 429:
                time.sleep(2)
            else:
                return None
        except:
            time.sleep(1)
    return None


def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS watchlist (
        symbol TEXT PRIMARY KEY,
        coin TEXT,
        added_date TEXT,
        sideways_days INT,
        range_pct REAL,
        avg_vol REAL,
        low_price REAL,
        high_price REAL,
        current_price REAL,
        score REAL,
        status TEXT DEFAULT 'watching',
        last_oi_alert TEXT,
        notes TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT,
        alert_type TEXT,
        alert_time TEXT,
        price REAL,
        oi_delta_pct REAL,
        vol_ratio REAL,
        details TEXT
    )""")
    conn.commit()
    return conn


def get_all_perp_symbols():
    info = api_get("/fapi/v1/exchangeInfo")
    if not info:
        return []
    return [s["symbol"] for s in info["symbols"]
            if s["quoteAsset"] == "USDT" 
            and s["contractType"] == "PERPETUAL"
            and s["status"] == "TRADING"]


def analyze_accumulation(symbol, klines):
    if len(klines) < MIN_DATA_DAYS:
        return None
    
    data = []
    for k in klines:
        data.append({
            "ts": k[0],
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "vol": float(k[7]),
        })
    
    coin = symbol.replace("USDT", "")
    EXCLUDE = {"USDC", "USDP", "TUSD", "FDUSD", "BTCDOM", "DEFI", "USDM"}
    if coin in EXCLUDE:
        return None
    
    recent_7d = data[-7:]
    prior = data[:-7]
    if not prior:
        return None
    
    recent_avg_px = sum(d["close"] for d in recent_7d) / len(recent_7d)
    prior_avg_px = sum(d["close"] for d in prior) / len(prior)
    
    if prior_avg_px > 0 and ((recent_avg_px - prior_avg_px) / prior_avg_px) > 3.0:
        return None
    
    best_sideways = 0
    best_range = 0
    best_low = 0
    best_high = 0
    best_avg_vol = 0
    best_slope_pct = 0
    
    for window in range(MIN_SIDEWAYS_DAYS, len(prior) + 1):
        window_data = prior[-window:]
        lows = [d["low"] for d in window_data]
        highs = [d["high"] for d in window_data]
        w_low = min(lows)
        w_high = max(highs)
        if w_low <= 0:
            continue
        range_pct = ((w_high - w_low) / w_low) * 100
        if range_pct <= MAX_RANGE_PCT:
            avg_vol = sum(d["vol"] for d in window_data) / len(window_data)
            if avg_vol <= MAX_AVG_VOL_USD:
                closes = [d["close"] for d in window_data]
                n = len(closes)
                x_mean = (n - 1) / 2.0
                y_mean = sum(closes) / n
                num = sum((i - x_mean) * (c - y_mean) for i, c in enumerate(closes))
                den = sum((i - x_mean) ** 2 for i in range(n))
                slope = num / den if den > 0 else 0
                slope_pct = (slope * n / closes[0] * 100) if closes[0] > 0 else 0
                if abs(slope_pct) > 20:
                    continue
                if window > best_sideways:
                    best_sideways = window
                    best_range = range_pct
                    best_low = w_low
                    best_high = w_high
                    best_avg_vol = avg_vol
                    best_slope_pct = slope_pct
    
    if best_sideways < MIN_SIDEWAYS_DAYS:
        return None
    
    days_score = min(best_sideways / 90, 1.0) * 25
    range_score = max(0, (1 - best_range / MAX_RANGE_PCT)) * 20
    vol_score = max(0, (1 - best_avg_vol / MAX_AVG_VOL_USD)) * 20
    
    recent_vol = sum(d["vol"] for d in recent_7d) / len(recent_7d)
    vol_breakout = recent_vol / best_avg_vol if best_avg_vol > 0 else 0
    breakout_score = min(vol_breakout / VOL_BREAKOUT_MULT, 1.0) * 15
    
    est_mcap = data[-1]["close"] * best_avg_vol * 30
    if est_mcap > 0 and est_mcap < 50_000_000:
        mcap_score = 20
    elif est_mcap < 100_000_000:
        mcap_score = 15
    elif est_mcap < 200_000_000:
        mcap_score = 10
    elif est_mcap < 500_000_000:
        mcap_score = 5
    else:
        mcap_score = 0
    
    total_score = days_score + range_score + vol_score + breakout_score + mcap_score
    flatness_bonus = max(0, (1 - abs(best_slope_pct) / 20)) * 5
    total_score += flatness_bonus
    
    if vol_breakout >= VOL_BREAKOUT_MULT:
        status = "🔥放量启动"
    elif vol_breakout >= 1.5:
        status = "⚡开始放量"
    else:
        status = "💤收筹中"
    
    return {
        "symbol": symbol, "coin": coin,
        "sideways_days": best_sideways, "range_pct": best_range,
        "slope_pct": best_slope_pct, "low_price": best_low,
        "high_price": best_high, "avg_vol": best_avg_vol,
        "current_price": data[-1]["close"], "recent_vol": recent_vol,
        "vol_breakout": vol_breakout, "score": total_score,
        "status": status, "data_days": len(data),
    }


def scan_accumulation_pool():
    print("📊 扫描全市场收筹标的...")
    symbols = get_all_perp_symbols()
    print(f"  共 {len(symbols)} 个合约")
    results = []
    for i, sym in enumerate(symbols):
        klines = api_get("/fapi/v1/klines", {"symbol": sym, "interval": "1d", "limit": 180})
        if klines and isinstance(klines, list):
            r = analyze_accumulation(sym, klines)
            if r:
                results.append(r)
        if (i + 1) % 10 == 0:
            time.sleep(0.5)
        if (i + 1) % 100 == 0:
            print(f"  进度: {i+1}/{len(symbols)}... 已发现{len(results)}个")
    results.sort(key=lambda x: x["score"], reverse=True)
    print(f"  ✅ 发现 {len(results)} 个收筹标的")
    return results


def format_usd(v):
    if v >= 1e9: return f"${v/1e9:.1f}B"
    if v >= 1e6: return f"${v/1e6:.1f}M"
    if v >= 1e3: return f"${v/1e3:.0f}K"
    return f"${v:.0f}"


def build_pool_report(results):
    if not results:
        return ""
    now = datetime.now(timezone(timedelta(hours=8)))
    lines = [
        f"🏦 **庄家收筹雷达** — 标的池更新",
        f"⏰ {now.strftime('%Y-%m-%d %H:%M')} CST",
        f"━━━━━━━━━━━━━━━━━━",
        f"扫描 {len(results)} 个合约，发现标的：",
        "",
    ]
    firing = [r for r in results if "放量启动" in r["status"]]
    warming = [r for r in results if "开始放量" in r["status"]]
    sleeping = [r for r in results if "收筹中" in r["status"]]
    if firing:
        lines.append(f"🔥 **放量启动** ({len(firing)}个)")
        for r in firing[:10]:
            lines.append(f"  🔥 **{r['coin']}** | 分:{r['score']:.0f} | 横盘{r['sideways_days']}天 | 波动{r['range_pct']:.0f}% | Vol放大{r['vol_breakout']:.1f}x")
            lines.append(f"     ${r['current_price']:.6f} | 区间: ${r['low_price']:.6f}~${r['high_price']:.6f} | 日均Vol: {format_usd(r['avg_vol'])}")
        lines.append("")
    if warming:
        lines.append(f"⚡ **开始放量** ({len(warming)}个)")
        for r in warming[:10]:
            lines.append(f"  ⚡ {r['coin']} | 分:{r['score']:.0f} | 横盘{r['sideways_days']}天 | 波动{r['range_pct']:.0f}% | Vol{r['vol_breakout']:.1f}x")
        lines.append("")
    if sleeping:
        lines.append(f"💤 **收筹中** ({len(sleeping)}个)")
        for r in sleeping[:15]:
            lines.append(f"  💤 {r['coin']} | 分:{r['score']:.0f} | 横盘{r['sideways_days']}天 | 波动{r['range_pct']:.0f}% | 日均Vol {format_usd(r['avg_vol'])}")
    return "\n".join(lines)


def send_feishu(text):
    if not FEISHU_WEBHOOK:
        return False
    chunks = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > 3500:
            chunks.append(current)
            current = line
        else:
            current += "\n" + line if current else line
    if current:
        chunks.append(current)
    first_line = text.split("\n")[0].strip()
    title = first_line.replace("**", "").strip()
    if len(title) > 60:
        title = title[:60]
    if "🔥" in title or "庄家雷达" in title:
        template_color = "red"
    elif "OI" in title:
        template_color = "orange"
    elif "标的池" in title or "收筹" in title:
        template_color = "blue"
    else:
        template_color = "indigo"
    success = True
    for idx, chunk in enumerate(chunks):
        chunk_title = title if len(chunks) == 1 else f"{title} ({idx+1}/{len(chunks)})"
        body = chunk.replace("_", "")
        payload = {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "template": template_color,
                    "title": {"tag": "plain_text", "content": chunk_title}
                },
                "elements": [
                    {"tag": "div", "text": {"tag": "lark_md", "content": body}}
                ]
            }
        }
        try:
            resp = requests.post(FEISHU_WEBHOOK, json=payload, timeout=10)
            data = resp.json() if resp.text else {}
            if resp.status_code == 200 and data.get("code", 0) == 0:
                print(f"[飞书] Sent ✓ ({len(chunk)} chars)")
            else:
                plain = chunk.replace("**", "").replace("*", "")
                resp2 = requests.post(FEISHU_WEBHOOK, json={
                    "msg_type": "text", "content": {"text": plain}
                }, timeout=10)
                d2 = resp2.json() if resp2.text else {}
                if resp2.status_code == 200 and d2.get("code", 0) == 0:
                    print(f"[飞书] Sent plain ✓")
                else:
                    print(f"[飞书] Failed: code={data.get('code')} msg={data.get('msg')}")
                    success = False
        except Exception as e:
            print(f"[飞书] Error: {e}")
            success = False
        time.sleep(0.5)
    return success


def send_telegram(text):
    if not TG_BOT_TOKEN:
        return False
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    chunks = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > 3800:
            chunks.append(current)
            current = line
        else:
            current += "\n" + line if current else line
    if current:
        chunks.append(current)
    sent_any = False
    for chunk in chunks:
        try:
            resp = requests.post(url, json={
                "chat_id": TG_CHAT_ID, "text": chunk, "parse_mode": "Markdown"
            }, timeout=10)
            if resp.status_code == 200:
                sent_any = True
            else:
                resp2 = requests.post(url, json={
                    "chat_id": TG_CHAT_ID, "text": chunk.replace("*", "").replace("_", ""),
                }, timeout=10)
                if resp2.status_code == 200:
                    sent_any = True
        except Exception as e:
            print(f"[TG] Error: {e}")
        time.sleep(0.5)
    return sent_any


def send_message(text):
    if not text:
        return
    sent = False
    if FEISHU_WEBHOOK:
        sent = send_feishu(text)
    if not sent and TG_BOT_TOKEN:
        sent = send_telegram(text)
    if not sent:
        print("\n[NO PUSH] stdout:\n")
        print(text)


def save_watchlist(conn, results):
    c = conn.cursor()
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    for r in results:
        c.execute("""INSERT OR REPLACE INTO watchlist 
            (symbol, coin, added_date, sideways_days, range_pct, avg_vol, 
             low_price, high_price, current_price, score, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (r["symbol"], r["coin"], now, r["sideways_days"], r["range_pct"],
             r["avg_vol"], r["low_price"], r["high_price"], r["current_price"],
             r["score"], r["status"]))
    conn.commit()
    print(f"  💾 保存 {len(results)} 个标的")


def load_watchlist_symbols(conn):
    c = conn.cursor()
    c.execute("SELECT symbol FROM watchlist WHERE status != 'removed'")
    return [row[0] for row in c.fetchall()]
    def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "full"
    
    print(f"🏦 庄家收筹雷达 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"   模式: {mode}")
    print(f"   推送: 飞书={'✓' if FEISHU_WEBHOOK else '✗'} TG={'✓' if TG_BOT_TOKEN else '✗'}\n")
    
    conn = init_db()
    
    if mode in ("full", "pool"):
        results = scan_accumulation_pool()
        if results:
            save_watchlist(conn, results)
            report = build_pool_report(results)
            if report:
                send_message(report)
    
    if mode in ("full", "oi"):
        watchlist = load_watchlist_symbols(conn)
        if not watchlist:
            print("⚠️ 标的池为空，先运行 pool 模式")
            conn.close()
            return
        
        tickers_raw = api_get("/fapi/v1/ticker/24hr")
        premiums_raw = api_get("/fapi/v1/premiumIndex")
        if not tickers_raw or not premiums_raw:
            print("❌ API失败")
            conn.close()
            return
        
        ticker_map = {}
        for t in tickers_raw:
            if t["symbol"].endswith("USDT"):
                ticker_map[t["symbol"]] = {
                    "px_chg": float(t["priceChangePercent"]),
                    "vol": float(t["quoteVolume"]),
                    "price": float(t["lastPrice"]),
                }
        
        funding_map = {}
        for p in premiums_raw:
            if p["symbol"].endswith("USDT"):
                funding_map[p["symbol"]] = float(p["lastFundingRate"])
        
        mcap_map = {}
        try:
            _r = requests.get("https://www.binance.com/bapi/composite/v1/public/marketing/symbol/list", timeout=10)
            if _r.status_code == 200:
                for item in _r.json().get("data", []):
                    name = item.get("name", "")
                    mc = item.get("marketCap", 0)
                    if name and mc:
                        mcap_map[name] = float(mc)
                print(f"✅ 拉到 {len(mcap_map)} 个币市值")
        except Exception as e:
            print(f"⚠️ 市值API失败: {e}")
        
        heat_map = {}
        cg_trending = set()
        try:
            _r = requests.get("https://api.coingecko.com/api/v3/search/trending", timeout=10)
            if _r.status_code == 200:
                for item in _r.json().get("coins", []):
                    sym = item["item"]["symbol"].upper()
                    rank = item["item"].get("score", 99)
                    cg_trending.add(sym)
                    heat_map[sym] = heat_map.get(sym, 0) + max(50 - rank * 3, 10)
                print(f"🔥 CG Trending: {len(cg_trending)}个")
        except Exception as e:
            print(f"⚠️ CG失败: {e}")
        
        vol_surge_coins = set()
        for sym, tk in ticker_map.items():
            coin = sym.replace("USDT", "")
            vol_24h = tk["vol"]
            if vol_24h > 20_000_000:
                kl = api_get("/fapi/v1/klines", {"symbol": sym, "interval": "1d", "limit": 6})
                if kl and len(kl) >= 5:
                    avg_5d = sum(float(k[7]) for k in kl[:-1]) / (len(kl)-1)
                    if avg_5d > 0:
                        ratio = vol_24h / avg_5d
                        if ratio >= 2.5:
                            vol_surge_coins.add(coin)
                            heat_map[coin] = heat_map.get(coin, 0) + min(ratio * 10, 50)
                    time.sleep(0.05)
        
        for coin in (cg_trending & vol_surge_coins):
            heat_map[coin] = heat_map.get(coin, 0) + 20
        
        c2 = conn.cursor()
        c2.execute("SELECT symbol, score, sideways_days, range_pct, avg_vol, status FROM watchlist")
        pool_map = {}
        for row in c2.fetchall():
            pool_map[row[0]] = {"pool_score": row[1], "sideways_days": row[2], "range_pct": row[3], "avg_vol": row[4], "status": row[5]}
        
        scan_syms = set()
        for sym, pd in pool_map.items():
            if "放量" in pd.get("status", "") or "开始" in pd.get("status", ""):
                scan_syms.add(sym)
        top_by_vol = sorted(ticker_map.items(), key=lambda x: x[1]["vol"], reverse=True)[:100]
        for sym, _ in top_by_vol:
            scan_syms.add(sym)
        
        oi_map = {}
        for i, sym in enumerate(scan_syms):
            oi_hist = api_get("/futures/data/openInterestHist", {"symbol": sym, "period": "1h", "limit": 6})
            if oi_hist and len(oi_hist) >= 2:
                curr = float(oi_hist[-1]["sumOpenInterestValue"])
                prev_1h = float(oi_hist[-2]["sumOpenInterestValue"])
                prev_6h = float(oi_hist[0]["sumOpenInterestValue"])
                d1h = ((curr - prev_1h) / prev_1h * 100) if prev_1h > 0 else 0
                d6h = ((curr - prev_6h) / prev_6h * 100) if prev_6h > 0 else 0
                circ_supply = float(oi_hist[-1].get("CMCCirculatingSupply", 0))
                oi_map[sym] = {"oi_usd": curr, "d1h": d1h, "d6h": d6h, "circ_supply": circ_supply}
            if (i+1) % 10 == 0:
                time.sleep(0.5)
        
        all_syms = set(list(pool_map.keys()) + list(oi_map.keys()))
        coin_data = {}
        for sym in all_syms:
            tk = ticker_map.get(sym, {})
            if not tk: continue
            pool = pool_map.get(sym, {})
            oi = oi_map.get(sym, {})
            fr = funding_map.get(sym, 0)
            coin = sym.replace("USDT", "")
            d6h = oi.get("d6h", 0)
            fr_pct = fr * 100
            oi_usd = oi.get("oi_usd", 0)
            if coin in mcap_map:
                est_mcap = mcap_map[coin]
            else:
                circ_supply = oi.get("circ_supply", 0)
                price = tk.get("price", 0) if isinstance(tk, dict) else 0
                if circ_supply > 0 and price > 0:
                    est_mcap = circ_supply * price
                else:
                    est_mcap = max(tk["vol"] * 0.3, oi_usd * 2) if oi_usd > 0 else tk["vol"] * 0.3
            sw_days = pool.get("sideways_days", 0) if pool else 0
            heat = heat_map.get(coin, 0)
            coin_data[sym] = {
                "coin": coin, "sym": sym,
                "px_chg": tk["px_chg"], "vol": tk["vol"],
                "fr_pct": fr_pct, "d6h": d6h,
                "oi_usd": oi_usd, "est_mcap": est_mcap,
                "sw_days": sw_days, "in_pool": bool(pool), "heat": heat,
                "in_cg": coin in cg_trending,
                "vol_surge": coin in vol_surge_coins,
            }
        
        chase = []
        for sym, d in coin_data.items():
            if d["px_chg"] > 3 and d["fr_pct"] < -0.005 and d["vol"] > 1_000_000:
                fr_hist = api_get("/fapi/v1/fundingRate", {"symbol": sym, "limit": 5})
                fr_rates = [float(f["fundingRate"]) * 100 for f in fr_hist] if fr_hist else [d["fr_pct"]]
                fr_prev = fr_rates[-2] if len(fr_rates) >= 2 else d["fr_pct"]
                fr_delta = d["fr_pct"] - fr_prev
                trend = "🔥加速" if fr_delta < -0.05 else "⬇️变负" if fr_delta < -0.01 else "➡️" if abs(fr_delta) < 0.01 else "⬆️回升"
                chase.append({**d, "fr_delta": fr_delta, "trend": trend})
                time.sleep(0.2)
        chase.sort(key=lambda x: x["fr_pct"])
        
        combined = []
        for sym, d in coin_data.items():
            fr = d["fr_pct"]
            if fr < -0.5: f_sc = 25
            elif fr < -0.1: f_sc = 22
            elif fr < -0.05: f_sc = 18
            elif fr < -0.03: f_sc = 14
            elif fr < -0.01: f_sc = 10
            elif fr < 0: f_sc = 5
            else: f_sc = 0
            mc = d["est_mcap"]
            if mc > 0 and mc < 50e6: m_sc = 25
            elif mc < 100e6: m_sc = 22
            elif mc < 200e6: m_sc = 20
            elif mc < 300e6: m_sc = 17
            elif mc < 500e6: m_sc = 12
            elif mc < 1e9: m_sc = 7
            else: m_sc = 0
            sw = d["sw_days"]
            if sw >= 120: s_sc = 25
            elif sw >= 90: s_sc = 22
            elif sw >= 75: s_sc = 18
            elif sw >= 60: s_sc = 14
            elif sw >= 45: s_sc = 10
            else: s_sc = 0
            abs6 = abs(d["d6h"])
            if abs6 >= 15: o_sc = 25
            elif abs6 >= 8: o_sc = 22
            elif abs6 >= 5: o_sc = 18
            elif abs6 >= 3: o_sc = 14
            elif abs6 >= 2: o_sc = 10
            else: o_sc = 0
            total = f_sc + m_sc + s_sc + o_sc
            if total < 25: continue
            combined.append({**d, "total": total, "f_sc": f_sc, "m_sc": m_sc, "s_sc": s_sc, "o_sc": o_sc})
        combined.sort(key=lambda x: x["total"], reverse=True)
        
        ambush = []
        for sym, d in coin_data.items():
            if not d["in_pool"]: continue
            if d["px_chg"] > 50: continue
            mc = d["est_mcap"]
            if mc > 0 and mc < 50e6: m_sc = 35
            elif mc < 100e6: m_sc = 32
            elif mc < 150e6: m_sc = 28
            elif mc < 200e6: m_sc = 25
            elif mc < 300e6: m_sc = 20
            elif mc < 500e6: m_sc = 12
            elif mc < 1e9: m_sc = 5
            else: m_sc = 0
            abs6 = abs(d["d6h"])
            if abs6 >= 10: o_sc = 30
            elif abs6 >= 5: o_sc = 25
            elif abs6 >= 3: o_sc = 20
            elif abs6 >= 2: o_sc = 14
            elif abs6 >= 1: o_sc = 8
            else: o_sc = 0
            if d["d6h"] > 2 and abs(d["px_chg"]) < 5:
                o_sc = min(o_sc + 5, 30)
            sw = d["sw_days"]
            if sw >= 120: s_sc = 20
            elif sw >= 90: s_sc = 17
            elif sw >= 75: s_sc = 14
            elif sw >= 60: s_sc = 10
            elif sw >= 45: s_sc = 6
            else: s_sc = 0
            fr = d["fr_pct"]
            if fr < -0.1: f_sc = 15
            elif fr < -0.05: f_sc = 12
            elif fr < -0.03: f_sc = 9
            elif fr < -0.01: f_sc = 6
            elif fr < 0: f_sc = 3
            else: f_sc = 0
            total = m_sc + o_sc + s_sc + f_sc
            if total < 20: continue
            ambush.append({**d, "total": total, "m_sc": m_sc, "o_sc": o_sc, "s_sc": s_sc, "f_sc": f_sc})
        ambush.sort(key=lambda x: x["total"], reverse=True)
        
        def mcap_str(v):
            if v >= 1e6: return f"${v/1e6:.0f}M"
            if v >= 1e3: return f"${v/1e3:.0f}K"
            return f"${v:.0f}"
        
        now = datetime.now(timezone(timedelta(hours=8)))
        lines = [
            f"🏦 **庄家雷达** 三策略+热度",
            f"⏰ {now.strftime('%Y-%m-%d %H:%M')} CST",
        ]
        
        hot_coins = sorted([d for d in coin_data.values() if d["heat"] > 0], key=lambda x: x["heat"], reverse=True)
        if hot_coins:
            lines.append(f"\n🔥 **热度榜**")
            for s in hot_coins[:8]:
                tags = []
                if s["in_cg"]: tags.append("🌐CG")
                if s["vol_surge"]: tags.append("📈放量")
                if abs(s["d6h"]) >= 3: tags.append(f"⚡OI{s['d6h']:+.0f}%")
                if s["in_pool"]: tags.append(f"💤{s['sw_days']}天")
                if s["fr_pct"] < -0.03: tags.append(f"🧊{s['fr_pct']:.2f}%")
                lines.append(f"  {s['coin']:<8} ~{mcap_str(s['est_mcap'])} 涨{s['px_chg']:+.0f}% | {' '.join(tags)}")
        
        lines.append(f"\n🔥 **追多** (按费率排名)")
        if chase:
            for s in chase[:8]:
                lines.append(f"  {s['coin']:<7} 费率{s['fr_pct']:+.3f}% {s['trend']} | 涨{s['px_chg']:+.0f}% | ~{mcap_str(s['est_mcap'])}")
        else:
            lines.append("  暂无")
        
        lines.append(f"\n📊 **综合**")
        for s in combined[:8]:
            dims = []
            if s["f_sc"] >= 10: dims.append(f"🧊{s['fr_pct']:.2f}%")
            if s["m_sc"] >= 12: dims.append(f"💎{mcap_str(s['est_mcap'])}")
            if s["s_sc"] >= 10: dims.append(f"💤{s['sw_days']}天")
            if s["o_sc"] >= 10: dims.append(f"⚡OI{s['d6h']:+.0f}%")
            lines.append(f"  {s['coin']:<7} {s['total']}分 | {' '.join(dims)}")
        
        lines.append(f"\n🎯 **埋伏**")
        for s in ambush[:8]:
            tags = [f"~{mcap_str(s['est_mcap'])}"]
            if abs(s["d6h"]) >= 2: tags.append(f"OI{s['d6h']:+.0f}%")
            if s["d6h"] > 2 and abs(s["px_chg"]) < 5: tags.append("🎯暗流")
            if s["sw_days"] >= 45: tags.append(f"横盘{s['sw_days']}天")
            if s["fr_pct"] < -0.01: tags.append(f"费率{s['fr_pct']:.2f}%")
            lines.append(f"  {s['coin']:<7} {s['total']}分 | {' '.join(tags)}")
        
        report = "\n".join(lines)
        send_message(report)
    
    conn.close()
    print("\n✅ 完成")


if __name__ == "__main__":
    main()
    if __name__ == "__main__":
    main()
