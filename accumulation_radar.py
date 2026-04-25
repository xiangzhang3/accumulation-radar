#!/usr/bin/env python3
import os, sys, time, sqlite3, requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

FEISHU_WEBHOOK = os.getenv("FEISHU_WEBHOOK", "")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")
FAPI = "https://fapi.binance.com"
DB_PATH = Path(__file__).parent / "accumulation.db"

MIN_SIDEWAYS_DAYS = 45
MAX_RANGE_PCT = 80
MAX_AVG_VOL_USD = 20_000_000
MIN_DATA_DAYS = 50


def api_get(endpoint, params=None):
    url = f"{FAPI}{endpoint}"
    last_err = None
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=15)
            if r.status_code == 200:
                return r.json()
            elif r.status_code == 451:
                last_err = f"地区限制 451 (GitHub IP 被币安屏蔽)"
                break
            elif r.status_code == 429:
                time.sleep(2)
                last_err = "429 限流"
            else:
                last_err = f"HTTP {r.status_code}: {r.text[:100]}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:100]}"
            time.sleep(1)
    if last_err:
        print(f"  ⚠️ API失败 {endpoint}: {last_err}")
    return None


def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS watchlist (
        symbol TEXT PRIMARY KEY, coin TEXT, added_date TEXT,
        sideways_days INT, range_pct REAL, avg_vol REAL,
        low_price REAL, high_price REAL, current_price REAL,
        score REAL, status TEXT DEFAULT 'watching'
    )""")
    conn.commit()
    return conn


def get_symbols():
    info = api_get("/fapi/v1/exchangeInfo")
    if not info:
        return []
    return [s["symbol"] for s in info["symbols"]
            if s["quoteAsset"] == "USDT"
            and s["contractType"] == "PERPETUAL"
            and s["status"] == "TRADING"]


def analyze(symbol, klines):
    if len(klines) < MIN_DATA_DAYS:
        return None
    data = [{"close": float(k[4]), "high": float(k[2]),
             "low": float(k[3]), "vol": float(k[7])} for k in klines]
    coin = symbol.replace("USDT", "")
    if coin in {"USDC", "USDP", "TUSD", "FDUSD", "BTCDOM", "DEFI", "USDM"}:
        return None
    recent = data[-7:]
    prior = data[:-7]
    if not prior:
        return None
    best_days, best_range, best_low, best_high, best_vol = 0, 0, 0, 0, 0
    for w in range(MIN_SIDEWAYS_DAYS, len(prior) + 1):
        wd = prior[-w:]
        lo = min(d["low"] for d in wd)
        hi = max(d["high"] for d in wd)
        if lo <= 0:
            continue
        rng = (hi - lo) / lo * 100
        if rng <= MAX_RANGE_PCT:
            avg_v = sum(d["vol"] for d in wd) / len(wd)
            if avg_v <= MAX_AVG_VOL_USD and w > best_days:
                best_days = w
                best_range = rng
                best_low = lo
                best_high = hi
                best_vol = avg_v
    if best_days < MIN_SIDEWAYS_DAYS:
        return None
    rec_vol = sum(d["vol"] for d in recent) / len(recent)
    breakout = rec_vol / best_vol if best_vol > 0 else 0
    score = (min(best_days / 90, 1) * 30
             + max(0, 1 - best_range / MAX_RANGE_PCT) * 25
             + max(0, 1 - best_vol / MAX_AVG_VOL_USD) * 25
             + min(breakout / 3, 1) * 20)
    if breakout >= 3:
        status = "🔥放量启动"
    elif breakout >= 1.5:
        status = "⚡开始放量"
    else:
        status = "💤收筹中"
    return {"symbol": symbol, "coin": coin, "sideways_days": best_days,
            "range_pct": best_range, "avg_vol": best_vol,
            "low_price": best_low, "high_price": best_high,
            "current_price": data[-1]["close"], "vol_breakout": breakout,
            "score": score, "status": status}


def fmt_usd(v):
    if v >= 1e9:
        return f"${v/1e9:.1f}B"
    if v >= 1e6:
        return f"${v/1e6:.1f}M"
    if v >= 1e3:
        return f"${v/1e3:.0f}K"
    return f"${v:.0f}"


def scan_pool():
    print("📊 扫描全市场...")
    syms = get_symbols()
    print(f"  共 {len(syms)} 个合约")
    if not syms:
        return None
    results = []
    for i, s in enumerate(syms):
        kl = api_get("/fapi/v1/klines", {"symbol": s, "interval": "1d", "limit": 180})
        if kl and isinstance(kl, list):
            r = analyze(s, kl)
            if r:
                results.append(r)
        if (i + 1) % 10 == 0:
            time.sleep(0.5)
        if (i + 1) % 100 == 0:
            print(f"  进度 {i+1}/{len(syms)}, 发现{len(results)}")
    results.sort(key=lambda x: x["score"], reverse=True)
    print(f"  ✅ 发现 {len(results)} 个标的")
    return results


def build_report(results):
    if not results:
        return ""
    now = datetime.now(timezone(timedelta(hours=8)))
    lines = [
        f"🏦 **庄家收筹雷达** 标的池更新",
        f"⏰ {now.strftime('%Y-%m-%d %H:%M')} CST",
        f"━━━━━━━━━━━━━━━━━━",
        f"扫描发现 {len(results)} 个标的",
        "",
    ]
    fire = [r for r in results if "放量启动" in r["status"]]
    warm = [r for r in results if "开始放量" in r["status"]]
    sleep_ = [r for r in results if "收筹中" in r["status"]]
    if fire:
        lines.append(f"🔥 **放量启动** ({len(fire)})")
        for r in fire[:10]:
            lines.append(f"  🔥 **{r['coin']}** 分:{r['score']:.0f} 横盘{r['sideways_days']}天 波动{r['range_pct']:.0f}% Vol{r['vol_breakout']:.1f}x")
        lines.append("")
    if warm:
        lines.append(f"⚡ **开始放量** ({len(warm)})")
        for r in warm[:10]:
            lines.append(f"  ⚡ {r['coin']} 分:{r['score']:.0f} 横盘{r['sideways_days']}天 Vol{r['vol_breakout']:.1f}x")
        lines.append("")
    if sleep_:
        lines.append(f"💤 **收筹中** ({len(sleep_)})")
        for r in sleep_[:15]:
            lines.append(f"  💤 {r['coin']} 分:{r['score']:.0f} 横盘{r['sideways_days']}天 日均{fmt_usd(r['avg_vol'])}")
    return "\n".join(lines)


def send_feishu(text):
    if not FEISHU_WEBHOOK:
        return False
    chunks = []
    cur = ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3500:
            chunks.append(cur)
            cur = line
        else:
            cur += "\n" + line if cur else line
    if cur:
        chunks.append(cur)
    title = text.split("\n")[0].replace("**", "").strip()[:60]
    if "失败" in title or "诊断" in title:
        color = "orange"
    elif "收筹" in title or "标的池" in title:
        color = "blue"
    else:
        color = "red"
    ok = True
    for i, ch in enumerate(chunks):
        t = title if len(chunks) == 1 else f"{title} ({i+1}/{len(chunks)})"
        payload = {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {"template": color, "title": {"tag": "plain_text", "content": t}},
                "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": ch}}]
            }
        }
        try:
            r = requests.post(FEISHU_WEBHOOK, json=payload, timeout=10)
            d = r.json() if r.text else {}
            if r.status_code == 200 and d.get("code", 0) == 0:
                print(f"[飞书] Sent ✓ ({len(ch)} chars)")
            else:
                r2 = requests.post(FEISHU_WEBHOOK, json={
                    "msg_type": "text",
                    "content": {"text": ch.replace("**", "")}
                }, timeout=10)
                d2 = r2.json() if r2.text else {}
                if d2.get("code", 0) == 0:
                    print("[飞书] Sent plain ✓")
                else:
                    print(f"[飞书] Failed: {d}")
                    ok = False
        except Exception as e:
            print(f"[飞书] Error: {e}")
            ok = False
        time.sleep(0.5)
    return ok


def send_telegram(text):
    if not TG_BOT_TOKEN:
        return False
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TG_CHAT_ID, "text": text[:3800],
            "parse_mode": "Markdown"
        }, timeout=10)
        if r.status_code == 200:
            print("[TG] Sent ✓")
            return True
        r2 = requests.post(url, json={
            "chat_id": TG_CHAT_ID,
            "text": text.replace("*", "").replace("_", "")[:3800]
        }, timeout=10)
        return r2.status_code == 200
    except Exception as e:
        print(f"[TG] Error: {e}")
        return False


def send(text):
    if not text:
        return
    sent = False
    if FEISHU_WEBHOOK:
        sent = send_feishu(text)
    if not sent and TG_BOT_TOKEN:
        sent = send_telegram(text)
    if not sent:
        print("\n[NO PUSH]\n" + text)


def save(conn, results):
    c = conn.cursor()
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    for r in results:
        c.execute("""INSERT OR REPLACE INTO watchlist
            (symbol, coin, added_date, sideways_days, range_pct, avg_vol,
             low_price, high_price, current_price, score, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (r["symbol"], r["coin"], now, r["sideways_days"], r["range_pct"],
             r["avg_vol"], r["low_price"], r["high_price"],
             r["current_price"], r["score"], r["status"]))
    conn.commit()
    print(f"  💾 保存 {len(results)} 个")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "full"
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"🏦 雷达 {now_str} 模式={mode}")
    print(f"   推送: 飞书={'✓' if FEISHU_WEBHOOK else '✗'} TG={'✓' if TG_BOT_TOKEN else '✗'}")

    # 启动测试：先发一条诊断消息验证推送通道
    diag = (f"🔧 **雷达启动诊断**\n"
            f"⏰ {now_str}\n"
            f"━━━━━━━━━━━━━\n"
            f"模式: {mode}\n"
            f"推送通道: 飞书={'✓' if FEISHU_WEBHOOK else '✗'} TG={'✓' if TG_BOT_TOKEN else '✗'}\n"
            f"开始测试币安 API 连通性...")
    send(diag)

    conn = init_db()
    results = scan_pool()

    if results is None:
        # API 不可达
        err = (f"⚠️ **币安 API 不可达**\n"
               f"⏰ {now_str}\n"
               f"━━━━━━━━━━━━━\n"
               f"`/fapi/v1/exchangeInfo` 返回空。\n"
               f"原因：GitHub Actions 服务器 IP 被币安屏蔽（HTTP 451）。\n\n"
               f"解决方案：\n"
               f"1. 使用代理（自建 VPS 中转）\n"
               f"2. 改用 OKX/Bybit 等不限制 GitHub IP 的交易所\n"
               f"3. 把脚本部署到自己的服务器")
        send(err)
        conn.close()
        return

    if results:
        save(conn, results)
        report = build_report(results)
        send(report)
    else:
        send(f"📭 **本次扫描未发现收筹标的**\n⏰ {now_str}\n（市场无符合条件的横盘币）")

    conn.close()
    print("✅ 完成")


if __name__ == "__main__":
    main()
