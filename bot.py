#!/usr/bin/env python3
"""
Liquidity Matrix Telegram Bot (SHORT Setup Only)
------------------------------------------------
This version detects bearish "sweep up + red close" setups
for XAU/USD and BTC/USD around the New York session.

Auto schedules alerts 5 minutes before and after NY open (Pakistan time).
"""

import os
import time
import json
import math
import requests
from datetime import datetime, timedelta, time as dtime
from apscheduler.schedulers.background import BackgroundScheduler

# ------------------ CONFIG ------------------
TELEGRAM_TOKEN = "8287859714:AAF1pSAlSXsa-NlWIwZ4xDcaYcs3KMueu0k"
TELEGRAM_CHAT_ID = "8410854765"
TD_API_KEY = "5be1b12e0de6475a850cc5caeea9ac72"

SYMBOL_XAU = "XAU/USD"
SYMBOL_BTC = "BTC/USD"

NY_SESSION_START_PK = dtime(hour=17, minute=0)
PRE_ALERT_MINUTES = 5
POST_ALERT_MINUTES = 5

XAU_SL_PIPS = 20
BTC_SL_USD = 350
RR = 4

# ------------------ HELPERS ------------------

def send_telegram_message(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print("Telegram send error:", e)
        return None


def twelvedata_get_series(symbol: str, interval: str = "15min", outputsize: int = 100):
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
        raise RuntimeError(f"TwelveData error or invalid response: {data}")
    return list(reversed(data["values"]))


def parse_candles(raw_candles):
    out = []
    for c in raw_candles:
        out.append({
            "datetime": datetime.fromisoformat(c["datetime"]),
            "open": float(c["open"]),
            "high": float(c["high"]),
            "low": float(c["low"]),
            "close": float(c["close"]),
            "volume": float(c.get("volume") or 0)
        })
    return out

# ------------------ STRATEGY SIGNALS ------------------

def detect_sweep_and_red(candles_15m, lookback=6):
    """
    Detect 'sweep up + red close' pattern on 15m timeframe.
    Conditions:
      - Candle makes higher high than both previous and next candle.
      - Long upper wick (>40% of total range).
      - Next candle closes red (bearish).
    """
    if len(candles_15m) < lookback + 2:
        return {"signal": False, "reason": "not_enough_data"}

    window = candles_15m[-(lookback + 1):]

    for i in range(1, len(window) - 1):
        if window[i]["high"] > window[i - 1]["high"] and window[i]["high"] > window[i + 1]["high"]:
            body = abs(window[i]["open"] - window[i]["close"])
            upper_wick = window[i]["high"] - max(window[i]["open"], window[i]["close"])
            range_ = window[i]["high"] - window[i]["low"] if window[i]["high"] - window[i]["low"] > 0 else 1e-6
            if upper_wick / range_ > 0.4:
                next_candle = window[i + 1]
                if next_candle["close"] < next_candle["open"]:
                    return {
                        "signal": True,
                        "sweep_candle": window[i],
                        "confirm_candle": next_candle,
                        "sweep_index_from_end": len(window) - (i + 1)
                    }

    return {"signal": False, "reason": "no_sweep_found"}


def compute_liquidity_zones(candles, lookback_hours=24):
    lows = [c["low"] for c in candles]
    highs = [c["high"] for c in candles]
    return {
        "recent_low": min(lows),
        "recent_high": max(highs),
        "last_close": candles[-1]["close"]
    }

# ------------------ TRADE PLAN BUILDER ------------------

def build_xau_short_plan(latest_15m, latest_5m, detection):
    """Short setup for Gold (XAU/USD)"""
    sweep = detection["sweep_candle"]
    confirm = detection["confirm_candle"]
    sweep_high = sweep["high"]

    pip_value = 0.01
    sl_price = sweep_high + (XAU_SL_PIPS * pip_value)
    entry = min(confirm["open"] - 0.02, (confirm["close"] + sweep_high) / 2)
    rr_distance = sl_price - entry
    tp = entry - rr_distance * RR
    tp1 = entry - rr_distance * 1.0

    return {
        "side": "SHORT",
        "entry": round(entry, 3),
        "sl": round(sl_price, 3),
        "tp": round(tp, 3),
        "tp1": round(tp1, 3),
        "confidence": 0.85,
        "logic": "Sweep up + red confirm on 15m"
    }


def build_btc_short_plan(latest_15m, latest_5m, detection):
    """Short setup for BTC/USD"""
    sweep = detection["sweep_candle"]
    confirm = detection["confirm_candle"]
    sweep_high = sweep["high"]

    sl_price = sweep_high + BTC_SL_USD
    entry = min(confirm["open"] - 1.0, (confirm["close"] + sweep_high) / 2)
    rr_distance = sl_price - entry
    tp = entry - rr_distance * RR
    tp1 = entry - rr_distance * 1.0

    return {
        "side": "SHORT",
        "entry": round(entry, 2),
        "sl": round(sl_price, 2),
        "tp": round(tp, 2),
        "tp1": round(tp1, 2),
        "confidence": 0.75,
        "logic": "Sweep up + red confirm on 15m"
    }

# ------------------ MAIN ALERT LOGIC ------------------

def get_and_analyze(symbol, interval_15m="15min", interval_5m="5min"):
    try:
        raw_15m = twelvedata_get_series(symbol, interval=interval_15m, outputsize=200)
        raw_5m = twelvedata_get_series(symbol, interval=interval_5m, outputsize=200)
    except Exception as e:
        return {"error": f"data_fetch_error: {e}"}

    candles_15m = parse_candles(raw_15m)
    candles_5m = parse_candles(raw_5m)
    detection = detect_sweep_and_red(candles_15m, lookback=6)
    liquidity = compute_liquidity_zones(candles_15m[-96:])

    result = {
        "symbol": symbol,
        "detection": detection,
        "liquidity": liquidity,
        "latest_15m": candles_15m[-1],
        "latest_5m": candles_5m[-1]
    }

    if detection.get("signal"):
        if "XAU" in symbol:
            result["plan"] = build_xau_short_plan(candles_15m[-1], candles_5m[-1], detection)
        else:
            result["plan"] = build_btc_short_plan(candles_15m[-1], candles_5m[-1], detection)

    return result


def format_plan_message(analysis):
    if "error" in analysis:
        return f"⚠ Error fetching data: {analysis['error']}"
    if not analysis.get("plan"):
        return (
            f"ℹ <b>{analysis['symbol']}</b>\n"
            f"No Sweep + Red confirmation found on 15m.\n"
            f"Liquidity snapshot:\nLow {analysis['liquidity']['recent_low']}, High {analysis['liquidity']['recent_high']}\n"
            f"Last close: {analysis['liquidity']['last_close']}"
        )

    p = analysis["plan"]
    msg = f"<b>Pro SmartMoney SHORT Setup — {analysis['symbol']}</b>\n"
    msg += f"Logic: {p['logic']}\n"
    msg += f"Side: <b>{p['side']}</b>\n"
    msg += f"Entry: <code>{p['entry']}</code>\nSL: <code>{p['sl']}</code>\nTP: <code>{p['tp']}</code>\nTP1: <code>{p['tp1']}</code>\n"
    msg += f"Confidence: {int(p['confidence'] * 100)}%\n\n"
    msg += f"Liquidity (24h): Low {analysis['liquidity']['recent_low']}, High {analysis['liquidity']['recent_high']}\n"
    msg += f"Latest 15m close: {analysis['latest_15m']['close']}\n"
    msg += "\nTrade Management:\n- TP1 hit → move SL to BE\n- TP2 hit → scale out 50%\n- TP3 → leave runner/full close\n"
    msg += "\n---\nPowered by Liquidity Matrix Bot"
    return msg

# ------------------ SCHEDULER TASKS ------------------

def job_pre_alert():
    now = datetime.utcnow() + timedelta(hours=5)
    text = f"🕒 <b>Pre-NY Alert</b>\nTime (PK): {now.strftime('%Y-%m-%d %H:%M')}\nScanning liquidity for SHORT setups..."
    send_telegram_message(text)
    try:
        x = get_and_analyze(SYMBOL_XAU)
        b = get_and_analyze(SYMBOL_BTC)
        send_telegram_message(format_plan_message(x))
        send_telegram_message(format_plan_message(b))
    except Exception as e:
        send_telegram_message(f"Pre-alert error: {e}")


def job_post_open():
    now = datetime.utcnow() + timedelta(hours=5)
    text = f"🕒 <b>NY Post-Open Alert</b>\nTime (PK): {now.strftime('%Y-%m-%d %H:%M')}\nChecking for bearish sweep setups..."
    send_telegram_message(text)
    try:
        x = get_and_analyze(SYMBOL_XAU)
        b = get_and_analyze(SYMBOL_BTC)
        send_telegram_message(format_plan_message(x))
        send_telegram_message(format_plan_message(b))
    except Exception as e:
        send_telegram_message(f"Post-open error: {e}")

# ------------------ SCHEDULER ------------------

def start_scheduler():
    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(job_pre_alert, 'cron', hour=11, minute=55)  # PK 16:55
    sched.add_job(job_post_open, 'cron', hour=12, minute=5)   # PK 17:05
    sched.start()
    print("Scheduler started (SHORT mode). Pre-alert: 16:55 PK, Post-open: 17:05 PK")

    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        sched.shutdown()

# ------------------ RUN ------------------

if __name__ == "__main__":
    print("Starting Liquidity Matrix Bot (SHORT Setup Only)...")
    start_scheduler()
