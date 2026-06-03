# weinstein_scan.py
# v6.1 — GitHub Actions friendly
# Skanuje S&P 500 + Nasdaq-100.
# Do data.json zapisuje tylko KUP + jakościowy WATCH.
# NIE KUPUJ zostaje tylko w diagnostyce, nie trafia do dashboardu.

import argparse
import json
import sys
import time
from datetime import datetime
from io import StringIO

import pandas as pd
import requests
import yfinance as yf


T = {
    "minVol": 1.5,
    "maxBase": 22.0,
    "pivotZone": 3.0,
    "maxRisk": 8.0,
    "mansfieldMin": 0.0,

    # Progi dla WATCH.
    "watchMaxBaseMult": 1.25,
    "watchMaxRiskMult": 1.35,
    "watchBelowPivot": -6.0,
    "watchAbovePivotExtra": 6.0,
    "minWatchScore": 55,
    "maxWatchCount": 80,
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

DEAD_TICKERS = {
    "ALXN", "ANSS", "BMC", "CEPH", "CERN", "CMCSK", "CTRP", "CTRX",
    "DISCA", "DISH", "DTV", "ENDP", "FB", "FLIR", "FMCN", "FWLT",
    "GMCR", "HANS", "KFT", "KRFT", "LEAP", "LMCA", "MXIM", "MYL",
    "NUAN", "PPDI", "SGEN", "SHPG", "SPLK", "SRCL", "STRZA", "UAUA",
    "VIAB", "VIP", "VMED", "WBA", "WCRX", "WFMI", "WLTW",
}


def sma(series, period):
    return series.rolling(period).mean()


def clean_ticker(value):
    if value is None:
        return ""

    ticker = str(value).strip().upper().replace(".", "-")

    if ticker in ("", "NAN", "NONE"):
        return ""

    return ticker


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

    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()

    return pd.read_html(StringIO(response.text))


def get_universe():
    tickers = set()
    names = {}
    sectors = {}

    try:
        tables = read_html_with_headers(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        )

        for table in tables:
            symbol_col = next((c for c in table.columns if "Symbol" in str(c)), None)

            if symbol_col is None or len(table) < 400:
                continue

            sector_col = next((c for c in table.columns if "GICS Sector" in str(c)), None)
            name_col = next((c for c in table.columns if "Security" in str(c)), None)

            for _, row in table.iterrows():
                ticker = clean_ticker(row[symbol_col])

                if not ticker or ticker in DEAD_TICKERS:
                    continue

                tickers.add(ticker)
                names[ticker] = str(row[name_col]) if name_col else ticker
                sectors[ticker] = str(row[sector_col]) if sector_col else ""

            print(f"Pobrano S&P 500: {len(tickers)} tickerów")
            break

    except Exception as exc:
        print("UWAGA: nie pobrano listy S&P 500:", exc)

    try:
        tables = read_html_with_headers("https://en.wikipedia.org/wiki/Nasdaq-100")
        candidates = []

        for table in tables:
            cols = [str(c) for c in table.columns]
            symbol_col = next(
                (c for c in table.columns if "Ticker" in str(c) or "Symbol" in str(c)),
                None,
            )

            if symbol_col is None:
                continue

            has_company = any("Company" in c or "Security" in c for c in cols)
            has_current_size = 80 <= len(table) <= 130

            if has_company and has_current_size:
                candidates.append((table, symbol_col))

        if candidates:
            table, symbol_col = candidates[0]
            name_col = next(
                (c for c in table.columns if "Company" in str(c) or "Security" in str(c)),
                None,
            )

            before = len(tickers)

            for _, row in table.iterrows():
                ticker = clean_ticker(row[symbol_col])

                if not ticker or ticker in DEAD_TICKERS:
                    continue

                tickers.add(ticker)

                if ticker not in names:
                    names[ticker] = str(row[name_col]) if name_col else ticker

                if ticker not in sectors:
                    sectors[ticker] = ""

            print(f"Pobrano Nasdaq-100: +{len(tickers) - before} nowych tickerów")

    except Exception as exc:
        print("UWAGA: nie pobrano listy Nasdaq-100:", exc)

    clean = sorted(t for t in tickers if t and t not in DEAD_TICKERS)
    return clean, names, sectors


def fill_missing_sectors(tickers, sectors):
    missing = [ticker for ticker in tickers if not sectors.get(ticker)]
    filled = 0

    for ticker in missing:
        try:
            info = yf.Ticker(ticker).get_info()
            sectors[ticker] = info.get("sector") or ""
            filled += 1
            time.sleep(0.15)
        except Exception:
            sectors[ticker] = ""

    return filled


def download(tickers, tries=RETRIES, **kwargs):
    for attempt in range(tries):
        try:
            df = yf.download(
                tickers,
                progress=False,
                threads=False,
                **kwargs,
            )

            if df is not None and not df.empty:
                return df

        except Exception as exc:
            print(
                f"UWAGA: błąd pobierania {tickers}: "
                f"{type(exc).__name__}: {str(exc)[:100]}"
            )

        time.sleep(2 + attempt * 2)

    return None


def extract(df, ticker):
    if df is None or df.empty:
        return None

    ticker = clean_ticker(ticker)

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
        if any(col not in sub.columns for col in needed):
            return None

        sub = sub[needed].copy().dropna()
        return sub if len(sub) else None

    except Exception:
        return None


def weekly_metrics(weekly_df, spy_close):
    if weekly_df is None or len(weekly_df) < MIN_WEEKS:
        return None

    close = weekly_df["Close"]
    high = weekly_df["High"]
    low = weekly_df["Low"]
    volume = weekly_df["Volume"]

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

    stage, sma30_rising = classify_stage(price, sma30_now, sma30_prev)

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

    vol_avg = float(volume.iloc[-21:-1].mean())

    if vol_avg <= 0 or pd.isna(vol_avg):
        breakout_vol = 0.0
    else:
        breakout_vol = float(volume.iloc[-1] / vol_avg)

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
        "sma30Rising": bool(sma30_rising),
        "sma10": round(float(s10.iloc[-1]), 2) if not pd.isna(s10.iloc[-1]) else None,
        "sma30": round(sma30_now, 2),
        "sma40": round(float(s40.iloc[-1]), 2) if not pd.isna(s40.iloc[-1]) else None,
        "stopLoss": round(stop_loss, 2),
    }


def adr_from_daily(daily_df):
    if daily_df is None or len(daily_df) < 14:
        return None

    try:
        rng = ((daily_df["High"] / daily_df["Low"] - 1) * 100)
        rng = rng.replace([float("inf"), -float("inf")], pd.NA)
        adr = float(rng.iloc[-14:].mean())

        if pd.isna(adr):
            return None

        return round(adr, 1)

    except Exception:
        return None


def weekly_series(weekly_df, n=110):
    out = []

    if weekly_df is None or weekly_df.empty:
        return out

    for ts, row in weekly_df.iloc[-n:].iterrows():
        try:
            out.append(
                {
                    "ts": int(pd.Timestamp(ts).timestamp() * 1000),
                    "o": round(float(row["Open"]), 2),
                    "h": round(float(row["High"]), 2),
                    "l": round(float(row["Low"]), 2),
                    "c": round(float(row["Close"]), 2),
                    "v": round(float(row["Volume"]) / 1e6, 2),
                }
            )
        except Exception:
            continue

    return out


def signal_gate(stock):
    dist = float(stock.get("distToPivot", 999) or 999)

    if (
        stock.get("stage") != 2
        or not stock.get("priceAboveSMA30")
        or float(stock.get("mansfield", 0) or 0) < T["mansfieldMin"]
    ):
        return "NIE KUPUJ"

    is_buy = (
        stock.get("sma30Rising")
        and float(stock.get("mansfield", 0) or 0) > T["mansfieldMin"]
        and stock.get("mansfieldRising")
        and 0 <= dist <= T["pivotZone"]
        and float(stock.get("breakoutVol", 0) or 0) >= T["minVol"]
        and float(stock.get("base", 999) or 999) <= T["maxBase"]
        and float(stock.get("riskToStop", 999) or 999) <= T["maxRisk"]
        and stock.get("sectorTrend") != "OFF"
    )

    return "KUP" if is_buy else "CZEKAJ"


def trigger_status(stock):
    dist = float(stock.get("distToPivot", 999) or 999)

    if dist < 0:
        if dist >= T["watchBelowPivot"]:
            return "BELOW_PIVOT"
        return "TOO_FAR_BELOW"

    if 0 <= dist <= T["pivotZone"]:
        if float(stock.get("breakoutVol", 0) or 0) >= T["minVol"]:
            return "IN_ZONE_WITH_VOLUME"
        return "IN_ZONE_NO_VOLUME"

    if dist <= T["pivotZone"] + T["watchAbovePivotExtra"]:
        return "EXTENDED"

    return "TOO_EXTENDED"


def setup_score(stock):
    score = 0

    if stock.get("signal") == "KUP":
        score += 100

    if stock.get("stage") == 2:
        score += 25

    if stock.get("priceAboveSMA30"):
        score += 10

    if stock.get("sma30Rising"):
        score += 10

    mansfield = float(stock.get("mansfield", 0) or 0)

    if mansfield > T["mansfieldMin"]:
        score += min(35, mansfield / 3)

    if stock.get("mansfieldRising"):
        score += 10

    if stock.get("sectorTrend") == "ON":
        score += 10
    elif stock.get("sectorTrend") == "NEU":
        score += 4

    if float(stock.get("breakoutVol", 0) or 0) >= T["minVol"]:
        score += 12

    if float(stock.get("base", 999) or 999) <= T["maxBase"]:
        score += 10

    if float(stock.get("riskToStop", 999) or 999) <= T["maxRisk"]:
        score += 10

    dist = float(stock.get("distToPivot", 999) or 999)

    if 0 <= dist <= T["pivotZone"]:
        score += 15
    elif T["watchBelowPivot"] <= dist < 0:
        score += 9
    elif T["pivotZone"] < dist <= T["pivotZone"] + T["watchAbovePivotExtra"]:
        score += 4

    return int(round(score))


def is_playable_watch(stock):
    if stock.get("signal") == "KUP":
        return True

    if stock.get("signal") != "CZEKAJ":
        return False

    dist = float(stock.get("distToPivot", 999) or 999)

    if stock.get("stage") != 2:
        return False

    if not stock.get("priceAboveSMA30"):
        return False

    if float(stock.get("mansfield", 0) or 0) <= T["mansfieldMin"]:
        return False

    if stock.get("sectorTrend") == "OFF":
        return False

    if float(stock.get("base", 999) or 999) > T["maxBase"] * T["watchMaxBaseMult"]:
        return False

    if float(stock.get("riskToStop", 999) or 999) > T["maxRisk"] * T["watchMaxRiskMult"]:
        return False

    if dist < T["watchBelowPivot"]:
        return False

    if dist > T["pivotZone"] + T["watchAbovePivotExtra"]:
        return False

    if int(stock.get("setupScore", 0) or 0) < T["minWatchScore"]:
        return False

    return True


def reason_text(stock):
    signal = stock.get("signal")
    status = stock.get("triggerStatus")

    if signal == "KUP":
        return "Pełny setup: Stage 2, RS, pivot, wolumen, baza i ryzyko są zgodne."

    if signal == "NIE KUPUJ":
        reasons = []

        if stock.get("stage") != 2:
            reasons.append("poza Stage 2")

        if not stock.get("priceAboveSMA30"):
            reasons.append("cena pod SMA30W")

        if float(stock.get("mansfield", 0) or 0) < T["mansfieldMin"]:
            reasons.append("słaby Mansfield")

        if stock.get("sectorTrend") == "OFF":
            reasons.append("sektor OFF")

        return ", ".join(reasons[:2]) or "setup odrzucony"

    reasons = []

    if status == "BELOW_PIVOT":
        reasons.append("jeszcze pod pivotem")
    elif status == "IN_ZONE_NO_VOLUME":
        reasons.append("w strefie, ale bez wolumenu")
    elif status == "EXTENDED":
        reasons.append("lekko po wybiciu / trzeba uważać na RR")
    elif status == "TOO_EXTENDED":
        reasons.append("za daleko od pivotu")

    if float(stock.get("breakoutVol", 0) or 0) < T["minVol"]:
        reasons.append("brakuje wolumenu")

    if float(stock.get("base", 999) or 999) > T["maxBase"]:
        reasons.append("baza lekko szeroka")

    if float(stock.get("riskToStop", 999) or 999) > T["maxRisk"]:
        reasons.append("ryzyko do stopa podwyższone")

    if not stock.get("mansfieldRising"):
        reasons.append("RS nie rośnie")

    return ", ".join(reasons[:2]) or "dobry kandydat, czekamy na trigger"


def market_block(close):
    s30 = sma(close, 30)

    return {
        "price": round(float(close.iloc[-1]), 2),
        "change": round(float((close.iloc[-1] / close.iloc[-2] - 1) * 100), 2),
        "aboveSMA30": bool(close.iloc[-1] > s30.iloc[-1]),
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

    print("\n=== Weinstein Scanner v6.1 ===")
    print(f"Paczka: {batch_size} spółek")
    print(f"Przerwa: {pause_s}s")
    print("Tryb dashboardu: zapisuję tylko KUP + jakościowy WATCH")
    print("==============================\n")

    tickers, names, sectors = get_universe()

    print(f"Uniwersum: {len(tickers)} spółek — S&P 500 + Nasdaq-100")

    if len(tickers) == 0:
        sys.exit("BŁĄD: uniwersum ma 0 spółek. Nie ma czego analizować.")

    if len(tickers) < 400:
        print("UWAGA: lista wygląda na niekompletną.")

    filled = fill_missing_sectors(tickers, sectors)

    if filled:
        print(f"Dociągnięto sektor dla {filled} spółek.")

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
        group_by="ticker",
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
    except Exception as exc:
        sys.exit(f"BŁĄD: nie udało się wyciągnąć SPY/QQQ: {exc}")

    sector_trend = {}

    for etf in ETFS:
        try:
            close = wk_close(etf)
            s30 = sma(close, 30)

            stage, _ = classify_stage(
                float(close.iloc[-1]),
                float(s30.iloc[-1]),
                float(s30.iloc[-6]),
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

    dashboard_stocks = []
    all_counted = []
    too_short = []
    errors = []
    rejected = []

    total = len(tickers)

    print("\nStart analizy spółek...\n")

    for i in range(0, total, batch_size):
        batch = tickers[i:i + batch_size]

        print(f"Paczka {i + 1}-{min(i + batch_size, total)} / {total}: {', '.join(batch)}")

        weekly_batch = download(
            batch,
            period="3y",
            interval="1wk",
            auto_adjust=True,
            group_by="ticker",
        )

        daily_batch = download(
            batch,
            period="3mo",
            interval="1d",
            auto_adjust=True,
            group_by="ticker",
        )

        for ticker in batch:
            try:
                weekly_df = extract(weekly_batch, ticker)

                if weekly_df is None:
                    print(f"  {ticker}: brak danych w paczce, ponawiam pojedynczo...")

                    single_weekly = download(
                        ticker,
                        period="3y",
                        interval="1wk",
                        auto_adjust=True,
                    )

                    weekly_df = extract(single_weekly, ticker)

                if weekly_df is None:
                    errors.append((ticker, "brak danych z Yahoo"))
                    print(f"  {ticker}: BŁĄD — brak danych")
                    continue

                if len(weekly_df) < MIN_WEEKS:
                    too_short.append(ticker)
                    print(f"  {ticker}: pominięto — za krótka historia")
                    continue

                metrics = weekly_metrics(weekly_df, spy_close)

                if metrics is None:
                    too_short.append(ticker)
                    print(f"  {ticker}: pominięto — brak metryk")
                    continue

                daily_df = extract(daily_batch, ticker)
                adr = adr_from_daily(daily_df)

                if adr is None:
                    try:
                        weekly_range = (
                            (weekly_df["High"].iloc[-4:] / weekly_df["Low"].iloc[-4:] - 1) * 100
                        )
                        adr = round(float(weekly_range.mean()) / 2.3, 1)
                    except Exception:
                        adr = None

                sector_name = sectors.get(ticker, "")
                etf = SECTOR_ETF.get(sector_name, None)

                metrics.update(
                    {
                        "ticker": ticker,
                        "name": names.get(ticker, ticker),
                        "sector": etf or "—",
                        "sectorName": sector_name or "",
                        "adr": adr,
                        "sectorTrend": sector_trend.get(etf, "NEU"),
                        "weekly": weekly_series(weekly_df),
                    }
                )

                metrics["signal"] = signal_gate(metrics)
                metrics["triggerStatus"] = trigger_status(metrics)
                metrics["setupScore"] = setup_score(metrics)
                metrics["score"] = metrics["setupScore"]
                metrics["playable"] = is_playable_watch(metrics)
                metrics["playStatus"] = (
                    "KUP TERAZ"
                    if metrics["signal"] == "KUP"
                    else "WATCH"
                    if metrics["playable"]
                    else "ODRZUCONE"
                )
                metrics["reason"] = reason_text(metrics)

                all_counted.append(metrics)

                if metrics["playable"]:
                    dashboard_stocks.append(metrics)
                    visibility = "POKAŻ"
                else:
                    rejected.append((ticker, metrics["reason"]))
                    visibility = "UKRYJ"

                print(
                    f"  {ticker}: OK | Stage {metrics['stage']} | "
                    f"Mansfield {metrics['mansfield']} | "
                    f"Signal: {metrics['signal']} | "
                    f"Score: {metrics['setupScore']} | {visibility}"
                )

            except Exception as exc:
                msg = f"{type(exc).__name__}: {str(exc)[:100]}"
                errors.append((ticker, msg))
                print(f"  {ticker}: BŁĄD — {msg}")

        done = min(i + batch_size, total)

        print(
            f"\nPostęp: {done}/{total} | "
            f"ocenione: {len(all_counted)} | "
            f"dashboard: {len(dashboard_stocks)} | "
            f"ukryte: {len(rejected)} | "
            f"krótkie/pominięte: {len(too_short)} | "
            f"błędy: {len(errors)}\n"
        )

        time.sleep(pause_s)

    buy_stocks = [s for s in dashboard_stocks if s.get("signal") == "KUP"]
    watch_stocks = [s for s in dashboard_stocks if s.get("signal") != "KUP"]

    buy_stocks = sorted(
        buy_stocks,
        key=lambda s: (
            -s.get("setupScore", 0),
            -s.get("mansfield", -999),
            abs(s.get("distToPivot", 999)),
        ),
    )

    watch_stocks = sorted(
        watch_stocks,
        key=lambda s: (
            -s.get("setupScore", 0),
            -s.get("mansfield", -999),
            abs(s.get("distToPivot", 999)),
        ),
    )[: int(T["maxWatchCount"])]

    dashboard_stocks = buy_stocks + watch_stocks

    counts_all = {
        "KUP": 0,
        "CZEKAJ": 0,
        "NIE KUPUJ": 0,
    }

    for stock in all_counted:
        counts_all[stock.get("signal", "NIE KUPUJ")] += 1

    data = {
        "meta": {
            "lastUpdate": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "universe": "S&P 500 + Nasdaq 100",
            "count": len(dashboard_stocks),
            "shown": len(dashboard_stocks),
            "scanned": len(tickers),
            "evaluated": len(all_counted),
            "buy": len(buy_stocks),
            "watch": len(watch_stocks),
            "hiddenRejected": len(rejected),
            "tooShort": len(too_short),
            "errors": len(errors),
            "batch": batch_size,
            "model": "Weinstein Scanner v6.1 — KUP + WATCH only",
        },
        "settings": T,
        "market": {
            "spy": market_block(spy_close),
            "qqq": market_block(qqq_close),
        },
        "sectorTrend": sector_trend,
        "stocks": dashboard_stocks,
        "diagnostics": {
            "allSignals": counts_all,
            "hiddenRejected": len(rejected),
            "tooShort": too_short[:100],
            "errors": [{"ticker": t, "reason": why} for t, why in errors[:100]],
            "rejectedSample": [{"ticker": t, "reason": why} for t, why in rejected[:100]],
        },
    }

    with open("data.json", "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False)

    accounted = len(all_counted) + len(too_short) + len(errors)

    print("\n==============================")
    print("ZAPISANO data.json")
    print("==============================")
    print(f"Uniwersum: {len(tickers)}")
    print(f"Ocenione spółki: {len(all_counted)}")
    print(f"Do dashboardu: {len(dashboard_stocks)}")
    print(f"KUP: {len(buy_stocks)}")
    print(f"WATCH: {len(watch_stocks)}")
    print(f"Ukryte / odrzucone: {len(rejected)}")
    print(f"Za krótka historia / brak metryk: {len(too_short)}")
    print(f"Błędy: {len(errors)}")
    print("\nWszystkie sygnały przed filtrem dashboardu:")
    print(f"  KUP: {counts_all['KUP']}")
    print(f"  CZEKAJ: {counts_all['CZEKAJ']}")
    print(f"  NIE KUPUJ: {counts_all['NIE KUPUJ']}")

    if errors:
        print("\nPierwsze błędy:")
        for ticker, reason in errors[:40]:
            print(f"  {ticker}: {reason}")

    if accounted != len(tickers):
        print(f"\nUWAGA: rozjazd liczby tickerów: {accounted} != {len(tickers)}")
    else:
        print("\nOK — każdy ticker został rozliczony.")

    print("\nGotowe.")


if __name__ == "__main__":
    main()
