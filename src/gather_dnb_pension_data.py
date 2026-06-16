#!/usr/bin/env python3
"""
gather_dnb_pension_data.py

Download en standaardiseer DNB pensioenfondsdata.

Doel:
- Eén script voor data gathering.
- Output is simpele, stabiele CSV input voor de analyse.
- Geen regressies of rapportage in dit script.

Ondersteunde DNB jaarformaten voor TER:
1. DNB resourcefile/xlsx met één sheet per jaar, zoals:
   2015, 2016, ..., 2024
   met kolommen:
   - Verkorte naam
   - Kosten vermogensbeheer als percentage van het gemiddeld belegd vermogen
   - Transactiekosten als percentage van het gemiddeld belegd vermogen

2. Long-format met kolommen:
   Rapporteur, Post, Periode, waarde

Outputs:
    data/available_return_funds.csv
    data/returns_quarterly.csv
    data/returns_quarterly_wide.csv
    data/ter_annual.csv
    data/ter_breakdown_wide.csv
    data/ter_candidates.csv
    data/flow_diagnostics.csv
    data/annual_xlsx_matching_report.csv
    data/annual_xlsx_missing_summary.csv
    data/dnb_quarterly_raw_download.*
    data/dnb_annual_raw_download.*

CSV-contracten:

1. returns_quarterly.csv
    fund,period,period_original,year,quarter,return_quarterly,dnb_reporter,source_return_post

    return_quarterly is decimaal:
        0.025 = 2.5%

2. ter_annual.csv
    fund,year,asset_management_costs,transaction_costs,ter_annual,
    dnb_reporter,source_asset_management_post,source_transaction_post,

3. flow_diagnostics.csv
    fund,year,total_premium,active_participants,deferred_participants,
    pensioners,total_participants,active_ratio,pensioner_ratio,
    participant_growth,total_premium_growth,premium_per_active,
    source_type,confidence,notes

    Alle kosten zijn decimalen:
        0.0045 = 0.45%

Gebruik:
    pip install pandas numpy requests openpyxl
    python gather_dnb_pension_data.py

Alle fondsen met DNB-risico-fonds-rendementen:
    python gather_dnb_pension_data.py --fund-selection all

Specifieke fondsen uit de DNB-rapporteurs:
    python gather_dnb_pension_data.py --fund-selection all --include-funds "ABP,Zorg en Welzijn,Bouwnijverheid"

Met lokale jaarexcel:
    python gather_dnb_pension_data.py --annual-file dnb_annual_raw_download.xlsx --skip-quarterly

Met eigen API key:
    export DNB_STATPUB_KEY="..."
    python gather_dnb_pension_data.py

Met custom fondsenmapping:
    python gather_dnb_pension_data.py --fund-map fund_map.csv

fund_map.csv:
    fund,dnb_name
    ABP,ABP
    PFZW,Zorg en Welzijn
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import warnings
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
import requests


QUARTERLY_RESOURCE_ID = "a4b6584f-09b7-498d-bce5-3ef12e966f87"
ANNUAL_RESOURCE_ID = "a4af8408-51a6-40c6-bcd5-6bc6c28a79b5"

STATPUB_RESOURCE_URL = (
    "https://api.dnb.nl/statpub-intapi-prd/v1/resources/{resource_id}/resourcefile/{fmt}"
)

DEFAULT_DNB_CONFIG_PATH = Path("config/dnb_resources.json")
DEFAULT_DNB_STATPUB_KEY = None

RETURN_POST = "Kwartaalrendement beleggingen risico fonds (in percentages)"

DEFAULT_FUND_MAP = {
    "ABP": "ABP",
    "PFZW": "Zorg en Welzijn",
    "PMT": "Metaal en Techniek",
    "PME": "Metalektro, bedrijfstakpensioenfonds",
    "bpfBOUW": "Bouwnijverheid",
    "ING": "ING",
    "Rabobank": "Rabobankorganisatie",
    "ABN AMRO": "ABN AMRO Bank",
}

ANNUAL_MATCH_RECORDS: list[dict[str, Any]] = []


def log(message: str) -> None:
    print(message, file=sys.stderr)


def load_dnb_config(path: Path | None = None) -> dict[str, Any]:
    """
    Load tracked DNB resource/API configuration.

    Resource IDs are safe to keep in git. A subscription/API key should normally
    be supplied through the DNB_STATPUB_KEY environment variable or GitHub Secret,
    not committed to the repository.
    """
    config_path = path or DEFAULT_DNB_CONFIG_PATH
    if not config_path.exists():
        return {
            "api": {
                "resourcefile_url": STATPUB_RESOURCE_URL,
                "subscription_key_env": "DNB_STATPUB_KEY",
                "subscription_key": DEFAULT_DNB_STATPUB_KEY,
                "headers": {
                    "origin": "https://www.dnb.nl",
                    "referer": "https://www.dnb.nl/statistieken/data-zoeken/",
                    "user_agent": "Mozilla/5.0",
                },
            },
            "resources": {
                "quarterly_individual_pension_funds": {"resource_id": QUARTERLY_RESOURCE_ID},
                "annual_individual_pension_funds": {"resource_id": ANNUAL_RESOURCE_ID},
            },
        }

    with config_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def dnb_api_key_from_config(config: dict[str, Any], cli_key: str | None = None) -> str | None:
    api = config.get("api", {})
    env_name = api.get("subscription_key_env", "DNB_STATPUB_KEY")
    return cli_key or os.getenv(str(env_name)) or api.get("subscription_key")


def dnb_resource_ids_from_config(config: dict[str, Any]) -> tuple[str, str]:
    resources = config.get("resources", {})
    quarterly = resources.get("quarterly_individual_pension_funds", {}).get("resource_id") or QUARTERLY_RESOURCE_ID
    annual = resources.get("annual_individual_pension_funds", {}).get("resource_id") or ANNUAL_RESOURCE_ID
    return str(quarterly), str(annual)


def dnb_resource_url_from_config(config: dict[str, Any]) -> str:
    return str(config.get("api", {}).get("resourcefile_url") or STATPUB_RESOURCE_URL)


def make_dnb_headers(api_key: str | None = None, accept: str = "*/*", config: dict[str, Any] | None = None) -> dict[str, str]:
    config = config or load_dnb_config(None)
    api = config.get("api", {})
    configured_headers = api.get("headers", {})
    key = dnb_api_key_from_config(config, cli_key=api_key)

    headers = {
        "accept": accept,
        "origin": configured_headers.get("origin", "https://www.dnb.nl"),
        "referer": configured_headers.get("referer", "https://www.dnb.nl/statistieken/data-zoeken/"),
        "user-agent": configured_headers.get("user_agent", "Mozilla/5.0"),
    }
    if key:
        headers["ocp-apim-subscription-key"] = key
    return headers


def request_dnb_resourcefile(
    resource_id: str,
    fmt: Literal["csv", "json", "xlsx"],
    api_key: str | None = None,
    config: dict[str, Any] | None = None,
) -> requests.Response:
    config = config or load_dnb_config(None)
    url = dnb_resource_url_from_config(config).format(resource_id=resource_id, fmt=fmt)
    accept = {
        "csv": "text/csv,*/*",
        "json": "application/json, text/javascript, */*; q=0.01",
        "xlsx": "*/*",
    }[fmt]

    response = requests.get(
        url,
        headers=make_dnb_headers(api_key, accept=accept, config=config),
        timeout=240,
    )
    log(
        f"DNB resource={resource_id} fmt={fmt}: "
        f"HTTP {response.status_code}, "
        f"content-type={response.headers.get('content-type')!r}, "
        f"bytes={len(response.content):,}"
    )

    if response.status_code in (401, 403):
        raise RuntimeError(
            f"DNB endpoint gaf {response.status_code}. "
            "Zet DNB_STATPUB_KEY met de key uit de browserrequest."
        )

    response.raise_for_status()
    return response


def looks_like_html(content: bytes) -> bool:
    head = content[:1000].lower()
    return b"<html" in head or b"<!doctype" in head


def looks_like_dnb_csv(content: bytes) -> bool:
    head = content[:3000].lower()
    return (
        b"rapporteur" in head
        and b"periode" in head
        and (b"waarde" in head or b"value" in head)
    )



def read_excel_sheets_quietly(path: Path) -> dict[str, pd.DataFrame]:
    """
    Lees DNB Excel-sheets zonder de onschuldige openpyxl header/footer-warning.

    De DNB xlsx bevat soms header/footer XML die openpyxl niet kan parsen.
    Dat beïnvloedt de celdata niet die wij gebruiken; alleen print-layout metadata
    wordt genegeerd.
    """
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"Cannot parse header or footer so it will be ignored",
            category=UserWarning,
        )
        warnings.filterwarnings(
            "ignore",
            message=r".*Cannot parse header or footer.*",
            category=UserWarning,
        )
        return pd.read_excel(path, sheet_name=None, header=None)


def record_annual_match(
    *,
    year: int,
    fund: str,
    dnb_name: str,
    context: str,
    status: str,
    matched_reporter: str | None = None,
    n_matches: int = 0,
    notes: str = "",
) -> None:
    ANNUAL_MATCH_RECORDS.append({
        "year": year,
        "fund": fund,
        "dnb_name": dnb_name,
        "context": context,
        "status": status,
        "matched_reporter": matched_reporter or "",
        "n_matches": n_matches,
        "notes": notes,
    })


def write_annual_matching_report(output_dir: Path) -> None:
    if not ANNUAL_MATCH_RECORDS:
        return

    report = pd.DataFrame(ANNUAL_MATCH_RECORDS).drop_duplicates()
    report = report.sort_values(["context", "status", "year", "fund"]).reset_index(drop=True)

    path = output_dir / "annual_xlsx_matching_report.csv"
    report.to_csv(path, index=False)

    missing = report[report["status"].eq("missing")]
    ambiguous = report[report["n_matches"].gt(1)]
    print(
        f"Saved: {path} "
        f"({len(missing)} missing rows, {len(ambiguous)} ambiguous/multiple-match rows)"
    )

    if not missing.empty:
        summary = (
            missing.groupby(["context", "year"])["fund"]
            .nunique()
            .reset_index(name="n_missing_funds")
            .sort_values(["context", "year"])
        )
        summary_path = output_dir / "annual_xlsx_missing_summary.csv"
        summary.to_csv(summary_path, index=False)
        print(f"Saved: {summary_path}")


def find_fund_subset(
    data: pd.DataFrame,
    name_col: str,
    fund: str,
    dnb_name: str,
    year: int,
    context: str,
) -> pd.DataFrame:
    exact = data[name_col].astype(str).str.strip().eq(dnb_name)
    subset = data.loc[exact].copy()

    if subset.empty:
        contains = data[name_col].astype(str).str.contains(
            re.escape(dnb_name),
            case=False,
            na=False,
        )
        subset = data.loc[contains].copy()

    if subset.empty:
        record_annual_match(
            year=year,
            fund=fund,
            dnb_name=dnb_name,
            context=context,
            status="missing",
            notes="Fondsnaam uit kwartaaldata/fund_map niet gevonden in DNB jaar-XLSX sheet.",
        )
    else:
        record_annual_match(
            year=year,
            fund=fund,
            dnb_name=dnb_name,
            context=context,
            status="matched",
            matched_reporter=str(subset.iloc[0][name_col]).strip(),
            n_matches=len(subset),
            notes="Exacte of contains-match gebruikt; check n_matches > 1.",
        )

    return subset


def find_records(obj: Any) -> list[dict[str, Any]]:
    if isinstance(obj, list):
        if not obj:
            return []
        if isinstance(obj[0], dict):
            return obj
        return [{"value": x} for x in obj]

    if isinstance(obj, dict):
        for key in ["value", "data", "items", "rows", "records", "observations", "resourcefile"]:
            value = obj.get(key)
            if isinstance(value, list) and (not value or isinstance(value[0], dict)):
                return value

        candidates: list[list[dict[str, Any]]] = []
        stack = list(obj.values())
        while stack:
            value = stack.pop()
            if isinstance(value, list) and value and isinstance(value[0], dict):
                candidates.append(value)
            elif isinstance(value, dict):
                stack.extend(value.values())

        if candidates:
            return max(candidates, key=len)

    raise ValueError(
        "Kon geen records vinden in JSON. "
        f"type={type(obj).__name__}, "
        f"keys={list(obj.keys())[:30] if isinstance(obj, dict) else 'n/a'}"
    )


def download_dnb_dataset(
    resource_id: str,
    output_dir: Path,
    stem: str,
    prefer_format: Literal["csv", "json", "xlsx"],
    api_key: str | None = None,
    config: dict[str, Any] | None = None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)

    if prefer_format == "csv":
        formats: list[Literal["csv", "json", "xlsx"]] = ["csv", "json", "xlsx"]
    elif prefer_format == "xlsx":
        formats = ["xlsx", "csv", "json"]
    else:
        formats = ["json", "csv", "xlsx"]

    errors = []

    for fmt in formats:
        try:
            response = request_dnb_resourcefile(resource_id, fmt, api_key=api_key, config=config)
            content = response.content

            if looks_like_html(content):
                raise RuntimeError(f"{fmt} endpoint gaf HTML terug.")

            if fmt == "csv":
                if not looks_like_dnb_csv(content):
                    raise RuntimeError("CSV endpoint gaf geen herkenbare DNB CSV.")
                path = output_dir / f"{stem}.csv"
                path.write_bytes(content)
                return path

            if fmt == "xlsx":
                if not content.startswith(b"PK"):
                    raise RuntimeError("XLSX endpoint gaf geen XLSX/ZIP bytes.")
                path = output_dir / f"{stem}.xlsx"
                path.write_bytes(content)
                return path

            payload = json.loads(content.decode("utf-8-sig"))
            records = find_records(payload)
            if not records:
                raise RuntimeError("JSON bevatte geen records.")

            raw_path = output_dir / f"{stem}.raw.json"
            raw_path.write_bytes(content)

            df = pd.json_normalize(records)
            csv_path = output_dir / f"{stem}.csv"
            df.to_csv(csv_path, index=False)
            return csv_path

        except Exception as exc:
            errors.append(f"{fmt}: {exc}")
            log(f"Download poging {fmt} faalde: {exc}")

    raise RuntimeError("Alle downloadformaten faalden:\n" + "\n".join(errors))


def read_table_file(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()

    if suffix in [".xlsx", ".xls"]:
        sheets = read_excel_sheets_quietly(path)
        return next(iter(sheets.values()))

    if suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        return pd.json_normalize(find_records(payload))

    try:
        return pd.read_csv(path, sep=None, engine="python", encoding="utf-8-sig")
    except UnicodeDecodeError:
        return pd.read_csv(path, sep=None, engine="python", encoding="latin1")


def normalize_dnb_columns(df: pd.DataFrame) -> pd.DataFrame:
    original_cols = list(df.columns)

    lookup: dict[str, str] = {}
    for col in df.columns:
        key = str(col).strip().lower()
        key = key.replace("_", " ").replace("-", " ")
        key = re.sub(r"\s+", " ", key)
        lookup[key] = col

    candidates = {
        "Rapporteur": ["rapporteur", "reporter", "reporter name", "reportername", "institution", "instelling"],
        "Post": ["post", "item", "variable", "variabele", "measure", "statistic", "onderwerp", "gegeven"],
        "Periode": ["periode", "period", "time period", "timeperiod", "quarter", "kwartaal", "jaar", "year"],
        "waarde": ["waarde", "value", "obs value", "obsvalue", "observation"],
    }

    rename = {}
    for target, keys in candidates.items():
        if target in df.columns:
            continue
        for key in keys:
            if key in lookup:
                rename[lookup[key]] = target
                break

    df = df.rename(columns=rename)

    required = {"Rapporteur", "Post", "Periode", "waarde"}
    missing = sorted(required - set(df.columns))
    if missing:
        raise ValueError(
            f"DNB data mist kolommen na normalisatie: {missing}. "
            f"Originele kolommen: {original_cols[:100]}"
        )

    return df


def parse_numeric(value: Any) -> float:
    if pd.isna(value):
        return np.nan

    if isinstance(value, str):
        v = value.strip().replace("%", "")
        if re.match(r"^-?\d{1,3}(\.\d{3})+,\d+$", v):
            v = v.replace(".", "")
        v = v.replace(",", ".")
        return float(pd.to_numeric(v, errors="coerce"))

    return float(pd.to_numeric(value, errors="coerce"))


def parse_percentage_to_decimal(value: Any) -> float:
    number = parse_numeric(value)
    if pd.isna(number):
        return np.nan
    return number / 100.0


def parse_rate_to_decimal(value: Any) -> float:
    """
    Voor Excel-percentages zoals 0.0045 = 0.45%.
    Als de bron toch 0.45 of 45 als percentage geeft, wordt >1 gedeeld door 100.
    """
    number = parse_numeric(value)
    if pd.isna(number):
        return np.nan
    if abs(number) > 1:
        return number / 100.0
    return number


def parse_year(period: Any) -> int | None:
    if pd.isna(period):
        return None
    match = re.search(r"(20\d{2}|19\d{2})", str(period))
    return int(match.group(1)) if match else None


def normalize_period_label(period: Any) -> str | None:
    """
    Normaliseer DNB-kwartalen naar 2015Q1.

    Ondersteunt o.a.:
    - 2015K1
    - 2015Q1
    - 2015-K1
    - 2015 Q1
    """
    if pd.isna(period):
        return None

    text = str(period).strip().upper()
    match = re.search(r"(19\d{2}|20\d{2})\s*[- ]?\s*[KQ]\s*([1-4])", text)
    if match:
        return f"{match.group(1)}Q{match.group(2)}"

    return None


def parse_quarter(period: Any) -> int | None:
    norm = normalize_period_label(period)
    if norm is None:
        return None
    return int(norm[-1])


def parse_fund_map(path: Path | None) -> dict[str, str]:
    if path is None:
        return DEFAULT_FUND_MAP

    df = pd.read_csv(path, sep=None, engine="python")
    required = {"fund", "dnb_name"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"fund_map mist kolommen: {sorted(missing)}. Verwacht fund,dnb_name")

    return dict(zip(df["fund"].astype(str), df["dnb_name"].astype(str)))


def select_reporter_rows(df: pd.DataFrame, dnb_name: str) -> pd.DataFrame:
    exact = df["Rapporteur"].astype(str).str.strip().eq(dnb_name)
    subset = df.loc[exact].copy()

    if subset.empty:
        contains = df["Rapporteur"].astype(str).str.contains(
            re.escape(dnb_name),
            case=False,
            na=False,
        )
        subset = df.loc[contains].copy()

    return subset


def select_return_rows(df: pd.DataFrame) -> pd.DataFrame:
    post = df["Post"].astype(str).str.strip()
    mask = post.eq(RETURN_POST)

    if not mask.any():
        lower = post.str.lower()
        mask = (
            lower.str.contains("kwartaalrendement", na=False)
            & lower.str.contains("beleggingen", na=False)
            & lower.str.contains("risico fonds", na=False)
        )

    return_rows = df.loc[mask, ["Rapporteur", "Post", "Periode", "waarde"]].copy()
    if return_rows.empty:
        available = df["Post"].dropna().astype(str).drop_duplicates().head(80).tolist()
        raise RuntimeError(
            "Geen kwartaalrendementspost gevonden. Beschikbare Post-waarden:\n"
            + "\n".join(available)
        )

    return return_rows


def make_available_return_funds(return_rows: pd.DataFrame) -> pd.DataFrame:
    tmp = return_rows.copy()
    tmp["period_normalized"] = tmp["Periode"].astype(str).map(normalize_period_label)
    tmp["return_quarterly"] = tmp["waarde"].map(parse_percentage_to_decimal)

    available = (
        tmp.dropna(subset=["return_quarterly"])
        .groupby("Rapporteur", dropna=False)
        .agg(
            n_obs=("return_quarterly", "size"),
            first_period=("period_normalized", "min"),
            last_period=("period_normalized", "max"),
            source_return_post=("Post", "first"),
        )
        .reset_index()
        .rename(columns={"Rapporteur": "dnb_reporter"})
        .sort_values(["dnb_reporter"])
        .reset_index(drop=True)
    )
    available["fund"] = available["dnb_reporter"]
    available = available[["fund", "dnb_reporter", "n_obs", "first_period", "last_period", "source_return_post"]]
    return available


def parse_include_funds(value: str | None) -> list[str] | None:
    if value is None or not str(value).strip():
        return None
    return [x.strip() for x in str(value).split(",") if x.strip()]


def build_all_fund_map_from_available(
    available_funds: pd.DataFrame,
    include_funds: list[str] | None = None,
) -> dict[str, str]:
    df = available_funds.copy()
    if include_funds:
        wanted = {x.casefold() for x in include_funds}
        df = df[
            df["fund"].astype(str).str.casefold().isin(wanted)
            | df["dnb_reporter"].astype(str).str.casefold().isin(wanted)
        ].copy()

        missing = sorted(wanted - set(df["fund"].astype(str).str.casefold()) - set(df["dnb_reporter"].astype(str).str.casefold()))
        if missing:
            log("Waarschuwing: sommige --include-funds zijn niet gevonden: " + ", ".join(missing))

    return dict(zip(df["fund"].astype(str), df["dnb_reporter"].astype(str)))


def build_returns_quarterly(
    quarterly_df: pd.DataFrame,
    fund_map: dict[str, str],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = normalize_dnb_columns(quarterly_df.copy())
    return_rows = select_return_rows(df)

    frames = []
    for fund, dnb_name in fund_map.items():
        subset = select_reporter_rows(return_rows, dnb_name)
        if subset.empty:
            log(f"Waarschuwing: fonds niet gevonden in kwartaaldata: {fund} -> {dnb_name}")
            continue

        subset = subset.rename(
            columns={
                "Rapporteur": "dnb_reporter",
                "Post": "source_return_post",
                "Periode": "period",
            }
        )
        subset["fund"] = fund
        subset["period_original"] = subset["period"].astype(str)
        subset["period"] = subset["period_original"].map(normalize_period_label)
        subset["year"] = subset["period"].map(parse_year)
        subset["quarter"] = subset["period"].map(parse_quarter)
        subset["return_quarterly"] = subset["waarde"].map(parse_percentage_to_decimal)

        bad_periods = subset.loc[subset["period"].isna(), "period_original"].drop_duplicates().tolist()
        if bad_periods:
            raise RuntimeError(
                f"Kon kwartaalperiode niet normaliseren voor {fund}: {bad_periods[:10]}"
            )

        subset = subset[
            [
                "fund",
                "period",
                "period_original",
                "year",
                "quarter",
                "return_quarterly",
                "dnb_reporter",
                "source_return_post",
            ]
        ]
        frames.append(subset)

    if not frames:
        raise RuntimeError("Geen geselecteerde fondsen gevonden in kwartaaldata.")

    long = pd.concat(frames, ignore_index=True)
    long = long.dropna(subset=["return_quarterly"]).sort_values(["fund", "period"]).reset_index(drop=True)

    wide = (
        long.pivot_table(index="period", columns="fund", values="return_quarterly", aggfunc="first")
        .sort_index()
        .reset_index()
    )

    ordered_funds = [fund for fund in fund_map.keys() if fund in wide.columns]
    wide = wide[["period"] + ordered_funds]

    return long, wide


def classify_cost_post(post: str) -> str | None:
    p = re.sub(r"\s+", " ", str(post).lower())

    is_percent = any(x in p for x in ["percentage", "procent", "percent", "%"])
    has_avg_assets = any(
        x in p
        for x in ["gemiddeld belegd vermogen", "gemiddelde beleggingen", "belegd vermogen"]
    )

    if "pensioenbeheer" in p:
        return None

    if ("transactiekosten" in p or "transactie kosten" in p) and (is_percent or has_avg_assets):
        return "transaction_costs"

    if "vermogensbeheer" in p and "transactie" not in p and (is_percent or has_avg_assets):
        return "asset_management_costs"

    return None


def build_ter_from_annual_wide_xlsx(
    annual_xlsx_path: Path,
    fund_map: dict[str, str],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Extract TER uit de DNB jaar-XLSX met één sheet per jaar.
    De kostenkolommen staan al als Excel-ratio's:
        0.0044 = 0.44%
    """
    sheets = read_excel_sheets_quietly(annual_xlsx_path)

    ter_records = []
    candidate_records = []

    for sheet_name, raw in sheets.items():
        if not re.fullmatch(r"\d{4}", str(sheet_name)):
            continue

        year = int(sheet_name)

        header_idx = None
        for idx in raw.index:
            first = raw.iat[idx, 0] if raw.shape[1] > 0 else None
            if isinstance(first, str) and first.strip() == "Verkorte naam":
                header_idx = idx
                break

        if header_idx is None:
            continue

        headers = [
            str(x).strip() if not pd.isna(x) else f"unnamed_{i}"
            for i, x in enumerate(raw.iloc[header_idx].tolist())
        ]

        data = raw.iloc[header_idx + 1 :].copy()
        data.columns = headers

        if "Verkorte naam" not in data.columns:
            continue

        am_cols = [
            c for c in data.columns
            if "Kosten vermogensbeheer als percentage" in str(c)
        ]
        trx_cols = [
            c for c in data.columns
            if "Transactiekosten als percentage" in str(c)
        ]

        if not am_cols:
            continue

        asset_col = am_cols[0]
        trx_col = trx_cols[0] if trx_cols else None

        for fund, dnb_name in fund_map.items():
            subset = find_fund_subset(
                data=data,
                name_col="Verkorte naam",
                fund=fund,
                dnb_name=dnb_name,
                year=year,
                context="ter",
            )

            if subset.empty:
                continue

            # Als contains meerdere regels oplevert, kies exacte of eerste en verlaag confidence.
            row = subset.iloc[0]
            reporter = str(row["Verkorte naam"]).strip()

            asset_value = parse_rate_to_decimal(row[asset_col])
            trx_value = parse_rate_to_decimal(row[trx_col]) if trx_col else np.nan

            if pd.isna(asset_value):
                continue

            trx_used = 0.0 if pd.isna(trx_value) else trx_value
            confidence = 0.95 if trx_col and not pd.isna(trx_value) and len(subset) == 1 else 0.70

            candidate_records.append({
                "fund": fund,
                "year": year,
                "dnb_reporter": reporter,
                "cost_metric": "asset_management_costs",
                "source_post": asset_col,
                "value_decimal": asset_value,
            })
            if trx_col:
                candidate_records.append({
                    "fund": fund,
                    "year": year,
                    "dnb_reporter": reporter,
                    "cost_metric": "transaction_costs",
                    "source_post": trx_col,
                    "value_decimal": trx_value,
                })

            ter_records.append({
                "fund": fund,
                "year": year,
                "asset_management_costs": asset_value,
                "transaction_costs": trx_used,
                "ter_annual": asset_value + trx_used,
                "dnb_reporter": reporter,
                "source_asset_management_post": asset_col,
                "source_transaction_post": trx_col or "",
                "source_type": "dnb_annual_wide_xlsx",
                "confidence": confidence,
                "notes": (
                    "Extracted from DNB annual wide Excel."
                    if confidence >= 0.95
                    else "Extracted from DNB annual wide Excel; check match/transaction costs."
                ),
            })

    ter = pd.DataFrame(ter_records)
    candidates = pd.DataFrame(candidate_records)

    if not ter.empty:
        ter = ter.sort_values(["fund", "year"]).reset_index(drop=True)

    if not candidates.empty:
        candidates = candidates.sort_values(["fund", "year", "cost_metric"]).reset_index(drop=True)

    return ter, candidates


def build_ter_from_annual_long(
    annual_df: pd.DataFrame,
    fund_map: dict[str, str],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = normalize_dnb_columns(annual_df.copy())

    df["cost_metric"] = df["Post"].astype(str).map(classify_cost_post)

    broad_mask = df["Post"].astype(str).str.lower().str.contains(
        "kosten|vermogensbeheer|transactie",
        regex=True,
        na=False,
    )

    candidates = df.loc[
        broad_mask,
        ["Rapporteur", "Post", "Periode", "waarde", "cost_metric"],
    ].copy()

    candidate_frames = []
    for fund, dnb_name in fund_map.items():
        subset = select_reporter_rows(candidates, dnb_name)
        if subset.empty:
            log(f"Waarschuwing: fonds niet gevonden in jaarkosten: {fund} -> {dnb_name}")
            continue

        subset = subset.rename(
            columns={
                "Rapporteur": "dnb_reporter",
                "Post": "source_post",
                "Periode": "period",
            }
        )
        subset["fund"] = fund
        subset["year"] = subset["period"].map(parse_year)
        subset["value_decimal"] = subset["waarde"].map(parse_rate_to_decimal)
        candidate_frames.append(subset)

    if not candidate_frames:
        return pd.DataFrame(), candidates

    cand = pd.concat(candidate_frames, ignore_index=True)
    cand = cand.sort_values(["fund", "year", "cost_metric", "source_post"]).reset_index(drop=True)

    classified = cand.dropna(subset=["cost_metric", "year", "value_decimal"]).copy()

    if classified.empty:
        return pd.DataFrame(), cand

    counts = (
        classified.groupby(["fund", "year", "cost_metric"])["source_post"]
        .nunique()
        .reset_index(name="n_unique_posts")
    )
    ambiguous = counts[counts["n_unique_posts"] > 1]
    if not ambiguous.empty:
        log("Ambigue TER-kostenposten gevonden; ter_annual.csv wordt niet automatisch gevuld.")
        return pd.DataFrame(), cand

    asset = classified[classified["cost_metric"].eq("asset_management_costs")].copy()
    trx = classified[classified["cost_metric"].eq("transaction_costs")].copy()

    asset = asset.rename(
        columns={
            "value_decimal": "asset_management_costs",
            "source_post": "source_asset_management_post",
            "dnb_reporter": "dnb_reporter_asset",
        }
    )
    trx = trx.rename(
        columns={
            "value_decimal": "transaction_costs",
            "source_post": "source_transaction_post",
            "dnb_reporter": "dnb_reporter_transaction",
        }
    )

    ter = asset[
        [
            "fund",
            "year",
            "asset_management_costs",
            "dnb_reporter_asset",
            "source_asset_management_post",
        ]
    ].merge(
        trx[
            [
                "fund",
                "year",
                "transaction_costs",
                "dnb_reporter_transaction",
                "source_transaction_post",
            ]
        ],
        on=["fund", "year"],
        how="left",
    )

    ter["transaction_costs"] = ter["transaction_costs"].fillna(0.0)
    ter["source_transaction_post"] = ter["source_transaction_post"].fillna("")
    ter["dnb_reporter"] = ter["dnb_reporter_asset"].fillna(ter.get("dnb_reporter_transaction"))
    ter["ter_annual"] = ter["asset_management_costs"] + ter["transaction_costs"]
    ter["source_type"] = "dnb_annual_long"
    ter["confidence"] = np.where(ter["source_transaction_post"].eq(""), 0.70, 0.95)
    ter["notes"] = np.where(
        ter["source_transaction_post"].eq(""),
        "Vermogensbeheerkosten gevonden; transactiekosten niet gevonden en op 0 gezet.",
        "Vermogensbeheerkosten + transactiekosten uit DNB jaarset.",
    )

    ter = ter[
        [
            "fund",
            "year",
            "asset_management_costs",
            "transaction_costs",
            "ter_annual",
            "dnb_reporter",
            "source_asset_management_post",
            "source_transaction_post",
            "source_type",
            "confidence",
            "notes",
        ]
    ].sort_values(["fund", "year"]).reset_index(drop=True)

    return ter, cand




def to_number(value: Any) -> float:
    """Robuuste numerieke parsing voor DNB Excel-cellen."""
    if pd.isna(value):
        return np.nan
    if isinstance(value, (int, float, np.integer, np.floating)):
        return float(value)
    text = str(value).strip()
    if text in {"", ".", "-", "nan", "NaN"}:
        return np.nan
    text = text.replace("\u00a0", "").replace(" ", "")
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return np.nan


def build_flow_diagnostics_from_annual_wide_xlsx(
    annual_xlsx_path: Path,
    fund_map: dict[str, str],
) -> pd.DataFrame:
    """
    Extract flow-/deelnemersdiagnostics uit de DNB jaar-XLSX met één sheet per jaar.

    Let op:
    - Bedragen in de DNB jaar-XLSX zijn in duizenden euro's.
    - Dit is géén beleggingsrendementscorrectie en géén echte cashflow-attributie.
    - Zonder belegd vermogen / uitkeringen / waardeoverdrachten berekenen we geen net-flow/AUM.
    """
    sheets = read_excel_sheets_quietly(annual_xlsx_path)

    col_map = {
        "premium_employee": "Premie werknemer",
        "premium_employer": "Premie werkgever",
        "total_premium": "Totale feitelijke premie",
        "active_participants": "Aantal deelnemers",
        "deferred_participants": "Aantal gewezen deelnemers",
        "pensioners": "Aantal pensioengerechtigden",
        "total_participants": "Totale aantal deelnemers",
    }

    records = []

    for sheet_name, raw in sheets.items():
        if not re.fullmatch(r"\d{4}", str(sheet_name)):
            continue

        year = int(sheet_name)

        header_idx = None
        for idx in raw.index:
            first = raw.iat[idx, 0] if raw.shape[1] > 0 else None
            if isinstance(first, str) and first.strip() == "Verkorte naam":
                header_idx = idx
                break

        if header_idx is None:
            continue

        headers = [
            str(x).strip() if not pd.isna(x) else f"unnamed_{i}"
            for i, x in enumerate(raw.iloc[header_idx].tolist())
        ]
        data = raw.iloc[header_idx + 1 :].copy()
        data.columns = headers

        if "Verkorte naam" not in data.columns:
            continue

        for fund, dnb_name in fund_map.items():
            subset = find_fund_subset(
                data=data,
                name_col="Verkorte naam",
                fund=fund,
                dnb_name=dnb_name,
                year=year,
                context="flow",
            )

            if subset.empty:
                continue

            row = subset.iloc[0]
            record = {
                "fund": fund,
                "year": year,
                "dnb_reporter": str(row["Verkorte naam"]).strip(),
                "source_type": "dnb_annual_wide_xlsx",
                "notes": "Bedragen in duizenden euro's; deelnemersaantallen zijn personen. Geen net-flow/AUM-correctie.",
            }
            for out_col, source_col in col_map.items():
                record[out_col] = to_number(row[source_col]) if source_col in data.columns else np.nan
            records.append(record)

    flow = pd.DataFrame(records)
    if flow.empty:
        return flow

    # Ratios / diagnostics
    for col in [
        "premium_employee", "premium_employer", "total_premium",
        "active_participants", "deferred_participants", "pensioners", "total_participants",
    ]:
        flow[col] = pd.to_numeric(flow[col], errors="coerce")

    total = flow["total_participants"].replace(0, np.nan)
    active = flow["active_participants"].replace(0, np.nan)
    flow["active_ratio"] = flow["active_participants"] / total
    flow["deferred_ratio"] = flow["deferred_participants"] / total
    flow["pensioner_ratio"] = flow["pensioners"] / total
    flow["dependency_ratio_pensioners_to_active"] = flow["pensioners"] / active
    flow["premium_per_active_participant_thousand_eur"] = flow["total_premium"] / active
    flow["premium_per_total_participant_thousand_eur"] = flow["total_premium"] / total

    flow = flow.sort_values(["fund", "year"]).reset_index(drop=True)
    for value_col, out_col in [
        ("total_participants", "participant_growth"),
        ("active_participants", "active_participant_growth"),
        ("total_premium", "total_premium_growth"),
    ]:
        flow[out_col] = flow.groupby("fund")[value_col].pct_change()

    ordered = [
        "fund", "year", "dnb_reporter",
        "premium_employee", "premium_employer", "total_premium",
        "active_participants", "deferred_participants", "pensioners", "total_participants",
        "active_ratio", "deferred_ratio", "pensioner_ratio", "dependency_ratio_pensioners_to_active",
        "participant_growth", "active_participant_growth", "total_premium_growth",
        "premium_per_active_participant_thousand_eur", "premium_per_total_participant_thousand_eur",
        "source_type", "notes",
    ]
    return flow[[c for c in ordered if c in flow.columns]]


def build_ter_from_annual_file(
    annual_path: Path,
    fund_map: dict[str, str],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    suffix = annual_path.suffix.lower()

    if suffix in [".xlsx", ".xls"]:
        ter, candidates = build_ter_from_annual_wide_xlsx(annual_path, fund_map)
        if not ter.empty:
            return ter, candidates

    annual_df = read_table_file(annual_path)
    return build_ter_from_annual_long(annual_df, fund_map)


def make_ter_wide(ter: pd.DataFrame) -> pd.DataFrame:
    if ter.empty:
        return pd.DataFrame()

    value_cols = ["asset_management_costs", "transaction_costs", "ter_annual"]
    parts = []

    for col in value_cols:
        part = ter.pivot_table(index="year", columns="fund", values=col, aggfunc="first")
        part.columns = [f"{fund}__{col}" for fund in part.columns]
        parts.append(part)

    return pd.concat(parts, axis=1).reset_index().sort_values("year")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download en standaardiseer DNB pensioenfondsdata."
    )
    parser.add_argument("--output-dir", type=Path, default=Path("data"))
    parser.add_argument(
        "--dnb-config",
        type=Path,
        default=DEFAULT_DNB_CONFIG_PATH,
        help="Tracked DNB resource/API config JSON. Changing this file should trigger a full DNB refresh.",
    )
    parser.add_argument("--dnb-key", default=os.getenv("DNB_STATPUB_KEY"))
    parser.add_argument("--fund-map", type=Path, default=None)
    parser.add_argument(
        "--fund-selection",
        choices=["default", "all"],
        default="default",
        help="default = vaste 8 fondsenmapping; all = alle DNB-rapporteurs met risico-fonds-rendementen.",
    )
    parser.add_argument(
        "--include-funds",
        default=None,
        help="Komma-gescheiden selectie bij --fund-selection all, op fund/dnb_reporter naam.",
    )
    parser.add_argument("--quarterly-file", type=Path, default=None)
    parser.add_argument("--annual-file", type=Path, default=None)
    parser.add_argument("--quarterly-format", choices=["csv", "json", "xlsx"], default="csv")
    parser.add_argument("--annual-format", choices=["csv", "json", "xlsx"], default="xlsx")
    parser.add_argument("--skip-quarterly", action="store_true", help="Kwartaaldata overslaan; alleen TER uit jaarset maken.")
    parser.add_argument("--skip-annual", action="store_true", help="Download/extract TER uit jaarset overslaan.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    dnb_config = load_dnb_config(args.dnb_config)
    quarterly_resource_id, annual_resource_id = dnb_resource_ids_from_config(dnb_config)
    log(f"Using DNB config: {args.dnb_config}")
    log(f"Quarterly DNB resource ID: {quarterly_resource_id}")
    log(f"Annual DNB resource ID: {annual_resource_id}")


    fund_map = parse_fund_map(args.fund_map)

    if args.fund_selection == "all" and args.fund_map is not None:
        log("Let op: --fund-selection all genegeerd omdat --fund-map is opgegeven.")

    if not args.skip_quarterly:
        if args.quarterly_file:
            quarterly_path = args.quarterly_file
            log(f"Gebruik lokale kwartaalfile: {quarterly_path}")
        else:
            quarterly_path = download_dnb_dataset(
                resource_id=quarterly_resource_id,
                output_dir=args.output_dir,
                stem="dnb_quarterly_raw_download",
                prefer_format=args.quarterly_format,
                api_key=args.dnb_key,
                config=dnb_config,
            )

        quarterly_df = read_table_file(quarterly_path)
        normalized_quarterly_df = normalize_dnb_columns(quarterly_df.copy())
        return_rows = select_return_rows(normalized_quarterly_df)
        available_funds = make_available_return_funds(return_rows)

        available_funds_path = args.output_dir / "available_return_funds.csv"
        available_funds.to_csv(available_funds_path, index=False)
        print(f"Saved: {available_funds_path}")

        if args.fund_selection == "all" and args.fund_map is None:
            fund_map = build_all_fund_map_from_available(
                available_funds,
                include_funds=parse_include_funds(args.include_funds),
            )
            print(f"Selected funds from DNB quarterly data: {len(fund_map)}")

        returns_long, returns_wide = build_returns_quarterly(quarterly_df, fund_map=fund_map)

        returns_long_path = args.output_dir / "returns_quarterly.csv"
        returns_wide_path = args.output_dir / "returns_quarterly_wide.csv"
        returns_long.to_csv(returns_long_path, index=False)
        returns_wide.to_csv(returns_wide_path, index=False)

        print(f"Saved: {returns_long_path}")
        print(f"Saved: {returns_wide_path}")

    if args.skip_annual:
        return

    if args.annual_file:
        annual_path = args.annual_file
        log(f"Gebruik lokale jaarfile: {annual_path}")
    else:
        annual_path = download_dnb_dataset(
            resource_id=annual_resource_id,
            output_dir=args.output_dir,
            stem="dnb_annual_raw_download",
            prefer_format=args.annual_format,
            api_key=args.dnb_key,
            config=dnb_config,
        )

    ter, candidates = build_ter_from_annual_file(annual_path, fund_map=fund_map)

    candidates_path = args.output_dir / "ter_candidates.csv"
    candidates.to_csv(candidates_path, index=False)
    print(f"Saved: {candidates_path}")

    ter_path = args.output_dir / "ter_annual.csv"
    ter.to_csv(ter_path, index=False)
    print(f"Saved: {ter_path}")

    ter_wide = make_ter_wide(ter)
    ter_wide_path = args.output_dir / "ter_breakdown_wide.csv"
    ter_wide.to_csv(ter_wide_path, index=False)
    print(f"Saved: {ter_wide_path}")

    if annual_path.suffix.lower() in {".xlsx", ".xls"}:
        flow_diagnostics = build_flow_diagnostics_from_annual_wide_xlsx(annual_path, fund_map=fund_map)
        flow_diagnostics_path = args.output_dir / "flow_diagnostics.csv"
        flow_diagnostics.to_csv(flow_diagnostics_path, index=False)
        print(f"Saved: {flow_diagnostics_path}")

    write_annual_matching_report(args.output_dir)

    if ter.empty:
        log(
            "TER is niet automatisch geëxtraheerd. "
            "Controleer data/ter_candidates.csv en maak/editeer data/ter_annual.csv handmatig."
        )


if __name__ == "__main__":
    main()
