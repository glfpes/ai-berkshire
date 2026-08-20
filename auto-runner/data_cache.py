#!/usr/bin/env python3
"""
data_cache.py — 回测数据准备层

职责：
  - 批量拉取一批标的过去 N 天的日线数据，落地到本地缓存（JSON）
  - 后续回测/优化反复读取时走缓存，避免重复请求 Yahoo 被限流
  - 提供「按截止日切片」的辅助，回测时严格只用建仓日之前的数据

缓存文件：auto-runner/.cache/{ticker}.json
用法：
  from data_cache import ensure_cache, load_prices, slice_until
  ensure_cache(["AAPL","MSFT",...], days=400)   # 一次性拉取并缓存
  rows = load_prices("AAPL")                     # 读缓存
  hist = slice_until(rows, "2026-07-01", 60)     # 截至某日的最后60个交易日
"""

import json
import os
import time

from market_data import fetch_prices, fetch_valuation

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
VAL_CACHE = os.path.join(CACHE_DIR, "_valuations.json")


def _cache_path(ticker):
    return os.path.join(CACHE_DIR, f"{ticker.replace('.', '_')}.json")


def ensure_cache(tickers, days=400, force=False, sleep=0.0):
    """批量拉取并缓存。已存在且非 force 则跳过。返回 {ticker: 天数}。"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    result = {}
    for t in tickers:
        path = _cache_path(t)
        if os.path.exists(path) and not force:
            try:
                rows = json.load(open(path))
                result[t] = len(rows)
                continue
            except Exception:
                pass
        rows = fetch_prices(t, days=days)
        if rows:
            json.dump(rows, open(path, "w"))
            result[t] = len(rows)
        else:
            result[t] = 0
        if sleep:
            time.sleep(sleep)
    return result


def load_prices(ticker):
    """从缓存读取；无缓存返回 None。"""
    path = _cache_path(ticker)
    if not os.path.exists(path):
        return None
    try:
        return json.load(open(path))
    except Exception:
        return None


def slice_until(rows, cutoff_date, n=60):
    """取 date <= cutoff_date 的最后 n 个交易日。"""
    if not rows:
        return None
    upto = [r for r in rows if r["date"] <= cutoff_date]
    if len(upto) < n:
        return upto if upto else None
    return upto[-n:]


def slice_after(rows, anchor_date, n):
    """取 date > anchor_date 的前 n 个交易日（用于持有期复盘）。"""
    if not rows:
        return []
    return [r for r in rows if r["date"] > anchor_date][:n]


def trading_days(rows, start=None, end=None):
    """返回区间内的交易日列表。"""
    ds = [r["date"] for r in rows] if rows else []
    if start:
        ds = [d for d in ds if d >= start]
    if end:
        ds = [d for d in ds if d <= end]
    return ds


# ============================================================
# PE 缓存（当前值近似历史 PE，随参数不变，缓存一次全程复用）
# ============================================================

def ensure_valuation_cache(tickers, force=False):
    """批量拉取估值并缓存到单一 JSON。返回 {ticker: valuation_dict}。"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache = {}
    if os.path.exists(VAL_CACHE) and not force:
        try:
            cache = json.load(open(VAL_CACHE))
        except Exception:
            cache = {}
    changed = False
    for t in tickers:
        if t in cache and not force:
            continue
        v = fetch_valuation(t)
        cache[t] = v
        changed = True
    if changed:
        json.dump(cache, open(VAL_CACHE, "w"), ensure_ascii=False)
    return cache


def load_valuation(ticker):
    if not os.path.exists(VAL_CACHE):
        return None
    try:
        cache = json.load(open(VAL_CACHE))
        return cache.get(ticker)
    except Exception:
        return None
