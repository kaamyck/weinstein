# weinstein_scan.py  (v3 — małe paczki + ponawianie pojedynczo, łagodniejsze dla Yahoo)
# pip install yfinance pandas lxml
import json, time, argparse, sys
import pandas as pd
import yfinance as yf

T = dict(minVol=1.5, maxBase=22.0, pivotZone=3.0, maxRisk=8.0, mansfieldMin=0.0)
FLAT = 0.3          # próg "płaskiej" SMA30 (slope %) dla rozróżnienia Stage 1/2/3/4
MIN_WEEKS = 60      # minimum historii: Mansfield(52) + slope(5) + zapas
BATCH = 5           # rozmiar paczki (małe = łagodniej dla Yahoo)
PAUSE = 2.0         # przerwa między paczkami (sekundy)
RETRIES = 3         # liczba prób pobrania zanim się poddamy

SECTOR_ETF = {
    "Technology":"XLK","Information Technology":"XLK","Communication Services":"XLC",
    "Consumer Cyclical":"XLY","Consumer Discretionary":"XLY","Consumer Defensive":"XLP",
    "Consumer Staples":"XLP","Financial Services":"XLF","Financials":"XLF","Energy":"XLE",
    "Healthcare":"XLV","Health Care":"XLV","Industrials":"XLI","Basic Materials":"XLB",
    "Materials":"XLB","Real Estate":"XLRE","Utilities":"XLU",
}
ETFS = sorted(set(SECTOR_ETF.values()))

def sma(s, p): return s.rolling(p).mean()

def classify_stage(price, sma_now, sma_prev):
    if pd.isna(sma_now) or pd.isna(sma_prev): return None, None
    slope = (sma_now - sma_prev) / sma_prev * 100
    above = price > sma_now
    rising, falling = slope > FLAT, slope < -FLAT
    if above and rising:        st = 2
    elif (not above) and falling: st = 4
    elif above and not falling:   st = 3
    else:                         st = 1
    return st, rising

def download(tickers, tries=RETRIES, **kw):
    """Pobranie z rosnącą przerwą i bez wątków (łagodniej dla Yahoo)."""
    for k in range(tries):
        try:
            df = yf.download(tickers, progress=False, threads=False, **kw)
            if df is not None and len(df): return df
        except Exception:
            pass
        time.sleep(2 + k * 2)   # 2s, 4s, 6s...
    return None

def extract(df, t, batch):
    """Wyciąga dane jednej spółki z (być może zbiorczego) wyniku."""
    if df is None: return None
    try:
        sub = df[t] if len(batch) > 1 else df
        sub = sub.dropna()
        return sub if len(sub) else None
    except Exception:
        return None

def get_universe():
    tickers, names, sectors = set(), {}, {}
    try:
        for tbl in pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"):
            sym = next((c for c in tbl.columns if "Symbol" in str(c)), None)
            if sym is None or len(tbl) < 100: continue
            sec = next((c for c in tbl.columns if "GICS Sector" in str(c)), None)
            nm  = next((c for c in tbl.columns if "Security" in str(c)), None)
            for _, r in tbl.iterrows():
                t = str(r[sym]).replace(".", "-").strip().upper()
                if not t or t == "NAN": continue
                tickers.add(t); names[t] = str(r[nm]) if nm else t
                sectors[t] = str(r[sec]) if sec else ""
            break
    except Exception as e:
        print("UWAGA: nie pobrano listy S&P 500:", e)
    try:
        best = None
        for tbl in pd.read_html("https://en.wikipedia.org/wiki/Nasdaq-100"):
            sym = next((c for c in tbl.columns if "Ticker" in str(c) or "Symbol" in str(c)), None)
            if sym is None: continue
            if best is None or len(tbl) > len(best[0]): best = (tbl, sym)
        if best:
            tbl, sym = best
            nm = next((c for c in tbl.columns if "Company" in str(c) or "Security" in str(c)), None)
            for _, r in tbl.iterrows():
                t = str(r[sym]).replace(".", "-").strip().upper()
                if not t or t == "NAN": continue
                tickers.add(t)
                if t not in names: names[t] = str(r[nm]) if nm else t
    except Exception as e:
        print("UWAGA: nie pobrano listy Nasdaq-100:", e)
    return sorted(tickers), names, sectors

def fill_missing_sectors(tickers, sectors):
    missing = [t for t in tickers if not sectors.get(t)]
    for t in missing:
        try: sectors[t] = (yf.Ticker(t).get_info().get("sector") or "")
        except Exception: sectors[t] = ""
    return len(missing)

def weekly_metrics(wk, spy_close):
    if wk is None or len(wk) < MIN_WEEKS: return None
    close, high, low, vol = wk["Close"], wk["High"], wk["Low"], wk["Volume"]
    price = float(close.iloc[-1])
    s10, s30, s40 = sma(close,10), sma(close,30), sma(close,40)
    sma30_now, sma30_prev = float(s30.iloc[-1]), float(s30.iloc[-6])
    stage, rising = classify_stage(price, sma30_now, sma30_prev)
    if stage is None: return None
    spy = spy_close.reindex(close.index, method="ffill")
    ratio = close / spy
    rsma = sma(ratio, 52)
    if pd.isna(rsma.iloc[-1]): return None
    mansfield = float((ratio.iloc[-1] / rsma.iloc[-1] - 1) * 100)
    rs_prev = float((ratio.iloc[-2] / rsma.iloc[-2] - 1) * 100) if not pd.isna(rsma.iloc[-2]) else mansfield
    pivot = float(high.iloc[-21:-1].max())
    dist = (price - pivot) / pivot * 100
    base = float((high.iloc[-12:].max() - low.iloc[-12:].min()) / low.iloc[-12:].min() * 100)
    breakout_vol = float(vol.iloc[-1] / vol.iloc[-21:-1].mean())
    change_1w = float((close.iloc[-1] / close.iloc[-2] - 1) * 100)
    swing_low = float(low.iloc[-6:].min())
    risk = max(0.1, (price - swing_low) / price * 100)
    return dict(price=round(price,2), change1W=round(change_1w,2),
        mansfield=round(mansfield,2), mansfieldRising=bool(mansfield>rs_prev),
        breakoutVol=round(breakout_vol,2), base=round(base,1), riskToStop=round(risk,1),
        pivot=round(pivot,2), distToPivot=round(dist,2), stage=int(stage),
        priceAboveSMA30=bool(price>sma30_now), sma30Rising=bool(rising),
        sma10=round(float(s10.iloc[-1]),2), sma30=round(sma30_now,2), sma40=round(float(s40.iloc[-1]),2),
        stopLoss=round(price*(1-risk/100),2))

def adr_from_daily(dly):
    if dly is None or len(dly) < 14: return None
    return round(float(((dly["High"]/dly["Low"]-1)*100).iloc[-14:].mean()), 1)

def weekly_series(wk, n=110):
    out = []
    for ts, row in wk.iloc[-n:].iterrows():
        out.append({"ts":int(pd.Timestamp(ts).timestamp()*1000),
                    "o":round(float(row["Open"]),2),"h":round(float(row["High"]),2),
                    "l":round(float(row["Low"]),2),"c":round(float(row["Close"]),2),
                    "v":round(float(row["Volume"])/1e6,2)})
    return out

def gate(s):
    d = s["distToPivot"]
    if s["stage"]!=2 or not s["priceAboveSMA30"] or s["mansfield"]<T["mansfieldMin"]: return "NIE KUPUJ"
    ok = (s["sma30Rising"] and s["mansfield"]>T["mansfieldMin"] and s["mansfieldRising"]
          and 0<=d<=T["pivotZone"] and s["breakoutVol"]>=T["minVol"] and s["base"]<=T["maxBase"]
          and s["riskToStop"]<=T["maxRisk"] and s["sectorTrend"]!="OFF")
    return "KUP" if ok else "CZEKAJ"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--batch", type=int, default=BATCH)
    ap.add_argument("--pause", type=float, default=PAUSE)
    args = ap.parse_args()
    B, PAUSE_S = max(1, args.batch), args.pause

    tickers, names, sectors = get_universe()
    print(f"Uniwersum: {len(tickers)} spółek (S&P 500 u Nasdaq 100) | paczka={B}, przerwa={PAUSE_S}s")
    if len(tickers) < 400:
        print("UWAGA: lista wygląda na niekompletną — sprawdź połączenie / strukturę Wikipedii.")
    nmiss = fill_missing_sectors(tickers, sectors)
    if nmiss: print(f"Dociągnięto sektor dla {nmiss} spółek (spoza S&P 500).")
    if args.limit: tickers = tickers[:args.limit]

    idx = download(["SPY","QQQ"]+ETFS, period="3y", interval="1wk", auto_adjust=True, group_by="ticker")
    if idx is None: sys.exit("BŁĄD: nie pobrano danych indeksów. Uruchom ponownie.")
    def wk_close(sym): return idx[sym]["Close"].dropna()
    spy_close, qqq_close = wk_close("SPY"), wk_close("QQQ")

    sector_trend = {}
    for e in ETFS:
        try:
            c = wk_close(e)
            st,_ = classify_stage(float(c.iloc[-1]), float(sma(c,30).iloc[-1]), float(sma(c,30).iloc[-6]))
            sector_trend[e] = "ON" if st==2 else "OFF" if st==4 else "NEU"
        except Exception: sector_trend[e] = "NEU"

    def market_block(c):
        s30 = sma(c,30)
        return dict(price=round(float(c.iloc[-1]),2),
                    change=round(float((c.iloc[-1]/c.iloc[-2]-1)*100),2),
                    aboveSMA30=bool(c.iloc[-1] > s30.iloc[-1]))

    stocks, too_short, errors = [], [], []
    total = len(tickers)
    for i in range(0, total, B):
        batch = tickers[i:i+B]
        wk  = download(batch, period="3y", interval="1wk", auto_adjust=True, group_by="ticker")
        dly = download(batch, period="3mo", interval="1d", auto_adjust=True, group_by="ticker")
        for t in batch:
            try:
                wkt = extract(wk, t, batch)
                if wkt is None:   # paczka nie oddała tej spółki -> ponów pojedynczo
                    wkt = extract(download(t, period="3y", interval="1wk", auto_adjust=True), t, [t])
                if wkt is None:
                    errors.append((t, "brak danych z Yahoo")); continue
                if len(wkt) < MIN_WEEKS:
                    too_short.append(t); continue
                m = weekly_metrics(wkt, spy_close)
                if m is None: too_short.append(t); continue
                dlt = extract(dly, t, batch)
                adr = adr_from_daily(dlt)
                if adr is None:
                    rng = (wkt["High"].iloc[-4:]/wkt["Low"].iloc[-4:]-1)*100
                    adr = round(float(rng.mean())/2.3, 1)
                etf = SECTOR_ETF.get(sectors.get(t,""), None)
                m.update(ticker=t, name=names.get(t,t), sector=etf or "—", adr=adr,
                         sectorTrend=sector_trend.get(etf,"NEU"), weekly=weekly_series(wkt))
                stocks.append(m)
            except Exception as e:
                errors.append((t, type(e).__name__+": "+str(e)[:70]))
        done = min(i+B, total)
        print(f"  {done}/{total}  |  OK:{len(stocks)}  krótkie:{len(too_short)}  błędy:{len(errors)}")
        time.sleep(PAUSE_S)

    data = {"meta":{"lastUpdate":time.strftime("%Y-%m-%d %H:%M"),
                    "universe":"S&P 500 + Nasdaq 100","count":len(stocks)},
            "market":{"spy":market_block(spy_close),"qqq":market_block(qqq_close)},
            "stocks":stocks}
    with open("data.json","w",encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False)

    # ----- PEŁNY RAPORT -----
    counts = {"KUP":0,"CZEKAJ":0,"NIE KUPUJ":0}
    for s in stocks: counts[gate(s)] += 1
    print(f"\nZapisano data.json — ocenione: {len(stocks)} spółek.")
    print(f"Wynik: KUP {counts['KUP']} | CZEKAJ {counts['CZEKAJ']} | NIE KUPUJ {counts['NIE KUPUJ']}")
    print(f"Za krótka historia (<{MIN_WEEKS} tyg.): {len(too_short)}")
    print(f"Błędy/inne pominięcia: {len(errors)}")
    for t, why in errors[:40]: print(f"    {t}: {why}")
    accounted = len(stocks) + len(too_short) + len(errors)
    if accounted != len(tickers):
        print(f"UWAGA: rozjazd ({accounted} != {len(tickers)}) — zgłoś, sprawdzę.")
    else:
        print("OK — każdy ticker z uniwersum został rozliczony.")

if __name__ == "__main__":
    main()
