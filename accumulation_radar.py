#!/usr/bin/env python3
"""庄家收筹雷达 — OKX 数据源 + TG 优先推送"""
import os, sys, time, sqlite3, requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

FEISHU_WEBHOOK = os.getenv("FEISHU_WEBHOOK", "")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")
OKX = "https://www.okx.com"
DB_PATH = Path(__file__).parent / "accumulation.db"

MIN_SIDEWAYS_DAYS = 45
MAX_RANGE_PCT = 80
MAX_AVG_VOL_USD = 20_000_000
MIN_DATA_DAYS = 50


def okx_get(endpoint, params=None):
    url = f"{OKX}{endpoint}"
    last_err = None
    for _ in range(3):
        try:
            r = requests.get(url, params=params, timeout=15)
            if r.status_code == 200:
                d = r.json()
                if d.get("code") == "0":
                    return d.get("data", [])
                last_err = f"OKX code={d.get('code')} msg={d.get('msg')}"
            else:
                last_err = f"HTTP {r.status_code}"
            time.sleep(0.3)
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:80]}"
            time.sleep(1)
    if last_err:
        print(f"  ⚠️ {endpoint}: {last_err}")
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
    data = okx_get("/api/v5/public/instruments", {"instType": "SWAP"})
    if not data:
        return []
    return [d["instId"] for d in data
            if d.get("settleCcy") == "USDT" and d.get("state") == "live"]


def get_klines(inst_id, days=180):
    data = okx_get("/api/v5/market/candles", {
        "instId": inst_id, "bar": "1D", "limit": str(days)
    })
    if not data:
        return None
    klines = []
    for row in reversed(data):
        klines.append([
            int(row[0]), float(row[1]), float(row[2]), float(row[3]),
            float(row[4]), float(row[5]), int(row[0]), float(row[7]),
        ])
    return klines


def analyze(symbol, klines):
    if len(klines) < MIN_DATA_DAYS:
        return None
    data = [{"close": float(k[4]), "high": float(k[2]),
             "low": float(k[3]), "vol": float(k[7])} for k in klines]
    coin = symbol.replace("-USDT-SWAP", "")
    if coin in {"USDC", "USDP", "TUSD", "FDUSD"}:
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
    print("📊 扫描 OKX 永续合约...")
    syms = get_symbols()
    print(f"  共 {len(syms)} 个合约")
    if not syms:
        return None
    results = []
    for i, s in enumerate(syms):
        kl = get_klines(s, 180)
        if kl:
            r = analyze(s, kl)
            if r:
                results.append(r)
        if (i + 1) % 10 == 0:
            time.sleep(0.3)
        if (i + 1) % 50 == 0:
            print(f"  进度 {i+1}/{len(syms)}, 发现{len(results)}")
    results.sort(key=lambda x: x["score"], reverse=True)
    print(f"  ✅ 发现 {len(results)} 个标的")
    return results


def build_report(results):
    if not results:
        return ""
    now = datetime.now(timezone(timedelta(hours=8)))
    lines = [
        f"🏦 *庄家收筹雷达* [OKX] 标的池更新",
        f"⏰ {now.strftime('%Y-%m-%d %H:%M')} CST",
        f"━━━━━━━━━━━━━━━━━━",
        f"扫描发现 {len(results)} 个标的",
        "",
    ]
    fire = [r for r in results if "放量启动" in r["status"]]
    warm = [r for r in results if "开始放量" in r["status"]]
    sleep_ = [r for r in results if "收筹中" in r["status"]]
    if fire:
        lines.append(f"🔥 *放量启动* ({len(fire)})")
        for r in fire[:10]:
            lines.append(f"  🔥 *{r['coin']}* 分:{r['score']:.0f} 横盘{r['sideways_days']}天 波动{r['range_pct']:.0f}% Vol{r['vol_breakout']:.1f}x")
        lines.append("")
    if warm:
        lines.append(f"⚡ *开始放量* ({len(warm)})")
        for r in warm[:10]:
            lines.append(f"  ⚡ {r['coin']} 分:{r['score']:.0f} 横盘{r['sideways_days']}天 Vol{r['vol_breakout']:.1f}x")
        lines.append("")
    if sleep_:
        lines.append(f"💤 *收筹中* ({len(sleep_)})")
        for r in sleep_[:15]:
            lines.append(f"  💤 {r['coin']} 分:{r['score']:.0f} 横盘{r['sideways_days']}天 日均{fmt_usd(r['avg_vol'])}")
    return "\n".join(lines)


def send_telegram(text):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("[TG] 未配置，跳过")
        return False
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    chunks = []
    cur = ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3800:
            chunks.append(cur)
            cur = line
        else:
            cur += "\n" + line if cur else line
    if cur:
        chunks.append(cur)
    sent_any = False
    for i, ch in enumerate(chunks):
        try:
            r = requests.post(url, json={
                "chat_id": TG_CHAT_ID, "text": ch,
                "parse_mode": "Markdown"
            }, timeout=10)
            if r.status_code == 200:
                print(f"[TG] Sent ✓ ({len(ch)} chars)")
                sent_any = True
            else:
                # Markdown 失败，降级为纯文本
                plain = ch.replace("*", "").replace("_", "").replace("`", "")
                r2 = requests.post(url, json={
                    "chat_id": TG_CHAT_ID, "text": plain
                }, timeout=10)
                if r2.status_code == 200:
                    print(f"[TG] Sent plain ✓")
                    sent_any = True
                else:
                    print(f"[TG] Failed: {r2.status_code} {r2.text[:200]}")
        except Exception as e:
            print(f"[TG] Error: {type(e).__name__}: {e}")
        time.sleep(0.5)
    return sent_any


def send_feishu(text):
    """飞书备用通道（如果 webhook 配置了且可用）"""
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
    title = text.split("\n")[0].replace("*", "").strip()[:60]
    color = "blue" if "收筹" in title or "标的池" in title else "red"
    sent_any = False
    for i, ch in enumerate(chunks):
        t = title if len(chunks) == 1 else f"{title} ({i+1}/{len(chunks)})"
        try:
            r = requests.post(FEISHU_WEBHOOK, json={
                "msg_type": "interactive",
                "card": {
                    "config": {"wide_screen_mode": True},
                    "header": {"template": color, "title": {"tag": "plain_text", "content": t}},
                    "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": ch}}]
                }
            }, timeout=10)
            d = r.json() if r.text else {}
            if r.status_code == 200 and d.get("code", 0) == 0:
                print(f"[飞书] Sent ✓")
                sent_any = True
            else:
                print(f"[飞书] Failed: {d}")
        except Exception as e:
            print(f"[飞书] Error: {e}")
        time.sleep(0.3)
    return sent_any


def send(text):
    """TG 优先，飞书备用"""
    if not text:
        return
    sent = False
    # TG 优先
    if TG_BOT_TOKEN:
        sent = send_telegram(text)
    # TG 没成功才用飞书
    if not sent and FEISHU_WEBHOOK:
        sent = send_feishu(text)
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
    print(f"🏦 雷达 {now_str} 模式={mode} (OKX)")
    print(f"   推送: TG={'✓' if TG_BOT_TOKEN else '✗'} 飞书={'✓' if FEISHU_WEBHOOK else '✗'}")

    diag = (f"🔧 *雷达启动诊断* (OKX)\n"
            f"⏰ {now_str}\n"
            f"━━━━━━━━━━━━━\n"
            f"模式: {mode}\n"
            f"TG={'✓' if TG_BOT_TOKEN else '✗'} 飞书={'✓' if FEISHU_WEBHOOK else '✗'}\n"
            f"开始拉取 OKX 永续合约数据...")
    send(diag)

    conn = init_db()
    results = scan_pool()

    if results is None:
        send(f"⚠️ *OKX API 不可达*\n⏰ {now_str}\n请查看 GitHub Actions 日志。")
        conn.close()
        return

    if results:
        save(conn, results)
        report = build_report(results)
        send(report)
    else:
        send(f"📭 *本次扫描未发现收筹标的*\n⏰ {now_str}")

    conn.close()
    print("✅ 完成")


if __name__ == "__main__":
    main()
