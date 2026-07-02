#!/usr/bin/env python3
"""
gather_factors.py

Download factorproxies en schrijf factors.csv voor process_pension_alpha.py.

Default market proxies via Yahoo Finance / yfinance:

    rf           XEON.DE      Xtrackers EUR Overnight Rate Swap ETF
    equity       IWDA.AS      iShares Core MSCI World UCITS ETF
    duration     IBGL.AS      iShares EUR Govt Bond 15-30yr UCITS ETF
    credit       IEAC.AS      iShares Core EUR Corp Bond UCITS ETF
    real_estate  IPRP.AS      iShares European Property Yield UCITS ETF
    fx           EURUSD=X     EUR/USD; script gebruikt -return(EURUSD) = USD strength vs EUR

Optioneel: Ken French style factors, als extra kolommen:

    ff_mkt_rf
    ff_smb
    ff_hml
    ff_rmw        alleen 5-factor model
    ff_cma        alleen 5-factor model
    ff_mom        indien --ken-french-momentum
    ff_rf         Ken French RF; ter controle, niet automatisch als rf gebruikt tenzij --rf-source ken_french

Ken French rapportage:
    factor_download_report.csv krijgt één regel per factorserie, dus bijvoorbeeld
    ff_mkt_rf, ff_smb, ff_hml, ff_rmw, ff_cma, ff_rf en ff_mom.

Output:
    factors/factors.csv
    factors/factor_prices_quarterly.csv
    factors/factor_download_report.csv
    factors/ticker_map.csv
    factors/ken_french_monthly.csv, indien gebruikt

CSV-contract voor processing:
    period,rf,equity,duration,credit,real_estate,fx,...

Alle waarden zijn kwartaalrendementen als decimalen:
    0.04 = 4%

Default vanaf v11:
    equity, duration, credit en real_estate worden als excess returns geschreven:
    factor_excess = factor_total_return - rf

    fx blijft ongewijzigd, omdat dit een zero-cost/valuta-spread proxy is.
    Ken-French ff_* factoren blijven ongewijzigd; ff_mkt_rf is al excess.

Installatie:
    pip install pandas numpy yfinance requests

Gebruik ETF-only:
    python gather_factors.py --start 2014-12-31 --first-period 2015Q1

Gebruik ETF + Ken French Europe 5-factor + momentum:
    python gather_factors.py \
      --start 2014-12-31 \
      --first-period 2015Q1 \
      --include-ken-french \
      --ken-french-region europe \
      --ken-french-model 5 \
      --ken-french-momentum

Gebruik lokale Ken-French CSV zoals handmatig gedownload:
    python gather_factors.py \
      --market-source none \
      --include-ken-french \
      --ken-french-file Europe_5_Factors.csv \
      --first-period 2015Q1 \
      --rf-source ken_french

Ken French only, met Ken French RF:
    python gather_factors.py \
      --market-source none \
      --include-ken-french \
      --ken-french-region europe \
      --ken-french-model 5 \
      --ken-french-momentum \
      --rf-source ken_french
"""

from __future__ import annotations

import argparse
import io
import re
import sys
import time
import zipfile
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

try:
    import yfinance as yf
except ImportError:
    yf = None

try:
    import requests
except ImportError:
    requests = None


DEFAULT_TICKERS = {
    "rf": "XEON.DE",
    "equity": "IWDA.AS",
    "duration": "IBGL.AS",
    "credit": "IEAC.AS",
    "real_estate": "IPRP.AS",
    "eurusd": "EURUSD=X",
}

KEN_FRENCH_URLS = {
    ("developed", "3"): "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/Developed_3_Factors_CSV.zip",
    ("developed", "5"): "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/Developed_5_Factors_CSV.zip",
    ("developed", "mom"): "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/Developed_Mom_Factor_CSV.zip",
    ("europe", "3"): "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/Europe_3_Factors_CSV.zip",
    ("europe", "5"): "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/Europe_5_Factors_CSV.zip",
    ("europe", "mom"): "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/Europe_Mom_Factor_CSV.zip",
}

BASE_OUTPUT_COLUMNS = [
    "period",
    "rf",
    "equity",
    "duration",
    "credit",
    "real_estate",
    "fx",
]


def log(message: str) -> None:
    print(message, file=sys.stderr)


def parse_period(period: str) -> pd.Period:
    text = str(period).strip().upper().replace("K", "Q")
    return pd.Period(text, freq="Q")


def quarter_end_date_from_period(period: str) -> str:
    """
    Zet een kwartaal zoals 2026Q1 of 2026K1 om naar een ISO-einddatum.

    Dit houdt workflowbestanden simpel: zij geven de rapportperiode door,
    terwijl dit script bepaalt welke Yahoo-einddatum daarbij hoort.
    """
    p = parse_period(period)
    return p.end_time.date().isoformat()


def filter_to_end_period(data: pd.DataFrame, end_period: str | None) -> pd.DataFrame:
    if data is None or data.empty or not end_period:
        return data
    if "period" not in data.columns:
        return data

    end_p = parse_period(end_period)
    periods = data["period"].astype(str).map(parse_period)
    return data.loc[periods <= end_p].copy()


def period_from_yyyymm(value: Any) -> str | None:
    text = str(value).strip()
    if not re.fullmatch(r"\d{6}", text):
        return None
    year = int(text[:4])
    month = int(text[4:6])
    if month < 1 or month > 12:
        return None
    return str(pd.Period(f"{year}-{month:02d}", freq="Q"))


def ensure_package(package_obj: Any, install_name: str) -> None:
    if package_obj is None:
        raise SystemExit(
            f"Package ontbreekt: {install_name}\n"
            f"Installeer met:\n  pip install {install_name} pandas numpy"
        )



def make_series_report_rows(
    source: str,
    data: pd.DataFrame,
    series_cols: list[str],
    ticker_or_url_map: dict[str, str],
    mode_map: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """
    Maak één reportregel per numerieke factorserie.

    Dit voorkomt aggregate regels zoals 'europe_5_factors' met NaN mean/vol.
    """
    rows = []
    mode_map = mode_map or {}

    for col in series_cols:
        if col not in data.columns:
            continue

        s = pd.to_numeric(data[col], errors="coerce")
        valid = s.notna()

        rows.append({
            "source": source,
            "series": col,
            "ticker_or_url": ticker_or_url_map.get(col, ""),
            "mode": mode_map.get(col, ""),
            "n_obs": int(valid.sum()),
            "n_missing": int(s.isna().sum()),
            "first_period": data.loc[valid, "period"].min() if valid.any() else "",
            "last_period": data.loc[valid, "period"].max() if valid.any() else "",
            "mean_quarterly_return": float(s.mean()) if valid.any() else np.nan,
            "vol_quarterly_return": float(s.std()) if valid.sum() > 1 else np.nan,
        })

    return rows



# -----------------------------------------------------------------------------
# Yahoo / ETF factors
# -----------------------------------------------------------------------------


def get_close_prices(raw: pd.DataFrame, tickers: list[str]) -> pd.DataFrame:
    """Haal Close-prijzen uit yfinance output, robuust voor MultiIndex en single-index."""
    if raw.empty:
        raise RuntimeError("yfinance gaf een lege dataframe terug.")

    if isinstance(raw.columns, pd.MultiIndex):
        if "Close" in raw.columns.get_level_values(0):
            close = raw["Close"].copy()
        elif "Adj Close" in raw.columns.get_level_values(0):
            close = raw["Adj Close"].copy()
        else:
            raise RuntimeError(
                "Geen Close/Adj Close kolommen gevonden in yfinance output. "
                f"columns voorbeeld: {raw.columns[:10]}"
            )
    else:
        if "Close" in raw.columns:
            close = raw[["Close"]].copy()
            close.columns = tickers[:1]
        elif "Adj Close" in raw.columns:
            close = raw[["Adj Close"]].copy()
            close.columns = tickers[:1]
        else:
            raise RuntimeError(f"Geen Close/Adj Close kolom gevonden. Kolommen: {list(raw.columns)}")

    for ticker in tickers:
        if ticker not in close.columns:
            close[ticker] = np.nan

    close = close[tickers].dropna(how="all")
    if close.empty:
        raise RuntimeError("Geen bruikbare prijzen na selectie van Close-kolommen.")

    return close


def download_prices(tickers: list[str], start: str, end: str | None) -> pd.DataFrame:
    ensure_package(yf, "yfinance")
    log(f"Download Yahoo tickers: {', '.join(tickers)}")
    raw = yf.download(
        tickers=tickers,
        start=start,
        end=end,
        auto_adjust=True,
        progress=False,
        group_by="column",
        threads=True,
    )
    return get_close_prices(raw, tickers)


def quarterly_last(prices: pd.DataFrame) -> pd.DataFrame:
    """Kwartaalultimo prijzen op basis van laatste beschikbare handelsdag in kwartaal."""
    try:
        return prices.resample("QE").last()
    except ValueError:
        return prices.resample("Q").last()


def build_market_factors(
    tickers: dict[str, str],
    start: str,
    end: str | None,
    first_period: str,
    rf_mode: str,
    output_dir: Path,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    output_dir.mkdir(parents=True, exist_ok=True)

    ticker_map = pd.DataFrame([
        {"factor": factor, "ticker": ticker}
        for factor, ticker in tickers.items()
    ])
    ticker_map.to_csv(output_dir / "ticker_map.csv", index=False)

    download_tickers = []
    for factor, ticker in tickers.items():
        if factor == "rf" and rf_mode == "zero":
            continue
        download_tickers.append(ticker)
    download_tickers = list(dict.fromkeys(download_tickers))

    prices_daily = download_prices(download_tickers, start=start, end=end)
    prices_q = quarterly_last(prices_daily)
    returns_q = prices_q.pct_change()

    reverse = {ticker: factor for factor, ticker in tickers.items()}
    prices_q_named = prices_q.rename(columns=reverse).copy()
    prices_q_named.index.name = "date"
    prices_q_named = prices_q_named.reset_index()
    prices_q_named["period"] = prices_q_named["date"].dt.to_period("Q").astype(str)
    prices_q_named = prices_q_named[["period", "date"] + [c for c in prices_q_named.columns if c not in ["period", "date"]]]

    factors = pd.DataFrame(index=returns_q.index)
    factors.index.name = "date"

    for factor in ["equity", "duration", "credit", "real_estate"]:
        ticker = tickers[factor]
        factors[factor] = returns_q[ticker] if ticker in returns_q.columns else np.nan

    eurusd_ticker = tickers["eurusd"]
    if eurusd_ticker in returns_q.columns:
        # EURUSD omhoog = EUR sterker / USD zwakker.
        # Voor USD exposure van een EUR-belegger gebruiken we USD strength: -EURUSD return.
        factors["fx"] = -returns_q[eurusd_ticker]
        factors["eurusd_return"] = returns_q[eurusd_ticker]
    else:
        factors["fx"] = np.nan
        factors["eurusd_return"] = np.nan

    if rf_mode == "zero":
        factors["rf"] = 0.0
    else:
        rf_ticker = tickers["rf"]
        factors["rf"] = returns_q[rf_ticker] if rf_ticker in returns_q.columns else np.nan

    factors = factors.reset_index()
    factors["period"] = factors["date"].dt.to_period("Q").astype(str)

    first_p = parse_period(first_period)
    factors = factors[factors["period"].map(parse_period) >= first_p].copy()

    if "rf" in factors.columns and len(factors) > 0 and pd.isna(factors["rf"].iloc[0]):
        factors.loc[factors.index[0], "rf"] = 0.0

    extra_cols = [c for c in factors.columns if c not in BASE_OUTPUT_COLUMNS + ["date"]]
    factors_out = factors[BASE_OUTPUT_COLUMNS + extra_cols].reset_index(drop=True)

    report = make_market_report(factors_out, tickers, rf_mode, market_factor_mode="raw")
    return factors_out, prices_q_named, report


def make_market_report(
    factors: pd.DataFrame,
    tickers: dict[str, str],
    rf_mode: str,
    market_factor_mode: str = "excess",
) -> pd.DataFrame:
    series_cols = ["rf", "equity", "duration", "credit", "real_estate", "fx", "eurusd_return"]

    ticker_map = {
        "rf": tickers.get("rf", ""),
        "equity": tickers.get("equity", ""),
        "duration": tickers.get("duration", ""),
        "credit": tickers.get("credit", ""),
        "real_estate": tickers.get("real_estate", ""),
        "fx": tickers.get("eurusd", ""),
        "eurusd_return": tickers.get("eurusd", ""),
    }

    market_mode = "excess_return_minus_rf" if market_factor_mode == "excess" else "raw_total_return"
    mode_map = {
        "rf": rf_mode,
        "equity": market_mode,
        "duration": market_mode,
        "credit": market_mode,
        "real_estate": market_mode,
        "fx": "-return(EURUSD), USD strength vs EUR; not rf-adjusted",
        "eurusd_return": "raw EURUSD return",
    }

    return pd.DataFrame(
        make_series_report_rows(
            source="yahoo",
            data=factors,
            series_cols=series_cols,
            ticker_or_url_map=ticker_map,
            mode_map=mode_map,
        )
    )


# -----------------------------------------------------------------------------
# Ken French factors
# -----------------------------------------------------------------------------


def download_ken_french_zip(url: str) -> bytes:
    ensure_package(requests, "requests")
    log(f"Download Ken French: {url}")
    response = requests.get(url, timeout=120)
    response.raise_for_status()
    return response.content


def read_ken_french_zip(url: str, cache_path: Path | None = None) -> pd.DataFrame:
    if cache_path is not None and cache_path.exists():
        content = cache_path.read_bytes()
    else:
        content = download_ken_french_zip(url)
        if cache_path is not None:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(content)

    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        names = zf.namelist()
        csv_names = [n for n in names if n.lower().endswith(".csv")]
        name = csv_names[0] if csv_names else names[0]
        raw_text = zf.read(name).decode("latin1")

    return parse_ken_french_csv_text(raw_text)


def read_ken_french_file(path: Path) -> pd.DataFrame:
    """
    Lees een lokaal Ken-French bestand.

    Ondersteunt:
    - .csv / .txt zoals de gebruiker die handmatig downloadt
    - .zip met CSV erin

    Het bestand hoeft géén normale CSV met header op regel 1 te zijn.
    parse_ken_french_csv_text zoekt zelf:
    - metadata/footer overslaan
    - headerregel vlak boven de eerste YYYYMM-regel
    - alleen maandregels YYYYMM gebruiken
    - stoppen voor jaarregels YYYY
    """
    path = Path(path)

    if not path.exists():
        raise FileNotFoundError(f"Ken-French bestand bestaat niet: {path}")

    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            csv_names = [n for n in names if n.lower().endswith(".csv")]
            name = csv_names[0] if csv_names else names[0]
            raw_text = zf.read(name).decode("latin1")
    else:
        raw_text = path.read_text(encoding="latin1")

    return parse_ken_french_csv_text(raw_text)


def parse_ken_french_csv_text(raw_text: str) -> pd.DataFrame:
    """
    Parse Ken French CSV-like text.

    Ken-French bestanden zijn meestal géén nette CSV vanaf regel 1.
    Voorbeeld uit Europe_5_Factors.csv:

        This file was created using ...
        Missing data are indicated by -99.99.
        ,Mkt-RF,SMB,HML,RMW,CMA,RF
        199007,4.46,0.2,-1.52,0.28,1.1,0.68
        ...
        202604,6.48,1.78,-0.32,-1.51,-1.26,0.29
        [daarna annual data: 1990,...]

    Parser:
    - zoekt eerste YYYYMM dataregel;
    - gebruikt de niet-lege regel daarboven als header;
    - vult lege eerste headercel met 'date';
    - neemt alleen YYYYMM maandregels;
    - stopt bij eerste YYYY annual-regel of tekst/footer;
    - zet -99.99/-999 om naar NaN;
    - zet percentages om naar decimalen.
    """
    lines = raw_text.splitlines()

    first_data_idx = None
    for idx, line in enumerate(lines):
        if re.match(r"^\s*\d{6}\s*,", line):
            first_data_idx = idx
            break

    if first_data_idx is None:
        raise RuntimeError(
            "Kon geen maandelijkse YYYYMM-regels vinden in Ken French CSV. "
            "Controleer of je de monthly factor file hebt gedownload."
        )

    header_idx = first_data_idx - 1
    while header_idx >= 0 and not lines[header_idx].strip().strip(","):
        header_idx -= 1

    if header_idx < 0:
        raise RuntimeError("Kon geen headerregel vinden boven Ken French maanddata.")

    header_cells = [cell.strip() for cell in lines[header_idx].split(",")]
    if not header_cells or header_cells[0] == "":
        header_cells[0] = "date"
    else:
        # Sommige bestanden noemen eerste kolom al Date of YYYYMM.
        header_cells[0] = "date"

    data_lines = [",".join(header_cells)]
    n_month_rows = 0
    stopped_at = ""

    for line in lines[first_data_idx:]:
        clean = line.strip()

        if re.match(r"^\d{6}\s*,", clean):
            data_lines.append(line)
            n_month_rows += 1
            continue

        if not clean or not clean.strip(","):
            continue

        if re.match(r"^\d{4}\s*,", clean):
            stopped_at = "annual_section"
            break

        stopped_at = "footer_or_text"
        break

    if n_month_rows == 0:
        raise RuntimeError("Ken French parser vond header maar geen maandregels.")

    df = pd.read_csv(io.StringIO("\n".join(data_lines)))
    df.columns = [str(c).strip() for c in df.columns]

    if "date" not in df.columns:
        df = df.rename(columns={df.columns[0]: "date"})

    # Drop eventuele lege unnamed kolommen door trailing commas.
    drop_cols = [c for c in df.columns if str(c).lower().startswith("unnamed")]
    if drop_cols:
        df = df.drop(columns=drop_cols)

    df["date"] = df["date"].astype(str).str.strip()
    df = df[df["date"].str.fullmatch(r"\d{6}")].copy()

    for col in df.columns:
        if col == "date":
            continue

        values = pd.to_numeric(df[col], errors="coerce")
        values = values.mask(values.isin([-99.99, -999, -999.0, -999.99]))
        df[col] = values / 100.0

    df["period"] = df["date"].map(period_from_yyyymm)
    df = df.dropna(subset=["period"]).reset_index(drop=True)

    # Bewaar parser-info als attrs voor debugging.
    df.attrs["ken_french_header_line"] = header_idx + 1
    df.attrs["ken_french_first_data_line"] = first_data_idx + 1
    df.attrs["ken_french_month_rows"] = n_month_rows
    df.attrs["ken_french_stopped_at"] = stopped_at

    return df


def quarterly_compound(series: pd.Series) -> float:
    values = series.dropna()
    if values.empty:
        return np.nan
    return float((1.0 + values).prod() - 1.0)


def ken_french_monthly_to_quarterly(
    monthly: pd.DataFrame,
    prefix: str = "ff",
) -> pd.DataFrame:
    rename_map = {}
    for col in monthly.columns:
        clean = col.strip().lower().replace("-", "_").replace(" ", "_")
        clean = clean.replace("mkt_rf", "mkt_rf")
        clean = clean.replace("mom", "mom")
        clean = clean.replace("wml", "mom")
        if col not in ["date", "period"]:
            rename_map[col] = f"{prefix}_{clean}"

    work = monthly.rename(columns=rename_map).copy()
    factor_cols = [c for c in work.columns if c.startswith(f"{prefix}_")]

    q = (
        work.groupby("period", as_index=False)[factor_cols]
        .agg(quarterly_compound)
        .sort_values("period")
        .reset_index(drop=True)
    )
    return q


def build_ken_french_factors(
    region: str,
    model: str,
    include_momentum: bool,
    first_period: str,
    output_dir: Path,
    ken_french_file: Path | None = None,
    ken_french_momentum_file: Path | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    output_dir.mkdir(parents=True, exist_ok=True)

    model_url = KEN_FRENCH_URLS[(region, model)]

    if ken_french_file is not None:
        monthly = read_ken_french_file(ken_french_file)
        model_source = str(ken_french_file)
    else:
        model_cache = output_dir / f"ken_french_{region}_{model}.zip"
        monthly = read_ken_french_zip(model_url, cache_path=model_cache)
        model_source = model_url

    monthly_parts = [monthly.copy()]
    q = ken_french_monthly_to_quarterly(monthly, prefix="ff")

    # Ken French report moet per kolom worden gemaakt, niet als aggregate bronregel.
    # Ook pas na first-period filtering, zodat report en factors.csv dezelfde sample hebben.
    source_url_map: dict[str, str] = {}
    for col in q.columns:
        if col != "period":
            source_url_map[col] = model_source

    if include_momentum:
        mom_url = KEN_FRENCH_URLS[(region, "mom")]

        if ken_french_momentum_file is not None:
            mom_monthly = read_ken_french_file(ken_french_momentum_file)
            mom_source = str(ken_french_momentum_file)
        else:
            mom_cache = output_dir / f"ken_french_{region}_mom.zip"
            mom_monthly = read_ken_french_zip(mom_url, cache_path=mom_cache)
            mom_source = mom_url
        monthly_parts.append(mom_monthly.copy())
        mom_q = ken_french_monthly_to_quarterly(mom_monthly, prefix="ff")

        # Momentum file heeft meestal Mom of WML. Na parser: ff_mom.
        mom_cols = [c for c in mom_q.columns if c != "period"]
        if len(mom_cols) == 1 and mom_cols[0] != "ff_mom":
            mom_q = mom_q.rename(columns={mom_cols[0]: "ff_mom"})

        for col in mom_q.columns:
            if col != "period":
                source_url_map[col] = mom_source

        q = q.merge(mom_q, on="period", how="outer")

    first_p = parse_period(first_period)
    q = q[q["period"].map(parse_period) >= first_p].sort_values("period").reset_index(drop=True)

    monthly_all = pd.concat(monthly_parts, axis=0, ignore_index=True, sort=False)
    monthly_all.to_csv(output_dir / "ken_french_monthly.csv", index=False)
    q.to_csv(output_dir / "ken_french_quarterly.csv", index=False)

    parser_inputs = [("model", monthly, model_source)]
    if include_momentum and len(monthly_parts) > 1:
        parser_inputs.append(("momentum", monthly_parts[1], source_url_map.get("ff_mom", "")))

    parser_rows = []
    for label, frame, source in parser_inputs:
        parser_rows.append({
            "file_type": label,
            "source": source,
            "header_line_1based": frame.attrs.get("ken_french_header_line", ""),
            "first_data_line_1based": frame.attrs.get("ken_french_first_data_line", ""),
            "monthly_rows": frame.attrs.get("ken_french_month_rows", ""),
            "stopped_at": frame.attrs.get("ken_french_stopped_at", ""),
            "first_period": frame["period"].min() if "period" in frame.columns else "",
            "last_period": frame["period"].max() if "period" in frame.columns else "",
            "columns": ", ".join([c for c in frame.columns if c not in ["date", "period"]]),
        })
    pd.DataFrame(parser_rows).to_csv(output_dir / "ken_french_parse_report.csv", index=False)

    ff_cols = [c for c in q.columns if c != "period"]
    mode_map = {c: "monthly_compounded_to_quarterly" for c in ff_cols}

    report = pd.DataFrame(
        make_series_report_rows(
            source="ken_french",
            data=q,
            series_cols=ff_cols,
            ticker_or_url_map=source_url_map,
            mode_map=mode_map,
        )
    )

    return q, monthly_all, report


# -----------------------------------------------------------------------------
# Combine / validate / output
# -----------------------------------------------------------------------------


def combine_factors(
    market: pd.DataFrame | None,
    ken_french: pd.DataFrame | None,
    rf_source: str,
    market_factor_mode: str = "excess",
) -> pd.DataFrame:
    if market is None and ken_french is None:
        raise RuntimeError("Geen factorbron gekozen. Gebruik --market-source yahoo en/of --include-ken-french.")

    if market is None:
        factors = ken_french.copy()
        factors["rf"] = factors["ff_rf"] if (rf_source == "ken_french" and "ff_rf" in factors.columns) else 0.0
    elif ken_french is None:
        factors = market.copy()
    else:
        factors = market.merge(ken_french, on="period", how="outer").sort_values("period").reset_index(drop=True)
        if rf_source == "ken_french" and "ff_rf" in factors.columns:
            factors["rf"] = factors["ff_rf"]
        elif "rf" not in factors.columns:
            factors["rf"] = 0.0

    # Ensure standard market columns exist for process script/report compatibility.
    for col in BASE_OUTPUT_COLUMNS:
        if col not in factors.columns:
            factors[col] = np.nan if col != "rf" else 0.0

    # Voor zuivere Jensen-alpha hoort de afhankelijke variabele én de verhandelbare
    # long-only factorproxies in excess-return termen te staan.
    # fx is een zero-cost valuta-spread proxy en Ken-French ff_* factoren zijn al
    # spreads/excess waar relevant, dus die passen we hier niet aan.
    market_cols = ["equity", "duration", "credit", "real_estate"]
    if market_factor_mode == "excess":
        for col in market_cols:
            if col in factors.columns:
                factors[col] = pd.to_numeric(factors[col], errors="coerce") - pd.to_numeric(factors["rf"], errors="coerce")
    elif market_factor_mode != "raw":
        raise RuntimeError(f"Onbekende market_factor_mode: {market_factor_mode}")

    # Keep period, rf first; then market factors; then FF extras.
    ordered = ["period", "rf", "equity", "duration", "credit", "real_estate", "fx"]
    extras = [c for c in factors.columns if c not in ordered]
    factors = factors[ordered + extras]

    return factors.sort_values("period").reset_index(drop=True)


def trim_trailing_incomplete_factor_rows(factors: pd.DataFrame) -> pd.DataFrame:
    """
    Verwijder automatisch trailing kwartalen die nog niet volledig beschikbaar zijn.

    Yahoo kan tijdens een lopend kwartaal al EUR/USD teruggeven, terwijl ETF-prijzen
    nog geen volledige kwartaalrij opleveren. Dan ontstaat bijvoorbeeld:

        2026Q3: equity/duration/credit/real_estate = NaN, fx = waarde

    Zo'n rij is geen datakwaliteitsfout in de historische sample maar een incomplete
    current-quarter artefact. Interior missings blijven wél een harde validatiefout.
    """
    if factors is None or factors.empty or "period" not in factors.columns:
        return factors

    out = factors.copy().sort_values("period").reset_index(drop=True)
    check_cols = [
        col
        for col in ["equity", "duration", "credit", "real_estate", "fx"]
        if col in out.columns and out[col].notna().any()
    ]

    if not check_cols:
        return out

    dropped: list[str] = []
    while not out.empty:
        last = out.iloc[-1]
        missing_cols = [col for col in check_cols if pd.isna(last[col])]
        if not missing_cols:
            break
        dropped.append(str(last["period"]))
        log(
            "Drop trailing incomplete factor quarter "
            f"{last['period']}: missende kolommen {', '.join(missing_cols)}"
        )
        out = out.iloc[:-1].copy()

    if dropped:
        log("Trailing incomplete factor quarters removed: " + ", ".join(dropped))

    return out.reset_index(drop=True)


def validate_factors(factors: pd.DataFrame, allow_partial_missing: bool = False) -> None:
    if "period" not in factors.columns:
        raise RuntimeError("factors mist kolom period")

    non_period = [c for c in factors.columns if c != "period"]
    if not non_period:
        raise RuntimeError("factors bevat geen factor- of rf-kolommen.")

    all_missing = [c for c in non_period if factors[c].isna().all()]
    # Market columns may be all missing in --market-source none mode; that is okay if FF columns exist.
    ff_cols = [c for c in factors.columns if c.startswith("ff_")]
    market_cols = ["equity", "duration", "credit", "real_estate", "fx", "eurusd_return"]
    hard_missing = [c for c in all_missing if c not in market_cols]
    if hard_missing:
        raise RuntimeError(
            "Deze factor-kolommen zijn volledig leeg: "
            + ", ".join(hard_missing)
            + ". Controleer brondata of internettoegang."
        )

    factor_cols = [c for c in non_period if c != "rf"]
    usable_factor_cols = [c for c in factor_cols if factors[c].notna().any()]
    if not usable_factor_cols:
        raise RuntimeError("Geen enkele factor-kolom bevat bruikbare data.")

    rows_with_missing = factors[["period"] + usable_factor_cols].isna().any(axis=1)
    if rows_with_missing.any() and not allow_partial_missing:
        sample = factors.loc[rows_with_missing, ["period"] + usable_factor_cols].head(10)
        raise RuntimeError(
            "Er zijn kwartalen met missende factorwaarden. "
            "Gebruik eventueel --allow-partial-missing als dit bewust is.\n"
            + sample.to_string(index=False)
        )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Download factorproxies naar factors.csv.")

    p.add_argument("--output-dir", type=Path, default=Path("factors"))
    p.add_argument("--output-file", type=str, default="factors.csv")
    p.add_argument("--start", default="2014-12-31")
    p.add_argument("--end", default=None, help="Laatste Yahoo-datum, bijvoorbeeld 2026-03-31.")
    p.add_argument(
        "--end-period",
        "--last-period",
        dest="end_period",
        default=None,
        help="Laatste kwartaal voor factors.csv, bijvoorbeeld 2026Q1. Wordt intern vertaald naar --end.",
    )
    p.add_argument("--first-period", default="2015Q1")

    p.add_argument("--market-source", choices=["yahoo", "none"], default="yahoo")
    p.add_argument(
        "--market-factor-mode",
        choices=["excess", "raw"],
        default="excess",
        help=(
            "Hoe Yahoo/ETF-factorproxies worden geschreven. "
            "'excess' trekt rf af van equity/duration/credit/real_estate; "
            "'raw' behoudt totale rendementen. fx en Ken-French worden niet aangepast."
        ),
    )
    p.add_argument("--rf-mode", choices=["xeon", "zero"], default="xeon")
    p.add_argument("--rf-source", choices=["market", "ken_french", "zero"], default="market")
    p.add_argument("--rf-ticker", default=DEFAULT_TICKERS["rf"])
    p.add_argument("--equity", default=DEFAULT_TICKERS["equity"])
    p.add_argument("--duration", default=DEFAULT_TICKERS["duration"])
    p.add_argument("--credit", default=DEFAULT_TICKERS["credit"])
    p.add_argument("--real-estate", default=DEFAULT_TICKERS["real_estate"])
    p.add_argument("--eurusd", default=DEFAULT_TICKERS["eurusd"])

    p.add_argument("--include-ken-french", action="store_true")
    p.add_argument("--ken-french-region", choices=["developed", "europe"], default="europe")
    p.add_argument("--ken-french-model", choices=["3", "5"], default="5")
    p.add_argument("--ken-french-momentum", action="store_true")
    p.add_argument(
        "--ken-french-file",
        type=Path,
        default=None,
        help="Lokale Ken-French CSV/ZIP voor 3- of 5-factor bestand, bv. Europe_5_Factors.csv.",
    )
    p.add_argument(
        "--ken-french-momentum-file",
        type=Path,
        default=None,
        help="Lokale Ken-French CSV/ZIP voor momentum bestand.",
    )

    p.add_argument(
        "--allow-partial-missing",
        action="store_true",
        help="Sta toe dat sommige kwartalen/factoren NaN bevatten.",
    )

    return p.parse_args()


def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    if args.end_period:
        end_from_period = quarter_end_date_from_period(args.end_period)
        if args.end and args.end != end_from_period:
            raise SystemExit(
                f"--end ({args.end}) en --end-period ({args.end_period} -> {end_from_period}) spreken elkaar tegen."
            )
        args.end = end_from_period
        log(f"Factor end period: {args.end_period} -> {args.end}")

    reports = []
    prices_q = pd.DataFrame()
    market_factors = None
    ff_factors = None
    tickers = None

    if args.market_source == "yahoo":
        tickers = {
            "rf": args.rf_ticker,
            "equity": args.equity,
            "duration": args.duration,
            "credit": args.credit,
            "real_estate": args.real_estate,
            "eurusd": args.eurusd,
        }
        market_factors, prices_q, _market_report_raw = build_market_factors(
            tickers=tickers,
            start=args.start,
            end=args.end,
            first_period=args.first_period,
            rf_mode=args.rf_mode,
            output_dir=args.output_dir,
        )

    if args.include_ken_french:
        ff_factors, _, ff_report = build_ken_french_factors(
            region=args.ken_french_region,
            model=args.ken_french_model,
            include_momentum=args.ken_french_momentum,
            first_period=args.first_period,
            output_dir=args.output_dir,
            ken_french_file=args.ken_french_file,
            ken_french_momentum_file=args.ken_french_momentum_file,
        )
        reports.append(ff_report)

    factors = combine_factors(
        market_factors,
        ff_factors,
        rf_source=args.rf_source,
        market_factor_mode=args.market_factor_mode,
    )
    factors = filter_to_end_period(factors, args.end_period)

    if not prices_q.empty:
        prices_q = filter_to_end_period(prices_q, args.end_period)

    if args.rf_source == "zero":
        factors["rf"] = 0.0
        if args.market_factor_mode == "excess":
            # rf is nul, dus excess == raw voor marktproxies. Deze regel houdt de
            # intentie expliciet zonder de factorwaarden te wijzigen.
            pass

    factors = trim_trailing_incomplete_factor_rows(factors)

    if not prices_q.empty:
        keep_periods = set(factors["period"].astype(str)) if "period" in factors.columns else set()
        if keep_periods and "period" in prices_q.columns:
            prices_q = prices_q[prices_q["period"].astype(str).isin(keep_periods)].copy()

    if tickers is not None:
        reports.insert(0, make_market_report(factors, tickers, args.rf_mode, args.market_factor_mode))

    validate_factors(factors, allow_partial_missing=args.allow_partial_missing)

    factors_path = args.output_dir / args.output_file
    prices_path = args.output_dir / "factor_prices_quarterly.csv"
    report_path = args.output_dir / "factor_download_report.csv"

    factors.to_csv(factors_path, index=False)
    prices_q.to_csv(prices_path, index=False)
    report = pd.concat(reports, ignore_index=True) if reports else pd.DataFrame()
    report.to_csv(report_path, index=False)

    print(f"Saved: {factors_path}")
    print(f"Saved: {prices_path}")
    print(f"Saved: {report_path}")
    print("")
    if not report.empty:
        print(report.to_string(index=False))
    print("")
    print("Output columns:")
    print(", ".join(factors.columns))


if __name__ == "__main__":
    main()
