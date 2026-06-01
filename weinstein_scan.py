# weinstein_scan.py
# v5 — GitHub Actions friendly
# Naprawia problem: Wikipedia 403 Forbidden na GitHub Actions
# Pobiera S&P 500 + Nasdaq-100, analizuje po 5 spółek, zapisuje data.json

import json
import time
import argparse
import sys
from datetime import datetime
from io import StringIO

import requests
import pandas as pd
import yfinance as yf


T = {
    "minVol": 1.5,
    "maxBase": 22.0,
    "pivotZone": 3.0,
    "maxRisk": 8.0,
    "mansfieldMin": 0.0
}

FLAT = 0.3
MIN_WEEKS = 60
BATCH = 5
PAUSE = 3.0
RETRIES = 3

SECTOR_ETF = {
    "Technology": "XLK",
    "Information Technology": "XLK",
    "Communication Services": "XLC",
    "Consumer Cyclical": "XLY",
    "Consumer Discretionary": "XLY",
    "Consumer Defensive": "XLP",
    "Consumer Staples": "XLP",
    "Financial Services": "XLF",
    "Financials": "XLF",
    "Energy": "XLE",
    "Healthcare": "XLV",
    "Health Care": "XLV",
    "Industrials": "XLI",
    "Basic Materials": "XLB",
    "Materials": "XLB",
    "Real Estate": "XLRE",
    "Utilities": "XLU",
}

ETFS = sorted(set(SECTOR_ETF.values()))


def sma(s, p):
    return s.rolling(p).mean()


def classify_stage(price, sma_now, sma_prev):
    if pd.isna(sma_now) or pd.isna(sma_prev) or sma_prev == 0:
        return None, None

    slope = (sma_now - sma_prev) / sma_prev * 100
    above = price > sma_now
    rising = slope > FLAT
    falling = slope < -FLAT

    if above and rising:
        stage = 2
    elif (not above) and falling:
        stage = 4
    elif above and not falling:
        stage = 3
    else:
        stage = 1

    return stage, rising


def read_html_with_headers(url):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    }

    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()

    return pd.read_html(StringIO(r.text))


def get_universe():
    tickers = set()
    names = {}
    sectors = {}

    try:
        tables = read_html_with_headers(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        )

        for tbl in tables:
            sym_col = next((c for c in tbl.columns if "Symbol" in str(c)), None)

            if sym_col is None or len(tbl) < 100:
                continue

            sector_col = next((c for c in tbl.columns if "GICS Sector" in str(c)), None)
            name_col = next((c for c in tbl.columns if "Security" in str(c)), None)

            for _, row in tbl.iterrows():
                t = str(row[sym_col]).replace(".", "-").strip().upper()

                if not t or t == "NAN":
                    continue

                tickers.add(t)
                names[t] = str(row[name_col]) if name_col else t
                sectors[t] = str(row[sector_col]) if sector_col else ""

            print(f"Pobrano S&P 500: {len(tickers)} tickerów")
            break

    except Exception as e:
        print("UWAGA: nie pobrano listy S&P 500:", e)

    try:
        tables = read_html_with_headers(
            "https://en.wikipedia.org/wiki/Nasdaq-100"
        )

        best = None

        for tbl in tables:
            sym_col = next(
                (c for c in tbl.columns if "Ticker" in str(c) or "Symbol" in str(c)),
                None
            )

            if sym_col is None:
                continue

            if best is None or len(tbl) > len(best[0]):
                best = (tbl, sym_col)

        if best:
            tbl, sym_col = best

            name_col = next(
                (c for c in tbl.columns if "Company" in str(c) or "Security" in str(c)),
                None
            )

            before = len(tickers)

            for _, row in tbl.iterrows():
                t = str(row[sym_col]).replace(".", "-").strip().upper()

                if not t or t == "NAN":
                    continue

                tickers.add(t)

                if t not in names:
                    names[t] = str(row[name_col]) if name_col else t

                if t not in sectors:
                    sectors[t] = ""

            print(f"Pobrano Nasdaq-100: +{len(tickers) - before} nowych tickerów")

    except Exception as e:
        print("UWAGA: nie pobrano listy Nasdaq-100:", e)

    return sorted(tickers), names, sectors


def fill_missing_sectors(tickers, sectors):
    missing = [t for t in tickers if not sectors.get(t)]
    filled = 0

    for t in missing:
        try:
            info = yf.Ticker(t).get_info()
            sectors[t] = info.get("sector") or ""
            filled += 1
            time.sleep(0.15)
        except Exception:
            sectors[t] = ""

    return filled


def download(tickers, tries=RETRIES, **kwargs):
    for attempt in range(tries):
        try:
            df = yf.download(
                tickers,
                progress=False,
                threads=False,
                **kwargs
            )

            if df is not None and not df.empty:
                return df

        except Exception as e:
            print(f"UWAGA: błąd pobierania {tickers}: {type(e).__name__}: {str(e)[:100]}")

        time.sleep(2 + attempt * 2)

    return None


def extract(df, ticker):
    if df is None or df.empty:
        return None

    ticker = str(ticker).upper().strip()

    try:
        if isinstance(df.columns, pd.MultiIndex):
            level0 = [str(x).upper() for x in df.columns.get_level_values(0)]
            level1 = [str(x).upper() for x in df.columns.get_level_values(1)]

            if ticker in level0:
                sub = df.xs(ticker, axis=1, level=0)
            elif ticker in level1:
                sub = df.xs(ticker, axis=1, level=1)
            else:
                return None
        else:
            sub = df.copy()

        sub.columns = [str(c).strip().title() for c in sub.columns]

        needed = ["Open", "High", "Low", "Close", "Volume"]
        missing = [c for c in needed if c not in sub.columns]

        if missing:
            return None

        sub = sub[needed].copy()
        sub = sub.dropna()

        return sub if len(sub) else None

    except Exception:
        return None


def weekly_metrics(wk, spy_close):
    if wk is None or len(wk) < MIN_WEEKS:
        return None

    close = wk["Close"]
    high = wk["High"]
    low = wk["Low"]
    vol = wk["Volume"]

    if len(close.dropna()) < MIN_WEEKS:
        return None

    price = float(close.iloc[-1])

    s10 = sma(close, 10)
    s30 = sma(close, 30)
    s40 = sma(close, 40)

    if len(s30) < 31 or pd.isna(s30.iloc[-1]) or pd.isna(s30.iloc[-6]):
        return None

    sma30_now = float(s30.iloc[-1])
    sma30_prev = float(s30.iloc[-6])

    stage, rising = classify_stage(price, sma30_now, sma30_prev)

    if stage is None:
        return None

    spy = spy_close.reindex(close.index, method="ffill")

    if spy is None or len(spy.dropna()) < MIN_WEEKS:
        return None

    ratio = close / spy
    rsma = sma(ratio, 52)

    if pd.isna(rsma.iloc[-1]) or rsma.iloc[-1] == 0:
        return None

    mansfield = float((ratio.iloc[-1] / rsma.iloc[-1] - 1) * 100)

    if len(rsma) >= 2 and not pd.isna(rsma.iloc[-2]) and rsma.iloc[-2] != 0:
        rs_prev = float((ratio.iloc[-2] / rsma.iloc[-2] - 1) * 100)
    else:
        rs_prev = mansfield

    if len(high) < 22:
        return None

    pivot = float(high.iloc[-21:-1].max())

    if pivot <= 0:
        return None

    dist_to_pivot = (price - pivot) / pivot * 100

    base_low = float(low.iloc[-12:].min())
    base_high = float(high.iloc[-12:].max())

    if base_low <= 0:
        return None

    base = (base_high - base_low) / base_low * 100

    vol_avg = float(vol.iloc[-21:-1].mean())

    if vol_avg <= 0 or pd.isna(vol_avg):
        breakout_vol = 0.0
    else:
        breakout_vol = float(vol.iloc[-1] / vol_avg)

    if len(close) >= 2 and close.iloc[-2] != 0:
        change_1w = float((close.iloc[-1] / close.iloc[-2] - 1) * 100)
    else:
        change_1w = 0.0

    swing_low = float(low.iloc[-6:].min())

    if swing_low <= 0:
        return None

    risk = max(0.1, (price - swing_low) / price * 100)
    stop_loss = price * (1 - risk / 100)

    return {
        "price": round(price, 2),
        "change1W": round(change_1w, 2),

        "mansfield": round(mansfield, 2),
        "mansfieldRising": bool(mansfield > rs_prev),

        "breakoutVol": round(breakout_vol, 2),
        "base": round(base, 1),
        "riskToStop": round(risk, 1),

        "pivot": round(pivot, 2),
        "distToPivot": round(dist_to_pivot, 2),

        "stage": int(stage),
        "priceAboveSMA30": bool(price > sma30_now),
        "sma30Rising": bool(rising),

        "sma10": round(float(s10.iloc[-1]), 2) if not pd.isna(s10.iloc[-1]) else None,
        "sma30": round(sma30_now, 2),
        "sma40": round(float(s40.iloc[-1]), 2) if not pd.isna(s40.iloc[-1]) else None,

        "stopLoss": round(stop_loss, 2)
    }


def adr_from_daily(dly):
    if dly is None or len(dly) < 14:
        return None

    try:
        rng = ((dly["High"] / dly["Low"] - 1) * 100)
        rng = rng.replace([float("inf"), -float("inf")], pd.NA)
        adr = float(rng.iloc[-14:].mean())

        if pd.isna(adr):
            return None

        return round(adr, 1)

    except Exception:
        return None


def weekly_series(wk, n=110):
    out = []

    if wk is None or wk.empty:
        return out

    for ts, row in wk.iloc[-n:].iterrows():
        try:
            out.append({
                "ts": int(pd.Timestamp(ts).timestamp() * 1000),
                "o": round(float(row["Open"]), 2),
                "h": round(float(row["High"]), 2),
                "l": round(float(row["Low"]), 2),
                "c": round(float(row["Close"]), 2),
                "v": round(float(row["Volume"]) / 1e6, 2)
            })
        except Exception:
            continue

    return out


def gate(s):
    d = s["distToPivot"]

    if (
        s["stage"] != 2
        or not s["priceAboveSMA30"]
        or s["mansfield"] < T["mansfieldMin"]
    ):
        return "NIE KUPUJ"

    ok = (
        s["sma30Rising"]
        and s["mansfield"] > T["mansfieldMin"]
        and s["mansfieldRising"]
        and 0 <= d <= T["pivotZone"]
        and s["breakoutVol"] >= T["minVol"]
        and s["base"] <= T["maxBase"]
        and s["riskToStop"] <= T["maxRisk"]
        and s["sectorTrend"] != "OFF"
    )

    return "KUP" if ok else "CZEKAJ"


def market_block(c):
    s30 = sma(c, 30)

    return {
        "price": round(float(c.iloc[-1]), 2),
        "change": round(float((c.iloc[-1] / c.iloc[-2] - 1) * 100), 2),
        "aboveSMA30": bool(c.iloc[-1] > s30.iloc[-1])
    }


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--batch", type=int, default=BATCH)
    parser.add_argument("--pause", type=float, default=PAUSE)
    parser.add_argument("--debug", action="store_true")

    args = parser.parse_args()

    batch_size = max(1, args.batch)
    pause_s = max(0.5, args.pause)

    print("\n=== Weinstein Scanner ===")
    print(f"Paczka: {batch_size} spółek")
    print(f"Przerwa: {pause_s}s")
    print("=========================\n")

    tickers, names, sectors = get_universe()

    print(f"Uniwersum: {len(tickers)} spółek — S&P 500 + Nasdaq-100")

    if len(tickers) == 0:
        print("BŁĄD: uniwersum ma 0 spółek. Nie ma czego analizować.")

    if len(tickers) < 400:
        print("UWAGA: lista wygląda na niekompletną.")

    n_filled = fill_missing_sectors(tickers, sectors)

    if n_filled:
        print(f"Dociągnięto sektor dla {n_filled} spółek.")

    if args.limit:
        tickers = tickers[:args.limit]
        print(f"Tryb testowy: analizuję tylko {len(tickers)} spółek.")

    print("\nPobieram SPY, QQQ i ETF-y sektorowe...")

    index_symbols = ["SPY", "QQQ"] + ETFS

    idx = download(
        index_symbols,
        period="3y",
        interval="1wk",
        auto_adjust=True,
        group_by="ticker"
    )

    if idx is None:
        sys.exit("BŁĄD: nie pobrano danych indeksów. Uruchom ponownie.")

    def wk_close(symbol):
        sub = extract(idx, symbol)

        if sub is None:
            raise ValueError(f"Brak danych dla {symbol}")

        return sub["Close"].dropna()

    try:
        spy_close = wk_close("SPY")
        qqq_close = wk_close("QQQ")
    except Exception as e:
        sys.exit(f"BŁĄD: nie udało się wyciągnąć SPY/QQQ: {e}")

    sector_trend = {}

    for etf in ETFS:
        try:
            c = wk_close(etf)
            s30 = sma(c, 30)

            stage, _ = classify_stage(
                float(c.iloc[-1]),
                float(s30.iloc[-1]),
                float(s30.iloc[-6])
            )

            if stage == 2:
                sector_trend[etf] = "ON"
            elif stage == 4:
                sector_trend[etf] = "OFF"
            else:
                sector_trend[etf] = "NEU"

        except Exception:
            sector_trend[etf] = "NEU"

    print("Trend sektorów:", sector_trend)

    stocks = []
    too_short = []
    errors = []

    total = len(tickers)

    print("\nStart analizy spółek...\n")

    for i in range(0, total, batch_size):
        batch = tickers[i:i + batch_size]

        print(f"Paczka {i + 1}-{min(i + batch_size, total)} / {total}: {', '.join(batch)}")

        wk = download(
            batch,
            period="3y",
            interval="1wk",
            auto_adjust=True,
            group_by="ticker"
        )

        dly = download(
            batch,
            period="3mo",
            interval="1d",
            auto_adjust=True,
            group_by="ticker"
        )

        for ticker in batch:
            try:
                wkt = extract(wk, ticker)

                if wkt is None:
                    print(f"  {ticker}: brak danych w paczce, ponawiam pojedynczo...")

                    single_wk = download(
                        ticker,
                        period="3y",
                        interval="1wk",
                        auto_adjust=True
                    )

                    wkt = extract(single_wk, ticker)

                if wkt is None:
                    errors.append((ticker, "brak danych z Yahoo"))
                    print(f"  {ticker}: BŁĄD — brak danych")
                    continue

                if len(wkt) < MIN_WEEKS:
                    too_short.append(ticker)
                    print(f"  {ticker}: pominięto — za krótka historia")
                    continue

                metrics = weekly_metrics(wkt, spy_close)

                if metrics is None:
                    too_short.append(ticker)
                    print(f"  {ticker}: pominięto — brak metryk")
                    continue

                dlt = extract(dly, ticker)
                adr = adr_from_daily(dlt)

                if adr is None:
                    try:
                        weekly_range = (wkt["High"].iloc[-4:] / wkt["Low"].iloc[-4:] - 1) * 100
                        adr = round(float(weekly_range.mean()) / 2.3, 1)
                    except Exception:
                        adr = None

                sector_name = sectors.get(ticker, "")
                etf = SECTOR_ETF.get(sector_name, None)

                metrics.update({
                    "ticker": ticker,
                    "name": names.get(ticker, ticker),
                    "sector": etf or "—",
                    "sectorName": sector_name or "",
                    "adr": adr,
                    "sectorTrend": sector_trend.get(etf, "NEU"),
                    "weekly": weekly_series(wkt)
                })

                metrics["signal"] = gate(metrics)

                stocks.append(metrics)

                print(
                    f"  {ticker}: OK | Stage {metrics['stage']} | "
                    f"Mansfield {metrics['mansfield']} | "
                    f"Signal: {metrics['signal']}"
                )

            except Exception as e:
                msg = f"{type(e).__name__}: {str(e)[:100]}"
                errors.append((ticker, msg))
                print(f"  {ticker}: BŁĄD — {msg}")

        done = min(i + batch_size, total)

        print(
            f"\nPostęp: {done}/{total} | "
            f"OK: {len(stocks)} | "
            f"krótkie/pominięte: {len(too_short)} | "
            f"błędy: {len(errors)}\n"
        )

        time.sleep(pause_s)

    order = {
        "KUP": 0,
        "CZEKAJ": 1,
        "NIE KUPUJ": 2
    }

    stocks = sorted(
        stocks,
        key=lambda s: (
            order.get(s.get("signal", "NIE KUPUJ"), 9),
            -s.get("mansfield", -999),
            abs(s.get("distToPivot", 999))
        )
    )

    data = {
        "meta": {
            "lastUpdate": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "universe": "S&P 500 + Nasdaq 100",
            "count": len(stocks),
            "batch": batch_size,
            "model": "Weinstein Scanner v5"
        },
        "settings": T,
        "market": {
            "spy": market_block(spy_close),
            "qqq": market_block(qqq_close)
        },
        "sectorTrend": sector_trend,
        "stocks": stocks
    }

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

    counts = {
        "KUP": 0,
        "CZEKAJ": 0,
        "NIE KUPUJ": 0
    }

    for s in stocks:
        counts[s.get("signal", "NIE KUPUJ")] += 1

    print("\n==============================")
    print("ZAPISANO data.json")
    print("==============================")
    print(f"Ocenione spółki: {len(stocks)}")
    print(f"KUP: {counts['KUP']}")
    print(f"CZEKAJ: {counts['CZEKAJ']}")
    print(f"NIE KUPUJ: {counts['NIE KUPUJ']}")
    print(f"Za krótka historia / brak metryk: {len(too_short)}")
    print(f"Błędy: {len(errors)}")

    if errors:
        print("\nPierwsze błędy:")
        for ticker, why in errors[:40]:
            print(f"  {ticker}: {why}")

    accounted = len(stocks) + len(too_short) + len(errors)

    if accounted != len(tickers):
        print(f"\nUWAGA: rozjazd liczby tickerów: {accounted} != {len(tickers)}")
    else:
        print("\nOK — każdy ticker został rozliczony.")

    print("\nGotowe.")


if __name__ == "__main__":
    main()
