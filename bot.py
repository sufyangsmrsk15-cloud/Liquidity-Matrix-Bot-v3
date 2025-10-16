#!/usr/bin/env python3
"""
Liquidity Matrix Bot v2.1 — Genius Setup Version
------------------------------------------------
✅ Pre-NY Alert always sends (16:55 PK)
✅ Post-NY Alert only sends if full setup confirmed (17:05 PK)
✅ No alerts on Saturday/Sunday
✅ Includes safe logic checks + Telegram error handling
"""

import os
import time
import math
import requests
from datetime import datetime, timedelta, time as dtime
from apscheduler.schedulers.background import BackgroundScheduler
from typing import List, Dict, Any, Optional

# ------------------ CONFIG ------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TD_API_KEY = os.getenv("TD_API_KEY", "")

SYMBOL_XAU = "XAU/USD"
SYMBOL_BTC = "BTC/USD"

# PK timezone session start (UTC+5)
NY_SESSION_START_PK = dtime(hour=17, minute=0)
PRE_ALERT_MINUTES = 5

# Strategy tuning
XAU_SL_PIPS = 20
XAU_PIP = 0.01
BTC_SL_USD = 350
RR = 4
SL_BUFFER_PIPS = 5
RETEST_TOUCH_ALLOWANCE = 2
CONFIRM_VOLUME_MULT = 1.0
LOOKBACK_15M = 96
LOOKBACK_5M = 288
MIN_CANDLES_REQUIRED = 20


# ------------------ HELPERS ------------------

def send_telegram_message(text: str):
    """Send Telegram message safely."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠ Telegram not configured. Message:\n", text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
    except Exception as e:
        print("❌ Telegram send error:", e)


def twelvedata_get_series(symbol: str, interval: str = "15min", outputsize: int = 200) -> List[Dict[str, Any]]:
    """Fetch from TwelveData and return oldest-first list."""
    if not TD_API_KEY:
        raise RuntimeError("TwelveData API key not set.")
    base = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": outputsize,
        "format": "JSON",
        "apikey": TD_API_KEY
    }
    r = requests.get(base, params=params, timeout=12)
    r.raise_for_status()
    data = r.json()
    if "values" not in data:
        raise RuntimeError(f"TwelveData error: {data}")
    return list(reversed(data["values"]))


def parse_candles(raw: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert raw JSON to candles with floats."""
    out = []
    for c in raw:
        vol = c.get("volume")
        vol_f = float(vol) if vol not in (None, "", "null") else 0.0
        out.append({
            "datetime": datetime.fromisoformat(c["datetime"]),
            "open": float(c["open"]),
            "high": float(c["high"]),
            "low": float(c["low"]),
            "close": float(c["close"]),
            "volume": vol_f
        })
    return out


# ------------------ DETECTION ------------------

def detect_sweep_and_green(candles_15m: List[Dict[str, Any]], lookback: int = 6) -> Dict[str, Any]]:
    """Detect liquidity sweep + green confirm candle on 15m."""
    if len(candles_15m) < lookback + 2:
        return {"signal": False, "reason": "not_enough_data"}
    window = candles_15m[-(lookback+1):]
    for i in range(1, len(window)-1):
        if window[i]["low"] < window[i-1]["low"] and window[i]["low"] < window[i+1]["low"]:
            lower_wick = abs(window[i]["close"] - window[i]["low"])
            rng = window[i]["high"] - window[i]["low"]
            if rng == 0:
                continue
            if lower_wick / rng > 0.35:
                next_c = window[i+1]
                if next_c["close"] > next_c["open"]:
                    return {
                        "signal": True,
                        "sweep_candle": window[i],
                        "confirm_candle": next_c,
                        "sweep_idx": len(candles_15m) - (lookback+1) + i
                    }
    return {"signal": False, "reason": "no_sweep"}


def detect_second_touch_and_confirmation(candles_15m, candles_5m, sweep_low, breakout_high):
    """Confirm entry only on second touch or strong confirmation candle."""
    zone_top = breakout_high
    zone_bottom = sweep_low
    touches = 0
    for c in candles_5m[-60:]:
        if zone_bottom - 0.5 <= c["low"] <= zone_top:
            touches += 1
    if touches < RETEST_TOUCH_ALLOWANCE:
        return {"ok": False, "reason": "not_enough_touches"}
    # confirm candle
    for i in range(-6, 0):
        cand = candles_5m[i]
        if cand["close"] > cand["open"]:
            return {"ok": True, "entry": round(cand["close"], 3), "confirm_candle": cand, "reason": "bull_confirm"}
    return {"ok": False, "reason": "no_confirm"}


def compute_liquidity_zones(candles):
    lows = [c["low"] for c in candles]
    highs = [c["high"] for c in candles]
    return {"recent_low": min(lows), "recent_high": max(highs), "last_close": candles[-1]["close"]}


# ------------------ TRADE PLAN ------------------

def build_trade_plan(symbol, detection, candles_15m, candles_5m):
    if not detection.get("signal"):
        return None
    sweep = detection["sweep_candle"]
    confirm = detection["confirm_candle"]
    sec = detect_second_touch_and_confirmation(candles_15m, candles_5m, sweep["low"], confirm["high"])
    if not sec.get("ok"):
        return None
    entry = sec["entry"]
    if "XAU" in symbol:
        sl = sweep["low"] - (SL_BUFFER_PIPS * XAU_PIP)
        tp = entry + (entry - sl) * RR
    else:
        sl = sweep["low"] - BTC_SL_USD
        tp = entry + (entry - sl) * RR
    return {
        "side": "LONG",
        "entry": entry,
        "sl": round(sl, 3),
        "tp": round(tp, 3),
        "logic": sec["reason"],
        "confirm_candle": sec["confirm_candle"],
        "confidence": 0.85
    }


# ------------------ ANALYSIS ------------------

def get_and_analyze(symbol):
    try:
        raw15 = twelvedata_get_series(symbol, "15min", LOOKBACK_15M)
        raw5 = twelvedata_get_series(symbol, "5min", LOOKBACK_5M)
    except Exception as e:
        return {"error": f"TwelveData fetch failed: {e}"}
    c15 = parse_candles(raw15)
    c5 = parse_candles(raw5)
    if len(c15) < MIN_CANDLES_REQUIRED or len(c5) < MIN_CANDLES_REQUIRED:
        return {"error": "not_enough_candles"}
    detection = detect_sweep_and_green(c15)
    liquidity = compute_liquidity_zones(c15)
    plan = build_trade_plan(symbol, detection, c15, c5) if detection.get("signal") else None
    return {"symbol": symbol, "liquidity": liquidity, "plan": plan, "latest": c15[-1]}


def format_plan_message(a):
    if "error" in a:
        return f"⚠ {a['symbol']} — {a['error']}"
    if not a.get("plan"):
        l = a["liquidity"]
        return (f"ℹ <b>{a['symbol']}</b>\nNo qualified setup.\n"
                f"Liquidity 24h: Low {l['recent_low']} | High {l['recent_high']}\n"
                f"Last Close: {l['last_close']}")
    p = a["plan"]
    return (f"<b>🔥 NY Confirmed Setup — {a['symbol']}</b>\n"
            f"Logic: {p['logic']}\n"
            f"Side: {p['side']}\nEntry: <code>{p['entry']}</code>\nSL: <code>{p['sl']}</code>\nTP: <code>{p['tp']}</code>\n"
            f"Confidence: {int(p['confidence']*100)}%\n"
            f"Confirm candle: {p['confirm_candle']['datetime']}\n"
            f"---\nPowered by Liquidity Matrix v2.1")


# ------------------ JOBS ------------------

def job_pre_alert():
    now = datetime.utcnow() + timedelta(hours=5)
    if now.weekday() in [5, 6]:  # skip weekends
        return
    send_telegram_message(f"🕒 <b>Pre-NY Alert</b>\nTime (PK): {now.strftime('%Y-%m-%d %H:%M')}\nScanning XAU & BTC...")
    for s in [SYMBOL_XAU, SYMBOL_BTC]:
        send_telegram_message(format_plan_message(get_and_analyze(s)))


def job_post_open():
    now = datetime.utcnow() + timedelta(hours=5)
    if now.weekday() in [5, 6]:  # skip weekends
        print("Weekend — no post alert.")
        return
    print(f"Running post-open scan at {now.strftime('%Y-%m-%d %H:%M')} PK...")
    valid = 0
    for s in [SYMBOL_XAU, SYMBOL_BTC]:
        a = get_and_analyze(s)
        if a.get("plan"):
            send_telegram_message(f"🕒 <b>NY Confirmed Setup Alert</b>\nTime (PK): {now.strftime('%Y-%m-%d %H:%M')}\n" + format_plan_message(a))
            valid += 1
    if valid == 0:
        print("No valid setup found, no message sent.")


def start_scheduler():
    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(job_pre_alert, 'cron', day_of_week='mon-fri', hour=11, minute=55)
    sched.add_job(job_post_open, 'cron', day_of_week='mon-fri', hour=12, minute=5)
    sched.start()
    print("✅ Scheduler running (Mon–Fri)\nPre-NY alert 16:55 PK | Post-open alert 17:05 PK")
    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        sched.shutdown()


# ------------------ MAIN ------------------

if __name__ == "__main__":
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID or not TD_API_KEY:
        print("⚠ Missing TELEGRAM_TOKEN / CHAT_ID / TD_API_KEY environment variables.")
    else:
        print("🚀 Starting Liquidity Matrix Bot v2.1 ...")
        start_scheduler()
