#!/usr/bin/env python3
"""
process_pension_alpha.py

Verwerk gestandaardiseerde pensioenfondsdata:
- periode-normalisatie: 2015K1 en 2015Q1 worden allebei 2015Q1
- TER-correctie met expliciet beleid voor missende jaren
- factorregressie
- pairwise alpha-tests
- HTML-rapport
- audit CSV's
- data-quality checks

Dit script doet GEEN DNB-downloads. Volledig lege factor-kolommen worden automatisch genegeerd voor regressies. --ter-missing-policy zero ondersteunt pd.NA in used_ter_year.
Voor all-funds runs zijn extra TER-fallbacks beschikbaar: nearest en nearest_zero.

CSV-contracten:

1. returns_quarterly.csv
    fund,period,return_quarterly

    Optioneel:
    period_original,year,quarter,dnb_reporter,source_return_post

2. ter_annual.csv
    fund,year,ter_annual

    Optioneel:
    asset_management_costs,transaction_costs,source_type,confidence,notes

3. factors.csv
    period,rf,equity,duration,credit,real_estate,fx,...

4. flow_diagnostics.csv (optioneel)
    fund,year,total_premium,active_participants,deferred_participants,
    pensioners,total_participants,active_ratio,pensioner_ratio,participant_growth

Gebruik:
    pip install pandas numpy statsmodels matplotlib
    python process_pension_alpha.py \
      --returns data/returns_quarterly.csv \
      --ter data/ter_annual.csv \
      --factors factors.csv

Belangrijk:
- Alle rendementen, kosten en factoren zijn decimalen.
- 0.025 = 2.5%
"""

from __future__ import annotations

import argparse
from html import escape as escape_html
import json
import re
import warnings
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import statsmodels.api as sm
from statsmodels.stats.multitest import multipletests


class InputFormatError(ValueError):
    """Duidelijke fout voor CSV-input die niet aan het verwachte contract voldoet."""


RETURNS_EXAMPLE = """
fund,period,return_quarterly
ABP,2015Q1,0.025
PFZW,2015K1,0.021
"""

TER_EXAMPLE = """
fund,year,ter_annual
ABP,2024,0.0045
PFZW,2024,0.0052
"""

FACTORS_EXAMPLE = """
period,rf,equity,duration,credit,real_estate,fx
2015Q1,0.0001,0.0400,-0.0100,0.0050,0.0200,-0.0300
2015Q2,0.0001,-0.0200,0.0100,-0.0020,-0.0100,0.0100
"""

FLOW_EXAMPLE = """
fund,year,total_premium,active_participants,deferred_participants,pensioners,total_participants
ABP,2024,15626609,1306248,781978,1051433,3139659
"""


ALPHA_COLUMNS = [
    "fund",
    "alpha_quarterly",
    "alpha_quarterly_ci_low",
    "alpha_quarterly_ci_high",
    "alpha_annualized",
    "alpha_annualized_ci_low",
    "alpha_annualized_ci_high",
    "t_alpha",
    "p_alpha",
    "p_alpha_holm",
    "r2",
    "n_obs",
]

PAIRWISE_COLUMNS = [
    "pair",
    "fund_1",
    "fund_2",
    "alpha_quarterly",
    "alpha_quarterly_ci_low",
    "alpha_quarterly_ci_high",
    "alpha_annualized",
    "alpha_annualized_ci_low",
    "alpha_annualized_ci_high",
    "t_alpha",
    "p_alpha",
    "p_alpha_holm",
    "r2",
    "n_obs",
]

EXCLUDED_REGRESSION_FACTOR_COLUMNS = {"period", "period_original", "rf", "ff_rf"}

PENSION_FACTOR_COLUMNS = ["equity", "duration", "credit", "real_estate", "fx"]

KEN_FRENCH_FACTOR_COLUMNS = ["ff_mkt_rf", "ff_smb", "ff_hml", "ff_rmw", "ff_cma", "ff_mom"]


def normalize_period_label(period: Any) -> str | None:
    """
    Normaliseer kwartaalperioden naar 2015Q1.

    Ondersteunt:
    - 2015K1
    - 2015Q1
    - 2015-K1
    - 2015 Q1
    - datums, via pandas Period fallback
    """
    if pd.isna(period):
        return None

    text = str(period).strip().upper()

    match = re.search(r"(19\d{2}|20\d{2})\s*[- ]?\s*[KQ]\s*([1-4])", text)
    if match:
        return f"{match.group(1)}Q{match.group(2)}"

    # Fallback voor datums zoals 2015-03-31.
    try:
        ts = pd.to_datetime(period, errors="coerce")
        if pd.notna(ts):
            return str(ts.to_period("Q"))
    except Exception:
        pass

    return None


def period_year(period: str) -> int:
    norm = normalize_period_label(period)
    if norm is None:
        raise InputFormatError(f"Kon periode niet normaliseren: {period!r}")
    return int(norm[:4])


def period_quarter(period: str) -> int:
    norm = normalize_period_label(period)
    if norm is None:
        raise InputFormatError(f"Kon periode niet normaliseren: {period!r}")
    return int(norm[-1])


def format_column_error(
    file_label: str,
    path: Path,
    required: set[str],
    found: list[str],
    optional: set[str] | None = None,
    example: str | None = None,
) -> str:
    optional = optional or set()
    missing = sorted(required - set(found))

    lines = [
        "",
        f"Inputformaat fout in {file_label}: {path}",
        f"Ontbrekende verplichte kolommen: {missing}",
        "",
        "Verwachte verplichte kolommen:",
        "  " + ", ".join(sorted(required)),
    ]

    if optional:
        lines.extend([
            "",
            "Optionele kolommen:",
            "  " + ", ".join(sorted(optional)),
        ])

    lines.extend([
        "",
        "Gevonden kolommen:",
        "  " + (", ".join(found) if found else "(geen kolommen gevonden)"),
    ])

    if example:
        lines.extend(["", "Voorbeeldformaat:", example.strip()])

    return "\n".join(lines)


def require_columns(
    df: pd.DataFrame,
    path: Path,
    file_label: str,
    required: set[str],
    optional: set[str] | None = None,
    example: str | None = None,
) -> None:
    df.columns = [str(c).strip() for c in df.columns]
    found = list(df.columns)
    missing = required - set(found)

    if missing:
        raise InputFormatError(
            format_column_error(file_label, path, required, found, optional, example)
        )


def read_returns(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, sep=None, engine="python")
    require_columns(
        df=df,
        path=path,
        file_label="returns",
        required={"fund", "period", "return_quarterly"},
        optional={"period_original", "year", "quarter", "dnb_reporter", "source_return_post"},
        example=RETURNS_EXAMPLE,
    )

    df["fund"] = df["fund"].astype(str).str.strip()

    if "period_original" not in df.columns:
        df["period_original"] = df["period"].astype(str)

    df["period"] = df["period"].map(normalize_period_label)

    bad_periods = df.loc[df["period"].isna(), "period_original"].drop_duplicates().tolist()
    if bad_periods:
        raise InputFormatError(
            "Kon niet alle return-perioden normaliseren naar 2015Q1-formaat.\n"
            f"Voorbeelden: {bad_periods[:20]}"
        )

    df["year"] = df["period"].map(period_year).astype(int)
    df["quarter"] = df["period"].map(period_quarter).astype(int)
    df["return_quarterly"] = pd.to_numeric(df["return_quarterly"], errors="coerce")

    if df["return_quarterly"].isna().all():
        raise InputFormatError(
            f"Inputformaat fout in returns: {path}\n"
            "Kolom 'return_quarterly' bevat geen numerieke waarden.\n"
            "Gebruik decimalen: 0.025 = 2.5%."
        )

    return df.dropna(subset=["return_quarterly"]).sort_values(["fund", "period"]).reset_index(drop=True)


def read_ter(path: Path | None) -> pd.DataFrame | None:
    if path is None:
        return None

    df = pd.read_csv(path, sep=None, engine="python")
    require_columns(
        df=df,
        path=path,
        file_label="ter",
        required={"fund", "year", "ter_annual"},
        optional={
            "asset_management_costs", "transaction_costs", "source_type",
            "confidence", "notes", "dnb_reporter", "source_asset_management_post",
            "source_transaction_post",
        },
        example=TER_EXAMPLE,
    )

    df["fund"] = df["fund"].astype(str).str.strip()
    df["year"] = pd.to_numeric(df["year"], errors="coerce").astype("Int64")
    df["ter_annual"] = pd.to_numeric(df["ter_annual"], errors="coerce")

    if df["ter_annual"].isna().all():
        raise InputFormatError(
            f"Inputformaat fout in ter: {path}\n"
            "Kolom 'ter_annual' bevat geen numerieke waarden.\n"
            "Gebruik decimalen: 0.0045 = 0.45%."
        )

    for col, default in [
        ("asset_management_costs", np.nan),
        ("transaction_costs", np.nan),
        ("source_type", "manual_or_external"),
        ("confidence", np.nan),
        ("notes", ""),
    ]:
        if col not in df.columns:
            df[col] = default

    df["asset_management_costs"] = pd.to_numeric(df["asset_management_costs"], errors="coerce")
    df["transaction_costs"] = pd.to_numeric(df["transaction_costs"], errors="coerce")
    df["confidence"] = pd.to_numeric(df["confidence"], errors="coerce")
    df["ter_quarterly"] = (1.0 + df["ter_annual"]) ** 0.25 - 1.0

    return df.sort_values(["fund", "year"]).reset_index(drop=True)


def read_factors(path: Path | None) -> pd.DataFrame | None:
    if path is None:
        return None

    df = pd.read_csv(path, sep=None, engine="python")
    require_columns(
        df=df,
        path=path,
        file_label="factors",
        required={"period"},
        optional={"rf", "equity", "duration", "rates", "credit", "real_estate", "fx"},
        example=FACTORS_EXAMPLE,
    )

    df["period_original"] = df["period"].astype(str)
    df["period"] = df["period"].map(normalize_period_label)

    bad_periods = df.loc[df["period"].isna(), "period_original"].drop_duplicates().tolist()
    if bad_periods:
        raise InputFormatError(
            "Kon niet alle factor-perioden normaliseren naar 2015Q1-formaat.\n"
            f"Voorbeelden: {bad_periods[:20]}"
        )

    for col in df.columns:
        if col not in ["period", "period_original"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    if "rf" not in df.columns:
        df["rf"] = 0.0

    factor_cols = [c for c in df.columns if c not in ["period", "period_original", "rf"]]
    if not factor_cols:
        raise InputFormatError(
            "Factorbestand bevat geen factor-kolommen naast period/rf.\n"
            "Voeg bijvoorbeeld equity,duration,credit,real_estate,fx toe.\n"
            + FACTORS_EXAMPLE.strip()
        )

    if df[factor_cols].isna().all(axis=None):
        raise InputFormatError(
            f"Inputformaat fout in factors: {path}\n"
            "Geen enkele factor-kolom bevat numerieke waarden.\n"
            "Gebruik decimalen: 0.04 = 4%."
        )

    return df.drop_duplicates("period").sort_values("period").reset_index(drop=True)




def read_flow_diagnostics(path: Path | None) -> pd.DataFrame | None:
    if path is None:
        return None
    if not path.exists():
        return None

    df = pd.read_csv(path, sep=None, engine="python")
    require_columns(
        df=df,
        path=path,
        file_label="flow_diagnostics",
        required={"fund", "year"},
        optional={
            "premium_employee", "premium_employer", "total_premium",
            "active_participants", "deferred_participants", "pensioners", "total_participants",
            "active_ratio", "deferred_ratio", "pensioner_ratio", "dependency_ratio_pensioners_to_active",
            "participant_growth", "active_participant_growth", "total_premium_growth",
            "premium_per_active_participant_thousand_eur", "premium_per_total_participant_thousand_eur",
            "dnb_reporter", "source_type", "notes",
        },
        example=FLOW_EXAMPLE,
    )

    df["fund"] = df["fund"].astype(str).str.strip()
    df["year"] = pd.to_numeric(df["year"], errors="coerce").astype("Int64")
    for col in df.columns:
        if col not in ["fund", "dnb_reporter", "source_type", "notes"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    return df.sort_values(["fund", "year"]).reset_index(drop=True)


def apply_ter(
    returns: pd.DataFrame,
    ter: pd.DataFrame | None,
    missing_policy: str = "ffill",
) -> pd.DataFrame:
    """
    TER-beleid:
    - ffill: gebruik laatst beschikbare TER per fonds voor latere jaren; faalt als er geen eerdere TER is.
    - nearest: gebruik dichtstbijzijnde TER-jaar per fonds; faalt als fonds helemaal geen TER heeft.
    - nearest_zero: gebruik dichtstbijzijnde TER-jaar per fonds; TER=0 als fonds helemaal geen TER heeft.
    - error: stop als TER mist voor fund/year.
    - zero: gebruik TER=0 voor missende fund/year, expliciet gemarkeerd.
    - drop: verwijder rijen zonder TER.

    Let op:
    ffill is geschikt voor de vaste 8-fondsen set.
    nearest_zero is pragmatischer voor --fund-selection all, maar minder zuiver.
    """
    out = returns.copy()

    if ter is None or ter.empty:
        out["asset_management_costs"] = np.nan
        out["transaction_costs"] = np.nan
        out["ter_annual"] = 0.0
        out["ter_quarterly"] = 0.0
        out["used_ter_year"] = pd.NA
        out["ter_missing_policy_applied"] = "no_ter_file"
        out["return_after_ter"] = out["return_quarterly"]
        return out

    exact = ter.copy()
    exact["used_ter_year"] = exact["year"]
    cols = [
        "fund", "year", "used_ter_year", "asset_management_costs",
        "transaction_costs", "ter_annual", "ter_quarterly",
        "source_type", "confidence", "notes",
    ]
    cols = [c for c in cols if c in exact.columns]

    merged = out.merge(exact[cols], on=["fund", "year"], how="left")
    missing_mask = merged["ter_annual"].isna()

    def apply_zero(idx: int, note: str = "TER ontbreekt; policy zero toegepast.") -> None:
        merged.at[idx, "asset_management_costs"] = 0.0
        merged.at[idx, "transaction_costs"] = 0.0
        merged.at[idx, "ter_annual"] = 0.0
        merged.at[idx, "ter_quarterly"] = 0.0
        merged.at[idx, "used_ter_year"] = pd.NA
        merged.at[idx, "source_type"] = "missing_ter_zero"
        merged.at[idx, "confidence"] = 0.0
        merged.at[idx, "notes"] = note

    def apply_ter_record(idx: int, row: pd.Series, rec: pd.Series, policy_label: str) -> None:
        for col in [
            "asset_management_costs", "transaction_costs",
            "ter_annual", "ter_quarterly", "confidence",
        ]:
            if col in rec.index:
                merged.at[idx, col] = rec[col]

        merged.at[idx, "used_ter_year"] = rec["year"]
        merged.at[idx, "source_type"] = f"{rec.get('source_type', 'ter')}|{policy_label}"
        merged.at[idx, "notes"] = (
            f"TER voor {int(row['year'])} ontbreekt; "
            f"TER uit {int(rec['year'])} gebruikt via policy {policy_label}."
        )

    if missing_mask.any():
        missing_pairs = (
            merged.loc[missing_mask, ["fund", "year"]]
            .drop_duplicates()
            .sort_values(["fund", "year"])
        )

        if missing_policy == "error":
            raise InputFormatError(
                "TER ontbreekt voor onderstaande fund/year-combinaties.\n"
                "Kies eventueel --ter-missing-policy ffill, nearest, nearest_zero, zero of drop.\n"
                + missing_pairs.to_string(index=False)
            )

        if missing_policy == "drop":
            merged = merged.loc[~missing_mask].copy()

        elif missing_policy == "zero":
            for idx in merged.loc[missing_mask].index:
                apply_zero(idx)

        elif missing_policy in ("ffill", "nearest", "nearest_zero"):
            ter_sorted = ter.sort_values(["fund", "year"]).copy()
            no_fill = []

            for idx, row in merged.loc[missing_mask].iterrows():
                fund = row["fund"]
                year = int(row["year"])
                fund_ter = ter_sorted[ter_sorted["fund"].eq(fund)].copy()

                rec = None
                policy_label = missing_policy

                if missing_policy == "ffill":
                    hist = fund_ter[fund_ter["year"].astype(int) <= year]
                    if not hist.empty:
                        rec = hist.iloc[-1]
                        policy_label = "ffill"

                else:
                    # Dichtstbijzijnde jaar; bij gelijke afstand liever verleden dan toekomst.
                    if not fund_ter.empty:
                        tmp = fund_ter.copy()
                        tmp["_year_int"] = tmp["year"].astype(int)
                        tmp["_distance"] = (tmp["_year_int"] - year).abs()
                        tmp["_future_penalty"] = (tmp["_year_int"] > year).astype(int)
                        tmp = tmp.sort_values(["_distance", "_future_penalty", "_year_int"])
                        rec = tmp.iloc[0]
                        policy_label = "nearest"

                if rec is None:
                    if missing_policy == "nearest_zero":
                        apply_zero(
                            idx,
                            note=(
                                "TER ontbreekt voor dit fund/year en er is geen enkele TER "
                                "voor dit fonds; policy nearest_zero gebruikt TER=0."
                            ),
                        )
                    else:
                        no_fill.append(idx)
                    continue

                apply_ter_record(idx, row, rec, policy_label)

            if no_fill:
                bad = merged.loc[no_fill, ["fund", "year"]].drop_duplicates()
                if missing_policy == "ffill":
                    extra = (
                        "\nBij --fund-selection all komt dit vaak doordat een fonds wel "
                        "kwartaalrendementen heeft, maar geen eerdere TER-observatie. "
                        "Gebruik bijvoorbeeld --ter-missing-policy nearest, nearest_zero, zero of drop."
                    )
                else:
                    extra = (
                        "\nDit fonds heeft waarschijnlijk helemaal geen TER-observaties in ter_annual.csv. "
                        "Gebruik eventueel --ter-missing-policy nearest_zero, zero of drop."
                    )
                raise InputFormatError(
                    "TER ontbreekt en kan niet worden ingevuld voor:\n"
                    + bad.to_string(index=False)
                    + extra
                )

        else:
            raise ValueError(f"Onbekend TER missing policy: {missing_policy}")

    merged["ter_annual"] = merged["ter_annual"].fillna(0.0)
    merged["ter_quarterly"] = merged["ter_quarterly"].fillna(0.0)
    merged["return_after_ter"] = merged["return_quarterly"] - merged["ter_quarterly"]

    is_exact_ter_year = (
        merged["year"].astype("Int64").eq(merged["used_ter_year"].astype("Int64"))
    ).fillna(False)
    merged["ter_missing_policy_applied"] = np.where(
        is_exact_ter_year,
        "exact",
        np.where(
            merged["source_type"].astype(str).str.contains(r"\\|nearest", na=False),
            "nearest",
            np.where(
                merged["source_type"].astype(str).str.contains(r"\\|ffill", na=False),
                "ffill",
                missing_policy,
            ),
        ),
    )

    return merged.sort_values(["fund", "period"]).reset_index(drop=True)


def add_factors(returns_after_ter: pd.DataFrame, factors: pd.DataFrame | None) -> pd.DataFrame:
    out = returns_after_ter.copy()

    if factors is None:
        out["rf"] = 0.0
        out["excess_return_after_ter"] = out["return_after_ter"]
        return out

    returns_periods = set(out["period"].dropna().unique())
    factor_periods = set(factors["period"].dropna().unique())
    overlap = returns_periods & factor_periods

    if not overlap:
        raise InputFormatError(
            "Geen overlap tussen return-perioden en factor-perioden.\n"
            f"Return perioden voorbeeld: {sorted(returns_periods)[:5]} ... {sorted(returns_periods)[-5:]}\n"
            f"Factor perioden voorbeeld: {sorted(factor_periods)[:5]} ... {sorted(factor_periods)[-5:]}\n"
            "Controleer of beide bestanden hetzelfde formaat gebruiken, bv. 2015Q1."
        )

    out = out.merge(factors.drop(columns=["period_original"], errors="ignore"), on="period", how="left")

    if "rf" not in out.columns:
        out["rf"] = 0.0

    out["excess_return_after_ter"] = out["return_after_ter"] - out["rf"]
    return out


def filter_by_end_period(df: pd.DataFrame, end_period: str | None, label: str) -> pd.DataFrame:
    """
    Filter tot en met end_period, bijvoorbeeld 2024Q4 of 2025K4.

    Wordt gebruikt om rendementsweergave en analyseperiode bewust te scheiden.
    """
    if df is None or df.empty or end_period is None:
        return df

    normalized_end = normalize_period_label(end_period)
    if normalized_end is None:
        raise InputFormatError(f"Kon {label} niet normaliseren: {end_period!r}. Gebruik bijvoorbeeld 2025Q4.")

    if "period" not in df.columns:
        raise InputFormatError(f"Kan {label} niet toepassen: DataFrame mist kolom period.")

    out = df.loc[df["period"].astype(str).le(normalized_end)].copy()
    if out.empty:
        raise InputFormatError(f"{label}={normalized_end} laat geen observaties over.")
    return out.reset_index(drop=True)


def all_factor_columns(factors: pd.DataFrame | None) -> list[str]:
    if factors is None:
        return []
    return [c for c in factors.columns if c not in EXCLUDED_REGRESSION_FACTOR_COLUMNS]


def parse_factor_columns_arg(value: str | None) -> list[str]:
    if value is None or str(value).strip() == "":
        return []
    return [c.strip() for c in str(value).split(",") if c.strip()]


def nonempty_factor_columns(factors: pd.DataFrame | None) -> list[str]:
    if factors is None:
        return []

    usable = []
    for c in all_factor_columns(factors):
        s = pd.to_numeric(factors[c], errors="coerce")
        if not s.isna().all():
            usable.append(c)
    return usable


def factor_columns(factors: pd.DataFrame | None) -> list[str]:
    """
    Factor-kolommen die bruikbaar zijn voor regressie.

    Volledig lege factor-kolommen worden genegeerd. Ook ff_rf wordt genegeerd,
    omdat risk-free al wordt gebruikt om de afhankelijke variabele als excess
    return te maken.
    """
    return nonempty_factor_columns(factors)


def apply_factor_model(
    factors: pd.DataFrame | None,
    factor_model: str = "all",
    factor_columns_arg: str | None = None,
) -> tuple[pd.DataFrame | None, pd.DataFrame]:
    """
    Beperk factors.csv tot een reproduceerbaar regressiemodel.

    - pension: equity,duration,credit,real_estate,fx
    - ken_french: ff_mkt_rf,ff_smb,ff_hml,ff_rmw,ff_cma,ff_mom
    - custom: exacte lijst uit --factor-columns
    - all: alle niet-lege factor-kolommen behalve period/rf/ff_rf
    """
    requested_custom = parse_factor_columns_arg(factor_columns_arg)

    if factors is None:
        meta = pd.DataFrame([{
            "factor_model": factor_model,
            "requested_factor_columns": ",".join(requested_custom),
            "selected_factor_columns": "",
            "available_nonempty_factor_columns": "",
            "missing_factor_columns": "",
            "ignored_empty_factor_columns": "",
            "notes": "Geen factors.csv opgegeven; regressies worden niet uitgevoerd.",
        }])
        return None, meta

    available = nonempty_factor_columns(factors)
    ignored_empty = ignored_empty_factor_columns(factors)

    if factor_model == "pension":
        requested = PENSION_FACTOR_COLUMNS
    elif factor_model == "ken_french":
        requested = KEN_FRENCH_FACTOR_COLUMNS
    elif factor_model == "custom":
        if not requested_custom:
            raise InputFormatError("--factor-model custom vereist --factor-columns, bijvoorbeeld: --factor-columns equity,duration,credit")
        requested = requested_custom
    elif factor_model == "all":
        requested = available
    else:
        raise InputFormatError(f"Onbekend factor_model: {factor_model}")

    missing = [c for c in requested if c not in factors.columns]
    empty_requested = [c for c in requested if c in factors.columns and c not in available]
    selected = [c for c in requested if c in available]

    if not selected:
        raise InputFormatError(
            "Geen bruikbare factor-kolommen over na factorselectie.\n"
            f"factor_model: {factor_model}\n"
            f"gevraagd: {requested}\n"
            f"beschikbaar/niet-leeg: {available}\n"
            f"ontbrekend: {missing}\n"
            f"leeg: {empty_requested}"
        )

    keep = ["period"]
    if "period_original" in factors.columns:
        keep.append("period_original")
    if "rf" in factors.columns:
        keep.append("rf")
    keep.extend(selected)

    out = factors[keep].copy()

    notes = []
    if missing:
        notes.append(f"Ontbrekende gevraagde factoren genegeerd: {missing}")
    if empty_requested:
        notes.append(f"Lege gevraagde factoren genegeerd: {empty_requested}")
    if "ff_rf" in factors.columns:
        notes.append("ff_rf niet gebruikt als regressorfactor; rf zit al in excess return.")
    if "equity" in selected and "ff_mkt_rf" in selected:
        notes.append("Let op: equity en ff_mkt_rf meten allebei aandelenmarktexposure; gebruik pension of ken_french voor hoofdmodellen.")

    meta = pd.DataFrame([{
        "factor_model": factor_model,
        "requested_factor_columns": ",".join(requested),
        "selected_factor_columns": ",".join(selected),
        "available_nonempty_factor_columns": ",".join(available),
        "missing_factor_columns": ",".join(missing),
        "ignored_empty_factor_columns": ",".join(sorted(set(ignored_empty + empty_requested))),
        "notes": " ".join(notes),
    }])

    print(f"Factor model: {factor_model}")
    print(f"Selected factor columns: {', '.join(selected)}")

    return out, meta


def ignored_empty_factor_columns(factors: pd.DataFrame | None) -> list[str]:
    if factors is None:
        return []

    ignored = []
    for c in all_factor_columns(factors):
        s = pd.to_numeric(factors[c], errors="coerce")
        if s.isna().all():
            ignored.append(c)

    return ignored


def returns_long_to_wide(df: pd.DataFrame, value_col: str) -> pd.DataFrame:
    wide = (
        df.pivot_table(index="period", columns="fund", values=value_col, aggfunc="first")
        .sort_index()
        .reset_index()
    )
    wide.columns.name = None
    return wide


def make_wide_audit(calculation_base: pd.DataFrame) -> pd.DataFrame:
    value_cols = [
        "return_quarterly", "asset_management_costs", "transaction_costs",
        "ter_annual", "ter_quarterly", "return_after_ter",
        "excess_return_after_ter",
    ]

    parts = []
    for col in value_cols:
        if col not in calculation_base.columns:
            continue
        part = calculation_base.pivot_table(index="period", columns="fund", values=col, aggfunc="first")
        part.columns = [f"{fund}__{col}" for fund in part.columns]
        parts.append(part)

    wide = pd.concat(parts, axis=1).reset_index() if parts else pd.DataFrame({"period": []})
    wide.columns.name = None

    excluded = set(value_cols + [
        "fund", "period", "period_original", "year", "quarter",
        "source_type", "confidence", "notes", "dnb_reporter",
        "source_return_post", "ter_missing_policy_applied",
    ])
    factor_cols = [c for c in calculation_base.columns if c not in excluded]
    factor_cols = ["rf"] + [c for c in factor_cols if c != "rf"]

    if factor_cols and "period" in calculation_base.columns:
        factors_once = calculation_base[["period"] + factor_cols].drop_duplicates("period")
        wide = wide.merge(factors_once, on="period", how="left")

    return wide.sort_values("period").reset_index(drop=True)


def make_ter_breakdown(ter: pd.DataFrame | None) -> tuple[pd.DataFrame, pd.DataFrame]:
    if ter is None or ter.empty:
        return pd.DataFrame(), pd.DataFrame()

    long = ter.copy()
    if "ter_quarterly" not in long.columns:
        long["ter_quarterly"] = (1.0 + long["ter_annual"]) ** 0.25 - 1.0

    preferred = [
        "fund", "year", "asset_management_costs", "transaction_costs",
        "ter_annual", "ter_quarterly", "source_type", "confidence", "notes",
    ]
    keep = [c for c in preferred if c in long.columns]
    extra = [c for c in long.columns if c not in keep]
    long = long[keep + extra].sort_values(["fund", "year"])

    parts = []
    for col in ["asset_management_costs", "transaction_costs", "ter_annual", "ter_quarterly"]:
        if col in long.columns:
            part = long.pivot_table(index="year", columns="fund", values=col, aggfunc="first")
            part.columns = [f"{fund}__{col}" for fund in part.columns]
            parts.append(part)

    wide = pd.concat(parts, axis=1).reset_index() if parts else pd.DataFrame()
    wide.columns.name = None
    return long, wide


def make_data_quality_checks(
    calculation_base: pd.DataFrame,
    factors: pd.DataFrame | None,
    annual_reconciliation: pd.DataFrame | None = None,
) -> pd.DataFrame:
    rows = []

    def add(severity: str, issue: str, count: int, details: str) -> None:
        rows.append({"severity": severity, "issue": issue, "count": int(count), "details": details})

    if "quarter" in calculation_base.columns:
        n = calculation_base["quarter"].isna().sum()
        if n:
            add("error", "quarter_missing", n, "Quarter kon niet uit period worden gelezen.")

    ret_missing = calculation_base["return_quarterly"].isna().sum()
    if ret_missing:
        add("warning", "return_quarterly_missing", ret_missing, "Missende rendementen worden niet gevuld; regressies droppen die observaties.")

    missing_ter = calculation_base["ter_missing_policy_applied"].ne("exact").sum() if "ter_missing_policy_applied" in calculation_base.columns else 0
    if missing_ter:
        detail_df = (
            calculation_base.loc[calculation_base["ter_missing_policy_applied"].ne("exact"), ["fund", "year", "used_ter_year", "ter_missing_policy_applied"]]
            .drop_duplicates()
            .sort_values(["fund", "year"])
        )
        add("warning", "ter_not_exact", len(detail_df), detail_df.to_string(index=False))

    if factors is None:
        add("info", "factors_not_supplied", 1, "Geen factors.csv opgegeven; alpha_results en pairwise_alpha_results blijven leeg.")
    else:
        ignored = ignored_empty_factor_columns(factors)
        if ignored:
            add(
                "warning",
                "ignored_empty_factor_columns",
                len(ignored),
                "Volledig lege factor-kolommen genegeerd voor regressies: " + ", ".join(ignored),
            )

        fcols = factor_columns(factors)
        if not fcols:
            add(
                "error",
                "no_usable_factor_columns",
                0,
                "Geen bruikbare factor-kolommen na het negeren van volledig lege kolommen.",
            )
        else:
            factor_na = calculation_base[fcols].isna().all(axis=1).sum()
            if factor_na:
                add("warning", "factor_rows_missing", factor_na, "Voor sommige return-perioden ontbreken factorwaarden.")

    if annual_reconciliation is not None and not annual_reconciliation.empty:
        review = annual_reconciliation[annual_reconciliation["reconciliation_status"].eq("review_difference")]
        if not review.empty:
            details_cols = [
                "fund",
                "year",
                "dnb_compounded_annual_return",
                "official_annual_return",
                "difference_pp",
            ]
            add(
                "warning",
                "annual_reconciliation_difference",
                int(len(review)),
                review[details_cols].head(20).to_string(index=False),
            )

        incomplete = annual_reconciliation[annual_reconciliation["reconciliation_status"].eq("incomplete_year")]
        if not incomplete.empty:
            add(
                "info",
                "annual_reconciliation_incomplete_year",
                int(len(incomplete)),
                incomplete[["fund", "year", "n_quarters", "quarters_present"]].head(20).to_string(index=False),
            )

    if not rows:
        add("ok", "no_issues_detected", 0, "Geen structurele issues gevonden.")

    return pd.DataFrame(rows)


def run_alpha_regressions(calculation_base: pd.DataFrame, factors: pd.DataFrame | None, maxlags: int) -> pd.DataFrame:
    fcols = factor_columns(factors)
    if not fcols:
        return pd.DataFrame(columns=ALPHA_COLUMNS)

    rows = []
    for fund, data in calculation_base.groupby("fund"):
        data = data.dropna(subset=["excess_return_after_ter"] + fcols)
        if len(data) < len(fcols) + 8:
            continue

        y = data["excess_return_after_ter"]
        X = sm.add_constant(data[fcols], has_constant="add")

        model = sm.OLS(y, X).fit(cov_type="HAC", cov_kwds={"maxlags": maxlags})
        alpha_q = float(model.params["const"])
        alpha_ci = alpha_confidence_interval_from_model(model, alpha_q)

        row = {
            "fund": fund,
            "alpha_quarterly": alpha_q,
            "alpha_annualized": annualize_quarterly_return(alpha_q),
            **alpha_ci,
            "t_alpha": float(model.tvalues["const"]),
            "p_alpha": float(model.pvalues["const"]),
            "r2": float(model.rsquared),
            "n_obs": int(model.nobs),
        }
        row.update({f"beta_{c}": float(model.params[c]) for c in fcols})
        rows.append(row)

    out = pd.DataFrame(rows)
    if out.empty:
        return pd.DataFrame(columns=ALPHA_COLUMNS + [f"beta_{c}" for c in fcols])

    out["p_alpha_holm"] = multipletests(out["p_alpha"], method="holm")[1]
    return out.sort_values("alpha_annualized", ascending=False).reset_index(drop=True)


def run_pairwise_alpha(calculation_base: pd.DataFrame, factors: pd.DataFrame | None, maxlags: int) -> pd.DataFrame:
    fcols = factor_columns(factors)
    if not fcols:
        return pd.DataFrame(columns=PAIRWISE_COLUMNS)

    returns_wide = calculation_base.pivot_table(
        index="period",
        columns="fund",
        values="excess_return_after_ter",
        aggfunc="first",
    ).reset_index()

    factor_values = calculation_base[["period"] + fcols].drop_duplicates("period")
    df = returns_wide.merge(factor_values, on="period", how="left")

    funds = [c for c in returns_wide.columns if c != "period"]
    rows = []

    for i, f1 in enumerate(funds):
        for f2 in funds[i + 1:]:
            data = df[["period", f1, f2] + fcols].dropna()
            if len(data) < len(fcols) + 8:
                continue

            y = data[f1] - data[f2]
            X = sm.add_constant(data[fcols], has_constant="add")
            model = sm.OLS(y, X).fit(cov_type="HAC", cov_kwds={"maxlags": maxlags})

            alpha_q = float(model.params["const"])
            alpha_ci = alpha_confidence_interval_from_model(model, alpha_q)
            row = {
                "pair": f"{f1} minus {f2}",
                "fund_1": f1,
                "fund_2": f2,
                "alpha_quarterly": alpha_q,
                "alpha_annualized": annualize_quarterly_return(alpha_q),
                **alpha_ci,
                "t_alpha": float(model.tvalues["const"]),
                "p_alpha": float(model.pvalues["const"]),
                "r2": float(model.rsquared),
                "n_obs": int(model.nobs),
            }
            row.update({f"beta_{c}": float(model.params[c]) for c in fcols})
            rows.append(row)

    out = pd.DataFrame(rows)
    if out.empty:
        return pd.DataFrame(columns=PAIRWISE_COLUMNS + [f"beta_{c}" for c in fcols])

    out["p_alpha_holm"] = multipletests(out["p_alpha"], method="holm")[1]
    return out.sort_values("p_alpha_holm").reset_index(drop=True)


def exposure_bucket(series: pd.Series, value: float, high_label: str, mid_label: str, low_label: str) -> str:
    clean = pd.to_numeric(series, errors="coerce").dropna()
    if clean.empty or pd.isna(value):
        return "onvoldoende data"
    q25 = clean.quantile(0.25)
    q75 = clean.quantile(0.75)
    if value >= q75:
        return high_label
    if value <= q25:
        return low_label
    return mid_label


def make_portfolio_exposure_diagnostics(alpha: pd.DataFrame, calculation_base: pd.DataFrame) -> pd.DataFrame:
    """
    Maak een returns-based portefeuilleprofiel uit regressie-beta's.

    Belangrijk:
    - Dit is géén holdings-based portefeuilleconstructie.
    - Alpha verklaart niet hoe de portefeuille eruit ziet; beta's/loadings geven wel een proxy
      voor marktgevoeligheden.
    """
    if alpha is None or alpha.empty:
        return pd.DataFrame()

    out = alpha.copy()
    beta_cols = [c for c in out.columns if c.startswith("beta_")]
    factor_cols = [c.removeprefix("beta_") for c in beta_cols]

    # Gemiddelde factorpremies in de analyseperiode: ruwe indicatie voor gemiddelde bijdrage.
    factor_means = {}
    if calculation_base is not None and not calculation_base.empty and factor_cols:
        available_factors = [c for c in factor_cols if c in calculation_base.columns]
        if available_factors:
            factors_once = calculation_base[["period"] + available_factors].drop_duplicates("period")
            factor_means = factors_once[available_factors].apply(pd.to_numeric, errors="coerce").mean().to_dict()

    for factor in factor_cols:
        beta_col = f"beta_{factor}"
        if beta_col in out.columns:
            out[f"avg_quarterly_contribution_{factor}"] = pd.to_numeric(out[beta_col], errors="coerce") * float(factor_means.get(factor, 0.0))
            out[f"avg_annualized_simple_contribution_{factor}"] = 4.0 * out[f"avg_quarterly_contribution_{factor}"]

    def col_or_zero(name: str) -> pd.Series:
        return pd.to_numeric(out[name], errors="coerce") if name in out.columns else pd.Series(0.0, index=out.index)

    # Pension-model samenvattingen. Voor andere factor-modellen blijven missende componenten nul.
    out["broad_risky_beta"] = col_or_zero("beta_equity") + col_or_zero("beta_credit") + col_or_zero("beta_real_estate")
    out["interest_rate_beta"] = col_or_zero("beta_duration")
    out["currency_beta"] = col_or_zero("beta_fx")

    positive_parts = {
        "equity": col_or_zero("beta_equity").clip(lower=0),
        "credit": col_or_zero("beta_credit").clip(lower=0),
        "real_estate": col_or_zero("beta_real_estate").clip(lower=0),
        "duration": col_or_zero("beta_duration").clip(lower=0),
        "fx_abs": col_or_zero("beta_fx").abs(),
    }
    denominator = sum(positive_parts.values()).replace(0, np.nan)
    for name, values in positive_parts.items():
        out[f"loading_share_{name}"] = values / denominator

    out["risk_exposure_profile"] = [
        exposure_bucket(out["broad_risky_beta"], v, "hoog risky/rendementsgevoelig", "gemiddeld risky profiel", "laag risky/defensiever")
        for v in out["broad_risky_beta"]
    ]
    out["duration_profile"] = [
        exposure_bucket(out["interest_rate_beta"], v, "hoog duration/rentegevoelig", "gemiddeld durationprofiel", "laag durationprofiel")
        for v in out["interest_rate_beta"]
    ]

    def alpha_label(row: pd.Series) -> str:
        alpha_ann = row.get("alpha_annualized", np.nan)
        p_holm = row.get("p_alpha_holm", np.nan)
        if pd.notna(p_holm) and p_holm < 0.05 and alpha_ann > 0:
            return "positieve alpha, significant na Holm-correctie"
        if pd.notna(p_holm) and p_holm < 0.05 and alpha_ann < 0:
            return "negatieve alpha, significant na Holm-correctie"
        if pd.notna(alpha_ann) and alpha_ann > 0.02:
            return "positieve alpha-puntinschatting, niet significant na Holm"
        if pd.notna(alpha_ann) and alpha_ann < -0.02:
            return "negatieve alpha-puntinschatting, niet significant na Holm"
        return "alpha rond nul / geen sterk residueel signaal"

    out["alpha_interpretation"] = out.apply(alpha_label, axis=1)

    contribution_cols = [c for c in out.columns if c.startswith("avg_annualized_simple_contribution_")]
    if contribution_cols:
        def largest_driver(row: pd.Series) -> str:
            vals = row[contribution_cols].apply(pd.to_numeric, errors="coerce").abs()
            if vals.dropna().empty:
                return ""
            col = vals.idxmax()
            factor = col.replace("avg_annualized_simple_contribution_", "")
            signed_value = row[col]
            direction = "positief" if pd.notna(signed_value) and signed_value >= 0 else "negatief"
            return f"{factor} ({direction})"
        out["largest_average_factor_contribution"] = out.apply(largest_driver, axis=1)
    else:
        out["largest_average_factor_contribution"] = ""

    preferred = [
        "fund", "risk_exposure_profile", "duration_profile", "alpha_interpretation",
        "broad_risky_beta", "interest_rate_beta", "currency_beta",
        "beta_equity", "beta_credit", "beta_real_estate", "beta_duration", "beta_fx",
        "loading_share_equity", "loading_share_credit", "loading_share_real_estate",
        "loading_share_duration", "loading_share_fx_abs",
        "alpha_annualized", "p_alpha_holm", "r2", "n_obs", "largest_average_factor_contribution",
    ]
    contribution_preferred = [c for c in out.columns if c.startswith("avg_annualized_simple_contribution_")]
    keep = [c for c in preferred if c in out.columns] + contribution_preferred
    keep += [c for c in out.columns if c.startswith("beta_") and c not in keep]
    result = out[keep].sort_values(["broad_risky_beta", "alpha_annualized"], ascending=[False, False]).reset_index(drop=True)
    return result




# -----------------------------------------------------------------------------
# Annual return reconciliation
# -----------------------------------------------------------------------------

STATIC_ABP_OFFICIAL_ANNUAL_RETURNS = [
    {"fund": "ABP", "year": 2015, "official_annual_return": 0.027, "source": "ABP beleggingsresultaten Rendement 2008-2025", "source_url": "https://www.abp.nl/over-abp/beleggingen/beleggingsresultaten", "notes": ""},
    {"fund": "ABP", "year": 2016, "official_annual_return": 0.095, "source": "ABP beleggingsresultaten Rendement 2008-2025", "source_url": "https://www.abp.nl/over-abp/beleggingen/beleggingsresultaten", "notes": ""},
    {"fund": "ABP", "year": 2017, "official_annual_return": 0.076, "source": "ABP beleggingsresultaten Rendement 2008-2025", "source_url": "https://www.abp.nl/over-abp/beleggingen/beleggingsresultaten", "notes": ""},
    {"fund": "ABP", "year": 2018, "official_annual_return": -0.023, "source": "ABP beleggingsresultaten Rendement 2008-2025", "source_url": "https://www.abp.nl/over-abp/beleggingen/beleggingsresultaten", "notes": ""},
    {"fund": "ABP", "year": 2019, "official_annual_return": 0.168, "source": "ABP beleggingsresultaten Rendement 2008-2025", "source_url": "https://www.abp.nl/over-abp/beleggingen/beleggingsresultaten", "notes": ""},
    {"fund": "ABP", "year": 2020, "official_annual_return": 0.066, "source": "ABP beleggingsresultaten Rendement 2008-2025", "source_url": "https://www.abp.nl/over-abp/beleggingen/beleggingsresultaten", "notes": ""},
    {"fund": "ABP", "year": 2021, "official_annual_return": 0.114, "source": "ABP beleggingsresultaten Rendement 2008-2025", "source_url": "https://www.abp.nl/over-abp/beleggingen/beleggingsresultaten", "notes": "ABP meldt dat 2021 is aangepast na definitieve jaarrekening."},
    {"fund": "ABP", "year": 2022, "official_annual_return": -0.176, "source": "ABP beleggingsresultaten Rendement 2008-2025", "source_url": "https://www.abp.nl/over-abp/beleggingen/beleggingsresultaten", "notes": ""},
    {"fund": "ABP", "year": 2023, "official_annual_return": 0.093, "source": "ABP beleggingsresultaten Rendement 2008-2025", "source_url": "https://www.abp.nl/over-abp/beleggingen/beleggingsresultaten", "notes": ""},
    {"fund": "ABP", "year": 2024, "official_annual_return": 0.084, "source": "ABP beleggingsresultaten Rendement 2008-2025", "source_url": "https://www.abp.nl/over-abp/beleggingen/beleggingsresultaten", "notes": "ABP meldt dat 2024 is aangepast na definitieve jaarrekening."},
    {"fund": "ABP", "year": 2025, "official_annual_return": -0.016, "source": "ABP beleggingsresultaten Rendement 2008-2025", "source_url": "https://www.abp.nl/over-abp/beleggingen/beleggingsresultaten", "notes": ""},
]


def read_official_annual_returns(path: Path | None) -> pd.DataFrame:
    if path is None:
        return pd.DataFrame(columns=["fund", "year", "official_annual_return", "source", "source_url", "notes"])

    df = pd.read_csv(path)
    required = ["fund", "year", "official_annual_return"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise InputFormatError(
            f"official annual returns mist kolommen: {missing}. "
            "Vereist: fund, year, official_annual_return"
        )

    if "source" not in df.columns:
        df["source"] = str(path)
    if "source_url" not in df.columns:
        df["source_url"] = ""
    if "notes" not in df.columns:
        df["notes"] = ""

    df["fund"] = df["fund"].astype(str).str.strip()
    df["year"] = pd.to_numeric(df["year"], errors="raise").astype(int)
    df["official_annual_return"] = pd.to_numeric(df["official_annual_return"], errors="raise")

    return df[["fund", "year", "official_annual_return", "source", "source_url", "notes"]]


def get_static_abp_official_annual_returns(enabled: bool = True) -> pd.DataFrame:
    if not enabled:
        return pd.DataFrame(columns=["fund", "year", "official_annual_return", "source", "source_url", "notes"])
    return pd.DataFrame(STATIC_ABP_OFFICIAL_ANNUAL_RETURNS)


def combine_official_annual_returns(
    custom: pd.DataFrame | None,
    include_static_abp: bool = True,
) -> pd.DataFrame:
    parts = []
    static = get_static_abp_official_annual_returns(include_static_abp)
    if not static.empty:
        parts.append(static)
    if custom is not None and not custom.empty:
        parts.append(custom)

    if not parts:
        return pd.DataFrame(columns=["fund", "year", "official_annual_return", "source", "source_url", "notes"])

    out = pd.concat(parts, ignore_index=True, sort=False)
    out["_priority"] = range(len(out))
    out = (
        out.sort_values("_priority")
        .drop_duplicates(["fund", "year"], keep="last")
        .drop(columns=["_priority"])
        .sort_values(["fund", "year"])
        .reset_index(drop=True)
    )
    return out



def make_estimated_portfolio_mix_timeseries(
    calculation_base: pd.DataFrame,
    rolling_window_quarters: int = 12,
) -> pd.DataFrame:
    """Maak een returns-based, tijdsvariërende schatting van de portefeuillemix per fonds."""
    if calculation_base is None or calculation_base.empty:
        return pd.DataFrame()

    factor_cols = [c for c in PENSION_FACTOR_COLUMNS if c in calculation_base.columns]
    required_cols = ["fund", "period", "excess_return_after_ter"] + factor_cols
    if not factor_cols or any(col not in calculation_base.columns for col in required_cols):
        return pd.DataFrame()

    work = calculation_base[required_cols].copy()
    work["period_normalized"] = work["period"].map(normalize_period_label)
    work = work[work["period_normalized"].notna()].copy()
    if work.empty:
        return pd.DataFrame()

    work["year"] = work["period_normalized"].str[:4].astype(int)
    work["quarter"] = work["period_normalized"].str[-1].astype(int)
    work = work.sort_values(["fund", "year", "quarter"]).reset_index(drop=True)

    rows: list[dict[str, Any]] = []
    min_obs = max(8, len(factor_cols) + 3)

    for fund, fund_data in work.groupby("fund"):
        fund_data = fund_data.sort_values(["year", "quarter"]).reset_index(drop=True)
        years = sorted(fund_data["year"].dropna().unique().tolist())

        for year in years:
            window = fund_data[fund_data["year"] <= year].tail(rolling_window_quarters).copy()
            window = window.dropna(subset=["excess_return_after_ter"] + factor_cols)
            if len(window) < min_obs:
                continue

            y = pd.to_numeric(window["excess_return_after_ter"], errors="coerce")
            X = window[factor_cols].apply(pd.to_numeric, errors="coerce")
            reg_data = pd.concat([y.rename("y"), X], axis=1).dropna()
            if len(reg_data) < min_obs:
                continue

            try:
                model = sm.OLS(reg_data["y"], sm.add_constant(reg_data[factor_cols], has_constant="add")).fit()
            except Exception:
                continue

            params = model.params.to_dict()
            equity = max(float(params.get("equity", 0.0)), 0.0)
            bonds = max(float(params.get("duration", 0.0)), 0.0) + max(float(params.get("credit", 0.0)), 0.0)
            real_estate = max(float(params.get("real_estate", 0.0)), 0.0)
            fx_overlay = abs(float(params.get("fx", 0.0)))

            total = equity + bonds + real_estate + fx_overlay
            if total <= 0:
                continue

            rows.append({
                "fund": str(fund),
                "year": int(year),
                "share_equity": equity / total,
                "share_bonds": bonds / total,
                "share_real_estate": real_estate / total,
                "share_fx_overlay": fx_overlay / total,
                "rolling_window_quarters": int(len(reg_data)),
                "r2": float(model.rsquared) if pd.notna(model.rsquared) else np.nan,
            })

    return pd.DataFrame(rows)


def portfolio_mix_timeseries_to_payload(portfolio_mix_timeseries: pd.DataFrame | None) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if portfolio_mix_timeseries is None or portfolio_mix_timeseries.empty:
        return payload

    needed = ["fund", "year", "share_equity", "share_bonds", "share_real_estate", "share_fx_overlay"]
    if any(col not in portfolio_mix_timeseries.columns for col in needed):
        return payload

    for fund, data in portfolio_mix_timeseries.groupby("fund"):
        data = data.sort_values("year")
        payload[str(fund)] = {
            "x": [int(x) for x in data["year"].tolist()],
            "series": {
                "Aandelen": [json_safe_value(x) for x in data["share_equity"].tolist()],
                "Obligaties": [json_safe_value(x) for x in data["share_bonds"].tolist()],
                "Vastgoed": [json_safe_value(x) for x in data["share_real_estate"].tolist()],
                "Valuta-overlay": [json_safe_value(x) for x in data["share_fx_overlay"].tolist()],
            },
            "rolling_window_quarters": int(pd.to_numeric(data["rolling_window_quarters"], errors="coerce").dropna().max())
            if "rolling_window_quarters" in data.columns and not data["rolling_window_quarters"].dropna().empty else None,
            "last_year": int(data["year"].max()) if not data["year"].dropna().empty else None,
        }
    return payload


def make_annual_returns_from_quarters(calculation_base: pd.DataFrame, value_col: str) -> pd.DataFrame:
    df = calculation_base.copy()
    # Gebruik de bestaande periodehelpers in dit script.
    # v12 gebruikte hier per ongeluk parse_period(), maar die functie bestaat alleen
    # in gather_factors.py. normalize_period_label/period_year/period_quarter zijn
    # de canonical helpers in process_pension_alpha.py.
    df["period_normalized_for_reconciliation"] = df["period"].map(normalize_period_label)
    if df["period_normalized_for_reconciliation"].isna().any():
        bad = df.loc[df["period_normalized_for_reconciliation"].isna(), "period"].drop_duplicates().head(10).tolist()
        raise InputFormatError(f"Kon perioden niet normaliseren voor annual reconciliation: {bad}")

    df["year_for_reconciliation"] = df["period_normalized_for_reconciliation"].map(period_year)
    df["quarter_for_reconciliation"] = df["period_normalized_for_reconciliation"].map(period_quarter)

    rows = []
    for (fund, year), g in df.groupby(["fund", "year_for_reconciliation"], dropna=False):
        valid = g.dropna(subset=[value_col]).copy()
        n_quarters = int(valid["quarter_for_reconciliation"].nunique())
        quarters_present = ",".join(str(q) for q in sorted(valid["quarter_for_reconciliation"].unique()))

        annual_return = np.nan
        if n_quarters > 0:
            annual_return = float((1.0 + valid[value_col]).prod() - 1.0)

        rows.append({
            "fund": fund,
            "year": int(year),
            "return_type": value_col,
            "dnb_compounded_annual_return": annual_return,
            "n_quarters": n_quarters,
            "quarters_present": quarters_present,
            "annual_sample_status": "complete_year" if n_quarters == 4 else "incomplete_year",
        })

    return pd.DataFrame(rows).sort_values(["fund", "year", "return_type"]).reset_index(drop=True)


def make_annual_reconciliation(
    calculation_base: pd.DataFrame,
    official_annual_returns: pd.DataFrame,
    tolerance_pp: float = 0.50,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    annual_raw = make_annual_returns_from_quarters(calculation_base, "return_quarterly")
    annual_after_ter = make_annual_returns_from_quarters(calculation_base, "return_after_ter")

    if official_annual_returns is None or official_annual_returns.empty:
        rec = annual_raw.copy()
        rec["official_annual_return"] = np.nan
        rec["difference"] = np.nan
        rec["difference_pp"] = np.nan
        rec["reconciliation_status"] = "no_official_return"
        rec["source"] = ""
        rec["source_url"] = ""
        rec["notes"] = ""
        return annual_raw, annual_after_ter, rec

    off = official_annual_returns.copy()
    off["fund"] = off["fund"].astype(str).str.strip()
    off["year"] = pd.to_numeric(off["year"], errors="raise").astype(int)

    rec = annual_raw.merge(off, on=["fund", "year"], how="left", validate="m:1")
    rec["difference"] = rec["dnb_compounded_annual_return"] - rec["official_annual_return"]
    rec["difference_pp"] = rec["difference"] * 100.0
    rec["reconciliation_status"] = np.where(
        rec["official_annual_return"].isna(),
        "no_official_return",
        np.where(
            rec["annual_sample_status"].ne("complete_year"),
            "incomplete_year",
            np.where(rec["difference_pp"].abs().le(tolerance_pp), "ok", "review_difference"),
        ),
    )
    return annual_raw, annual_after_ter, rec


def make_corrected_annual_returns(annual_raw: pd.DataFrame, official_annual_returns: pd.DataFrame) -> pd.DataFrame:
    corrected = annual_raw.merge(
        official_annual_returns[["fund", "year", "official_annual_return", "source", "source_url", "notes"]],
        on=["fund", "year"],
        how="left",
        validate="m:1",
    )
    corrected["corrected_annual_return"] = corrected["official_annual_return"].where(
        corrected["official_annual_return"].notna(),
        corrected["dnb_compounded_annual_return"],
    )
    corrected["correction_applied"] = corrected["official_annual_return"].notna()
    corrected["correction_basis"] = np.where(
        corrected["correction_applied"],
        "official_static_or_custom_annual_return",
        "dnb_compounded_quarterly_return",
    )
    return corrected.sort_values(["fund", "year"]).reset_index(drop=True)


def df_to_html_return_table(
    df: pd.DataFrame,
    max_rows: int | None = 120,
    pct_cols: list[str] | None = None,
) -> str:
    if df is None or df.empty:
        return '<p class="muted">Geen data beschikbaar.</p>'
    pct_cols = pct_cols or []
    view = df.copy() if max_rows is None else df.head(max_rows).copy()
    view.columns.name = None
    view.index.name = None
    for col in pct_cols:
        if col in view.columns:
            view[col] = view[col].map(lambda x: "" if pd.isna(x) else f"{float(x) * 100:.2f}%")
    if "difference_pp" in view.columns:
        view["difference_pp"] = view["difference_pp"].map(lambda x: "" if pd.isna(x) else f"{float(x):.2f} pp")
    html = view.to_html(index=False, escape=True, classes="data-table")
    if max_rows is not None and len(df) > max_rows:
        html += f'<p class="muted">Toont {max_rows} van {len(df):,} rijen. Download de volledige CSV voor alle rijen.</p>'
    return html



def make_returns_percent_wide(calculation_base: pd.DataFrame, value_col: str) -> pd.DataFrame:
    """
    Maak brede tabel met gewone kwartaalrendementen in procentpunten.

    Voorbeeld:
        ABP = 2.34 betekent 2.34%
    """
    wide = (
        calculation_base.pivot_table(index="period", columns="fund", values=value_col, aggfunc="first")
        .sort_index()
        .reset_index()
    )
    wide.columns.name = None

    for col in wide.columns:
        if col != "period":
            wide[col] = wide[col] * 100.0

    return wide



def finalize_figure(fig: plt.Figure, out_path: Path) -> None:
    """
    Sla figuur robuust op zonder Matplotlib tight_layout warnings.

    Bij all-funds runs kunnen legends en y-labels erg groot worden. Eerst proberen
    we tight_layout; als Matplotlib waarschuwt, vallen we terug op subplots_adjust.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("error", UserWarning)
        try:
            fig.tight_layout()
        except UserWarning:
            fig.subplots_adjust(left=0.18, right=0.78, bottom=0.18, top=0.88)

    fig.savefig(out_path, dpi=160, bbox_inches="tight")


def add_compact_legend(ax: plt.Axes, n_items: int, max_inline_items: int = 18) -> None:
    """
    Voor veel fondsen wordt een legenda in de plot onleesbaar en triggert hij
    layout warnings. Toon daarom alleen bij beperkte aantallen een legenda.
    """
    if n_items <= max_inline_items:
        ax.legend(loc="best", fontsize=8)
    else:
        ax.text(
            0.01,
            0.99,
            f"{n_items} fondsen getekend; legenda verborgen. Gebruik de HTML-tabellenfilter voor selectie.",
            transform=ax.transAxes,
            va="top",
            ha="left",
            fontsize=8,
            bbox={"boxstyle": "round,pad=0.3", "facecolor": "white", "alpha": 0.85},
        )



def save_quarterly_return_chart(
    calculation_base: pd.DataFrame,
    value_col: str,
    out_path: Path,
    title: str,
) -> bool:
    """
    Grafiek met niet-cumulatieve kwartaalrendementen.
    """
    wide = calculation_base.pivot_table(index="period", columns="fund", values=value_col, aggfunc="first")
    if wide.empty:
        return False

    fig, ax = plt.subplots(figsize=(12, 6))
    for fund in wide.columns:
        ax.plot(wide.index.astype(str), wide[fund], marker="o", linewidth=1.2, markersize=3, label=fund)

    ax.axhline(0.0, linewidth=0.8)
    ax.set_title(title)
    ax.set_xlabel("Kwartaal")
    ax.set_ylabel("Kwartaalrendement")
    ax.yaxis.set_major_formatter(lambda x, pos: f"{x * 100:.0f}%")
    ax.grid(True, alpha=0.3)
    add_compact_legend(ax, n_items=len(wide.columns))

    step = max(1, len(wide) // 10)
    ax.set_xticks(range(0, len(wide), step))
    ax.set_xticklabels(wide.index.astype(str)[::step], rotation=45, ha="right")

    finalize_figure(fig, out_path)
    plt.close(fig)
    return True


def df_to_html_percent_table(
    df: pd.DataFrame,
    max_rows: int | None = 80,
) -> str:
    """
    HTML tabel voor wide procentpunt-tabellen.
    Waarden staan al in procentpunten, dus 2.34 betekent 2.34%.
    """
    if df is None or df.empty:
        return '<p class="muted">Geen data beschikbaar.</p>'

    view = df.copy() if max_rows is None else df.head(max_rows).copy()
    view.columns.name = None
    view.index.name = None

    for col in view.columns:
        if col != "period":
            view[col] = view[col].map(lambda x: "" if pd.isna(x) else f"{float(x):.2f}%")

    html = view.to_html(index=False, escape=True, classes="data-table")
    if max_rows is not None and len(df) > max_rows:
        html += f'<p class="muted">Toont {max_rows} van {len(df):,} rijen. Download de volledige CSV voor alle rijen.</p>'
    return html



def save_cumulative_chart(calculation_base: pd.DataFrame, value_col: str, out_path: Path, title: str) -> bool:
    wide = calculation_base.pivot_table(index="period", columns="fund", values=value_col, aggfunc="first")
    if wide.empty:
        return False

    # Missing returns blijven ontbrekend; cumulatieve reeks wordt per fonds alleen over beschikbare observaties berekend.
    cumulative = wide.copy()
    for fund in cumulative.columns:
        cumulative[fund] = (1.0 + cumulative[fund].dropna()).cumprod() - 1.0

    fig, ax = plt.subplots(figsize=(12, 6))
    for fund in cumulative.columns:
        ax.plot(cumulative.index.astype(str), cumulative[fund], label=fund)

    ax.set_title(title)
    ax.set_xlabel("Kwartaal")
    ax.set_ylabel("Cumulatief rendement")
    ax.yaxis.set_major_formatter(lambda x, pos: f"{x * 100:.0f}%")
    ax.grid(True, alpha=0.3)
    add_compact_legend(ax, n_items=len(cumulative.columns))

    step = max(1, len(cumulative) // 10)
    ax.set_xticks(range(0, len(cumulative), step))
    ax.set_xticklabels(cumulative.index.astype(str)[::step], rotation=45, ha="right")

    finalize_figure(fig, out_path)
    plt.close(fig)
    return True


def save_ter_chart(ter: pd.DataFrame | None, out_path: Path) -> bool:
    if ter is None or ter.empty:
        return False

    fig, ax = plt.subplots(figsize=(12, 6))
    for fund, data in ter.groupby("fund"):
        data = data.sort_values("year")
        ax.plot(data["year"], data["ter_annual"], marker="o", label=fund)

    ax.set_title("TER-like kostenratio per fonds")
    ax.set_xlabel("Jaar")
    ax.set_ylabel("TER-like kostenratio")
    ax.yaxis.set_major_formatter(lambda x, pos: f"{x * 100:.2f}%")
    ax.grid(True, alpha=0.3)
    add_compact_legend(ax, n_items=ter["fund"].nunique())

    finalize_figure(fig, out_path)
    plt.close(fig)
    return True


def save_alpha_chart(alpha: pd.DataFrame, out_path: Path) -> bool:
    if alpha is None or alpha.empty or "alpha_annualized" not in alpha.columns:
        return False

    data = alpha.sort_values("alpha_annualized")
    fig_height = min(24, max(6, 0.28 * len(data) + 2))
    fig, ax = plt.subplots(figsize=(10, fig_height))
    ax.barh(data["fund"], data["alpha_annualized"])
    ax.set_title("Jaarlijkse alpha per fonds")
    ax.set_xlabel("Alpha, geannualiseerd")
    ax.xaxis.set_major_formatter(lambda x, pos: f"{x * 100:.2f}%")
    ax.grid(True, axis="x", alpha=0.3)

    if len(data) > 60:
        ax.tick_params(axis="y", labelsize=6)
    elif len(data) > 30:
        ax.tick_params(axis="y", labelsize=7)

    finalize_figure(fig, out_path)
    plt.close(fig)
    return True


def fmt_pct(x) -> str:
    if pd.isna(x):
        return ""
    return f"{float(x) * 100:.2f}%"


def fmt_num(x) -> str:
    if pd.isna(x):
        return ""
    return f"{float(x):.4f}"


def annualize_quarterly_return(value: float) -> float:
    """
    Annualiseer een kwartaalrendementachtige waarde.

    Voor CI-grenzen gebruiken we dezelfde transformatie als alpha_annualized.
    Waarden <= -100% per kwartaal zijn economisch niet zinvol voor deze transformatie
    en worden als NaN gerapporteerd.
    """
    if pd.isna(value) or value <= -1.0:
        return np.nan
    return (1.0 + float(value)) ** 4 - 1.0


def alpha_confidence_interval_from_model(model: Any, alpha_q: float) -> dict[str, float]:
    """
    95%-confidence interval voor alpha op basis van HAC/Newey-West SE.

    statsmodels levert hier een asymptotisch normaal interval bij cov_type="HAC".
    """
    try:
        ci = model.conf_int(alpha=0.05)
        if hasattr(ci, "loc"):
            low_q = float(ci.loc["const", 0])
            high_q = float(ci.loc["const", 1])
        else:
            const_idx = list(model.params.index).index("const")
            low_q = float(ci[const_idx][0])
            high_q = float(ci[const_idx][1])
    except Exception:
        se = float(model.bse["const"])
        low_q = float(alpha_q - 1.96 * se)
        high_q = float(alpha_q + 1.96 * se)

    return {
        "alpha_quarterly_ci_low": low_q,
        "alpha_quarterly_ci_high": high_q,
        "alpha_annualized_ci_low": annualize_quarterly_return(low_q),
        "alpha_annualized_ci_high": annualize_quarterly_return(high_q),
    }


def df_to_html_table(
    df: pd.DataFrame,
    max_rows: int | None = 80,
    percent_cols: list[str] | None = None,
    float_cols: list[str] | None = None,
) -> str:
    if df is None or df.empty:
        return '<p class="muted">Geen data beschikbaar.</p>'

    view = df.copy() if max_rows is None else df.head(max_rows).copy()
    view.columns.name = None
    view.index.name = None

    for col in percent_cols or []:
        if col in view.columns:
            view[col] = view[col].map(fmt_pct)

    for col in float_cols or []:
        if col in view.columns:
            view[col] = view[col].map(fmt_num)

    html = view.to_html(index=False, escape=True, classes="data-table")
    if max_rows is not None and len(df) > max_rows:
        html += f'<p class="muted">Toont {max_rows} van {len(df):,} rijen. Download de volledige CSV voor alle rijen.</p>'
    return html


def summarize_returns(calculation_base: pd.DataFrame, value_col: str) -> pd.DataFrame:
    rows = []
    for fund, data in calculation_base.groupby("fund"):
        data = data.sort_values("period")
        s = data[value_col].dropna()
        if s.empty:
            continue

        total = (1.0 + s).prod() - 1.0
        n = len(s)
        annualized = (1.0 + total) ** (4.0 / n) - 1.0 if n else np.nan

        rows.append({
            "fund": fund,
            "first_period": data.loc[data[value_col].notna(), "period"].min(),
            "last_period": data.loc[data[value_col].notna(), "period"].max(),
            "n_quarters": n,
            "missing_quarters": int(data[value_col].isna().sum()),
            "cumulative_return": total,
            "annualized_return": annualized,
            "quarterly_volatility": s.std(),
        })
    return pd.DataFrame(rows)




def json_safe_value(value):
    if pd.isna(value):
        return None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    return value


def series_to_plotly_xy(wide: pd.DataFrame) -> dict[str, dict[str, list]]:
    payload: dict[str, dict[str, list]] = {}
    if wide is None or wide.empty:
        return payload

    for fund in wide.columns:
        payload[str(fund)] = {
            "x": [str(x) for x in wide.index.tolist()],
            "y": [json_safe_value(x) for x in wide[fund].tolist()],
        }
    return payload


def make_echarts_chart_payload(
    calculation_base: pd.DataFrame,
    ter_long: pd.DataFrame | None,
    alpha: pd.DataFrame | None,
    portfolio_mix_timeseries: pd.DataFrame | None = None,
) -> dict[str, Any]:
    raw_wide = calculation_base.pivot_table(index="period", columns="fund", values="return_quarterly", aggfunc="first").sort_index()
    net_wide = calculation_base.pivot_table(index="period", columns="fund", values="return_after_ter", aggfunc="first").sort_index()

    cumulative_raw = raw_wide.copy()
    for fund in cumulative_raw.columns:
        cumulative_raw[fund] = (1.0 + cumulative_raw[fund].dropna()).cumprod() - 1.0

    cumulative_net = net_wide.copy()
    for fund in cumulative_net.columns:
        cumulative_net[fund] = (1.0 + cumulative_net[fund].dropna()).cumprod() - 1.0

    ter_payload: dict[str, dict[str, list]] = {}
    if ter_long is not None and not ter_long.empty and "fund" in ter_long.columns:
        for fund, data in ter_long.groupby("fund"):
            data = data.sort_values("year")
            ter_payload[str(fund)] = {
                "x": [int(x) for x in data["year"].tolist()],
                "y": [json_safe_value(x) for x in data["ter_annual"].tolist()],
            }

    alpha_payload = []
    if alpha is not None and not alpha.empty and "alpha_annualized" in alpha.columns:
        for _, row in alpha.sort_values("alpha_annualized").iterrows():
            alpha_payload.append({
                "fund": str(row["fund"]),
                "alpha_annualized": json_safe_value(row["alpha_annualized"]),
                "alpha_annualized_ci_low": json_safe_value(row.get("alpha_annualized_ci_low", np.nan)),
                "alpha_annualized_ci_high": json_safe_value(row.get("alpha_annualized_ci_high", np.nan)),
                "p_alpha_holm": json_safe_value(row.get("p_alpha_holm", np.nan)),
                "n_obs": json_safe_value(row.get("n_obs", np.nan)),
            })

    portfolio_mix_payload = portfolio_mix_timeseries_to_payload(portfolio_mix_timeseries)

    return {
        "quarterly_raw": series_to_plotly_xy(raw_wide),
        "quarterly_net": series_to_plotly_xy(net_wide),
        "cumulative_raw": series_to_plotly_xy(cumulative_raw),
        "cumulative_net": series_to_plotly_xy(cumulative_net),
        "ter": ter_payload,
        "alpha": alpha_payload,
        "portfolio_mix": portfolio_mix_payload,
    }




def make_sources_html(source_files: dict[str, str | None] | None = None, repo_url: str | None = None, generated_branch_url: str | None = None) -> str:
    source_files = source_files or {}

    def li_link(label: str, url: str, extra: str = "") -> str:
        return f'<li><a href="{url}" target="_blank" rel="noopener noreferrer">{label}</a>{extra}</li>'

    external = []
    if repo_url:
        external.append(
            li_link(
                "Projectcode — GitHub repository",
                repo_url,
                " — broncode en reproduceerbare workflow.",
            )
        )
    if generated_branch_url:
        external.append(
            li_link(
                "Gegenereerde statische site — gh-pages branch",
                generated_branch_url,
                " — gecommitteerde HTML/CSV-output.",
            )
        )

    external.extend([
        li_link(
            "DNB — Gegevens individuele pensioenfondsen kwartaal",
            "https://www.dnb.nl/statistieken/data-zoeken/#/details/gegevens-individuele-pensioenfondsen-kwartaal",
            " — bron voor kwartaalrendementen risico fonds.",
        ),
        li_link(
            "DNB — Gegevens individuele pensioenfondsen jaar",
            "https://www.dnb.nl/statistieken/data-zoeken/#/details/gegevens-individuele-pensioenfondsen-jaar",
            " — bron voor jaarlijkse kosten/TER-input.",
        ),
        li_link(
            "ABP — Beleggingsresultaten",
            "https://www.abp.nl/over-abp/beleggingen/beleggingsresultaten",
            " — bron voor statische ABP-jaarrendementen in de reconciliatie.",
        ),
        li_link(
            "Ken French Data Library",
            "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html",
            " — optionele factorbron wanneer Ken-French factoren worden gebruikt.",
        ),
    ])

    input_items = []
    for label, path in source_files.items():
        if path:
            input_items.append(f"<li><strong>{label}</strong>: <code>{path}</code></li>")

    inputs_html = "\n".join(input_items) if input_items else '<li class="muted">Geen inputpaden meegegeven.</li>'

    return f"""
    <h3>Externe bronnen</h3>
    <ul>
      {''.join(external)}
    </ul>
    <h3>Gebruikte inputbestanden</h3>
    <ul>
      {inputs_html}
    </ul>
    <p class="muted">
      De downloadknoppen bij tabellen exporteren de rijen en kolommen die op dat moment zichtbaar zijn in het HTML-rapport,
      dus inclusief de huidige fondsenselectie. Flow diagnostics gebruiken premies en deelnemersaantallen als context, niet als rendementsaanpassing.
    </p>
    """



def make_fund_filter_js(fund_list: list[str]) -> str:
    """
    Client-side table + chart filter en CSV-export voor het HTML-rapport.

    Werkt voor:
    - long tables met kolom fund
    - pairwise tables met fund_1/fund_2
    - wide tables waar fondsnamen kolomheaders zijn
    - Apache ECharts charts met dezelfde selected funds
    - per-tabel CSV-download van de zichtbare/gefilterde data
    """
    funds_json = json.dumps(fund_list, ensure_ascii=False)
    return f"""
const ALL_FUNDS = new Set({funds_json});
const ECHARTS_INSTANCES = {{}};

function normalizeFundName(value) {{
  return String(value || '').replace(/\u00a0/g, ' ').replace(/\\s+/g, ' ').trim();
}}

const ALL_FUNDS_NORMALIZED = new Map(Array.from(ALL_FUNDS).map(f => [normalizeFundName(f), f]));

function getSelectedFunds() {{
  const select = document.getElementById('fundSelect');
  if (!select) return new Set(ALL_FUNDS);
  const selected = Array.from(select.selectedOptions).map(o => ALL_FUNDS_NORMALIZED.get(normalizeFundName(o.value)) || normalizeFundName(o.value));
  if (selected.length === 0) return new Set(ALL_FUNDS);
  return new Set(selected);
}}

function getSelectedFundKeys() {{
  return new Set(Array.from(getSelectedFunds()).map(normalizeFundName));
}}

function selectedFundsArray() {{
  return Array.from(getSelectedFunds());
}}

function setAllFunds(selected) {{
  const select = document.getElementById('fundSelect');
  if (!select) return;
  Array.from(select.options).forEach(option => option.selected = selected);
  applyFundFilter();
}}

function headerTexts(table) {{
  const headerRow = table.querySelector('thead tr');
  if (!headerRow) return [];
  return Array.from(headerRow.children).map(th => th.textContent.trim());
}}

function getChart(divId) {{
  if (typeof echarts === 'undefined') return null;
  const el = document.getElementById(divId);
  if (!el) return null;
  if (!ECHARTS_INSTANCES[divId]) {{
    ECHARTS_INSTANCES[divId] = echarts.init(el, null, {{ renderer: 'canvas' }});
  }}
  return ECHARTS_INSTANCES[divId];
}}

function formatPct(value, decimals = 2) {{
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '';
  return Number(value).toFixed(decimals) + '%';
}}

function makeCategoryAxis(dataset, selectedFunds) {{
  const labels = new Set();
  selectedFunds.forEach(fund => {{
    const s = dataset[fund];
    if (s && s.x) s.x.forEach(x => labels.add(String(x)));
  }});
  return Array.from(labels).sort();
}}

function seriesForLine(dataset, selectedFunds, xLabels, asPercent) {{
  return selectedFunds.map(fund => {{
    const s = dataset[fund] || {{ x: [], y: [] }};
    const valueByX = new Map();
    (s.x || []).forEach((x, idx) => {{
      const raw = (s.y || [])[idx];
      valueByX.set(String(x), raw === null ? null : (asPercent ? raw * 100 : raw));
    }});
    return {{
      name: fund,
      type: 'line',
      showSymbol: xLabels.length <= 80,
      symbolSize: 4,
      connectNulls: false,
      data: xLabels.map(x => valueByX.has(String(x)) ? valueByX.get(String(x)) : null),
      emphasis: {{ focus: 'series' }}
    }};
  }});
}}

function renderLineChart(divId, datasetKey, title, yTitle, asPercent) {{
  if (typeof ECHARTS_DATA === 'undefined') return;
  const chart = getChart(divId);
  if (!chart) return;

  const dataset = ECHARTS_DATA[datasetKey] || {{}};
  const selected = selectedFundsArray().filter(f => dataset[f]);
  const xLabels = makeCategoryAxis(dataset, selected);

  const option = {{
    title: {{
      text: title,
      left: 8,
      top: 6,
      textStyle: {{ fontSize: 15, fontWeight: 600 }}
    }},
    tooltip: {{
      trigger: 'axis',
      valueFormatter: value => asPercent ? formatPct(value) : value
    }},
    legend: {{
      type: 'scroll',
      top: 36,
      left: 8,
      right: 8,
      selectedMode: true
    }},
    grid: {{
      left: 64,
      right: 28,
      top: selected.length > 18 ? 98 : 78,
      bottom: 68,
      containLabel: true
    }},
    toolbox: {{
      right: 10,
      feature: {{
        restore: {{}},
        saveAsImage: {{}}
      }}
    }},
    dataZoom: [
      {{ type: 'inside', throttle: 50 }},
      {{ type: 'slider', bottom: 18, height: 20 }}
    ],
    xAxis: {{
      type: 'category',
      data: xLabels,
      axisLabel: {{ rotate: xLabels.length > 14 ? 45 : 0 }}
    }},
    yAxis: {{
      type: 'value',
      name: yTitle,
      axisLabel: {{
        formatter: value => asPercent ? value + '%' : value
      }}
    }},
    series: seriesForLine(dataset, selected, xLabels, asPercent)
  }};

  chart.setOption(option, true);
}}

function renderTerChart() {{
  if (typeof ECHARTS_DATA === 'undefined') return;
  const chart = getChart('chart-ter');
  if (!chart) return;

  const dataset = ECHARTS_DATA.ter || {{}};
  const selected = selectedFundsArray().filter(f => dataset[f]);
  const xLabels = makeCategoryAxis(dataset, selected);

  const option = {{
    title: {{ text: 'TER-like kostenratio per fonds', left: 8, top: 6, textStyle: {{ fontSize: 15, fontWeight: 600 }} }},
    tooltip: {{ trigger: 'axis', valueFormatter: value => formatPct(value) }},
    legend: {{ type: 'scroll', top: 36, left: 8, right: 8 }},
    grid: {{ left: 64, right: 28, top: selected.length > 18 ? 98 : 78, bottom: 68, containLabel: true }},
    toolbox: {{ right: 10, feature: {{ restore: {{}}, saveAsImage: {{}} }} }},
    dataZoom: [
      {{ type: 'inside', throttle: 50 }},
      {{ type: 'slider', bottom: 18, height: 20 }}
    ],
    xAxis: {{ type: 'category', data: xLabels }},
    yAxis: {{
      type: 'value',
      name: 'TER-like kostenratio',
      axisLabel: {{ formatter: value => value + '%' }}
    }},
    series: seriesForLine(dataset, selected, xLabels, true)
  }};

  chart.setOption(option, true);
}}

function renderAlphaChart() {{
  if (typeof ECHARTS_DATA === 'undefined') return;
  const chart = getChart('chart-alpha');
  if (!chart) return;

  const selected = getSelectedFunds();
  const data = (ECHARTS_DATA.alpha || []).filter(row => selected.has(row.fund));
  data.sort((a, b) => (a.alpha_annualized || 0) - (b.alpha_annualized || 0));

  const option = {{
    title: {{ text: 'Jaarlijkse alpha per fonds', left: 8, top: 6, textStyle: {{ fontSize: 15, fontWeight: 600 }} }},
    tooltip: {{
      trigger: 'axis',
      axisPointer: {{ type: 'shadow' }},
      formatter: params => {{
        const p = params && params.length ? params[0] : null;
        if (!p) return '';
        const row = data[p.dataIndex];
        const ci = row && row.alpha_annualized_ci_low !== null && row.alpha_annualized_ci_high !== null
          ? '<br>95% CI: ' + formatPct(Number(row.alpha_annualized_ci_low) * 100) + ' tot ' + formatPct(Number(row.alpha_annualized_ci_high) * 100)
          : '';
        const holm = row && row.p_alpha_holm !== null ? '<br>Holm p=' + Number(row.p_alpha_holm).toFixed(3) : '';
        const nObs = row && row.n_obs !== null ? '<br>n=' + row.n_obs : '';
        return p.name + '<br>Alpha: ' + formatPct(p.value) + ci + holm + nObs;
      }}
    }},
    grid: {{
      left: 190,
      right: 28,
      top: 58,
      bottom: 54,
      containLabel: false
    }},
    toolbox: {{ right: 10, feature: {{ restore: {{}}, saveAsImage: {{}} }} }},
    dataZoom: [
      {{ type: 'inside', yAxisIndex: 0, filterMode: 'none' }},
      {{ type: 'slider', yAxisIndex: 0, right: 4, width: 18, filterMode: 'none' }}
    ],
    xAxis: {{
      type: 'value',
      name: 'Alpha, geannualiseerd',
      axisLabel: {{ formatter: value => value + '%' }}
    }},
    yAxis: {{
      type: 'category',
      data: data.map(row => row.fund),
      axisLabel: {{ fontSize: data.length > 60 ? 8 : 10 }}
    }},
    series: [{{
      type: 'bar',
      data: data.map(row => row.alpha_annualized === null ? null : row.alpha_annualized * 100),
      emphasis: {{ focus: 'series' }}
    }}]
  }};

  chart.setOption(option, true);
}}


function setChartMessage(divId, title, message) {{
  const chart = getChart(divId);
  if (!chart) return;
  chart.clear();
  chart.setOption({{
    title: {{
      text: title,
      left: 8,
      top: 6,
      textStyle: {{ fontSize: 15, fontWeight: 600 }}
    }},
    graphic: [{{
      type: 'text',
      left: 'center',
      top: 'middle',
      style: {{
        text: message,
        fill: '#64748b',
        fontSize: 14,
        textAlign: 'center',
        width: 280,
        overflow: 'break'
      }}
    }}]
  }}, true);
}}

function renderPortfolioMixCharts() {{
  if (typeof ECHARTS_DATA === 'undefined') return;

  const areaTitle = 'Geschatte returns-based portefeuillemix door de tijd';
  const pieTitle = 'Geschatte portefeuillemix in het laatste jaar';
  const dataset = ECHARTS_DATA.portfolio_mix || {{}};
  const selected = selectedFundsArray().filter(f => dataset[f]);

  if (selected.length !== 1) {{
    setChartMessage('chart-portfolio-mix-area', areaTitle, 'Selecteer precies één fonds om deze chart te tonen.');
    setChartMessage('chart-portfolio-mix-pie', pieTitle, 'Selecteer precies één fonds om deze chart te tonen.');
    return;
  }}

  const fund = selected[0];
  const item = dataset[fund];
  if (!item || !item.x || item.x.length === 0 || !item.series) {{
    setChartMessage('chart-portfolio-mix-area', areaTitle, 'Geen returns-based portefeuillemix beschikbaar voor dit fonds.');
    setChartMessage('chart-portfolio-mix-pie', pieTitle, 'Geen returns-based portefeuillemix beschikbaar voor dit fonds.');
    return;
  }}

  const labels = item.x.map(x => String(x));
  const areaChart = getChart('chart-portfolio-mix-area');
  const pieChart = getChart('chart-portfolio-mix-pie');
  if (!areaChart || !pieChart) return;

  const seriesNames = Object.keys(item.series);
  const areaSeries = seriesNames.map(name => {{
    const values = (item.series[name] || []).map(v => v === null ? null : Number(v) * 100);
    return {{
      name,
      type: 'line',
      stack: 'mix',
      areaStyle: {{}},
      showSymbol: labels.length <= 15,
      symbolSize: 5,
      connectNulls: false,
      emphasis: {{ focus: 'series' }},
      data: values
    }};
  }});

  areaChart.setOption({{
    title: {{
      text: areaTitle + ' — ' + fund,
      subtext: item.rolling_window_quarters ? ('rolling window: laatste ' + item.rolling_window_quarters + ' kwartalen') : '',
      left: 8,
      top: 6,
      textStyle: {{ fontSize: 15, fontWeight: 600 }}
    }},
    tooltip: {{
      trigger: 'axis',
      valueFormatter: value => formatPct(value)
    }},
    legend: {{
      type: 'scroll',
      top: 40,
      left: 8,
      right: 8
    }},
    grid: {{
      left: 68,
      right: 28,
      top: 92,
      bottom: 58,
      containLabel: true
    }},
    toolbox: {{
      right: 10,
      feature: {{
        restore: {{}},
        saveAsImage: {{}}
      }}
    }},
    xAxis: {{
      type: 'category',
      data: labels
    }},
    yAxis: {{
      type: 'value',
      min: 0,
      max: 100,
      name: 'Geschat aandeel',
      axisLabel: {{
        formatter: value => value + '%'
      }}
    }},
    series: areaSeries
  }}, true);

  const lastIndex = labels.length - 1;
  const lastYear = item.last_year || labels[lastIndex];
  const pieData = seriesNames.map(name => {{
    const arr = item.series[name] || [];
    const raw = arr[lastIndex];
    return {{
      name,
      value: raw === null ? 0 : Number(raw) * 100
    }};
  }}).filter(row => row.value > 0);

  pieChart.setOption({{
    title: {{
      text: pieTitle + ' — ' + fund,
      subtext: lastYear ? String(lastYear) : '',
      left: 8,
      top: 6,
      textStyle: {{ fontSize: 15, fontWeight: 600 }}
    }},
    tooltip: {{
      trigger: 'item',
      valueFormatter: value => formatPct(value)
    }},
    legend: {{
      type: 'scroll',
      bottom: 8,
      left: 'center'
    }},
    toolbox: {{
      right: 10,
      feature: {{
        restore: {{}},
        saveAsImage: {{}}
      }}
    }},
    series: [{{
      name: 'Portefeuillemix',
      type: 'pie',
      radius: ['35%', '70%'],
      center: ['50%', '54%'],
      minAngle: 4,
      itemStyle: {{
        borderRadius: 6,
        borderColor: '#fff',
        borderWidth: 1
      }},
      label: {{
        formatter: params => params.name + '\\n' + params.percent + '%'
      }},
      data: pieData
    }}]
  }}, true);
}}

function updateCharts() {{
  renderLineChart('chart-cumulative-raw', 'cumulative_raw', 'Cumulatieve ruwe rendementen', 'Cumulatief rendement', true);
  renderLineChart('chart-cumulative-net', 'cumulative_net', 'Cumulatieve rendementen na TER', 'Cumulatief rendement', true);
  renderLineChart('chart-quarterly-raw', 'quarterly_raw', 'Ruwe kwartaalrendementen', 'Kwartaalrendement', true);
  renderLineChart('chart-quarterly-net', 'quarterly_net', 'Kwartaalrendementen na TER', 'Kwartaalrendement', true);
  renderTerChart();
  renderAlphaChart();
  renderPortfolioMixCharts();

  Object.values(ECHARTS_INSTANCES).forEach(chart => {{
    if (chart && chart.resize) chart.resize();
  }});
}}

function isVisibleElement(el) {{
  return !!(el && el.offsetParent !== null && getComputedStyle(el).display !== 'none');
}}

function csvEscape(value) {{
  const text = String(value ?? '').replace(/\\s+/g, ' ').trim();
  if (/[",\\n\\r;]/.test(text)) {{
    return '"' + text.replace(/"/g, '""') + '"';
  }}
  return text;
}}

function tableToVisibleCsv(table) {{
  const lines = [];
  const rows = Array.from(table.querySelectorAll('thead tr, tbody tr'));

  rows.forEach(row => {{
    if (!isVisibleElement(row)) return;
    const cells = Array.from(row.children).filter(cell => isVisibleElement(cell));
    if (!cells.length) return;
    lines.push(cells.map(cell => csvEscape(cell.textContent)).join(','));
  }});

  return lines.join('\\n') + '\\n';
}}

function slugify(text) {{
  return String(text || 'table')
    .toLowerCase()
    .replace(/[^a-z0-9\\u00C0-\\u024f]+/gi, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 80) || 'table';
}}

function titleForTable(table, index) {{
  const section = table.closest('section');
  const h2 = section ? section.querySelector('h2') : null;
  let h3 = null;
  if (section) {{
    const headings = Array.from(section.querySelectorAll('h3'));
    const tableTop = table.getBoundingClientRect().top;
    h3 = headings.reverse().find(h => h.getBoundingClientRect().top < tableTop);
  }}
  const title = [h2 ? h2.textContent.trim() : '', h3 ? h3.textContent.trim() : '']
    .filter(Boolean)
    .join(' - ');
  return title || `tabel-${{index + 1}}`;
}}

function downloadText(filename, text, mimeType) {{
  const blob = new Blob([text], {{ type: mimeType }});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}}

function addTableDownloadButtons() {{
  const wrappers = Array.from(document.querySelectorAll('.table-wrap'));
  wrappers.forEach((wrap, index) => {{
    const table = wrap.querySelector('table.data-table');
    if (!table || wrap.dataset.downloadReady === '1') return;

    const title = titleForTable(table, index);
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'table-download-button';
    btn.textContent = 'Download geselecteerde CSV';
    btn.title = 'Download de rijen en kolommen die nu zichtbaar zijn na fondsfiltering';

    btn.addEventListener('click', () => {{
      const csv = tableToVisibleCsv(table);
      const selectedCount = getSelectedFunds().size;
      const date = new Date().toISOString().slice(0, 10);
      const filename = `${{slugify(title)}}-selected-${{selectedCount}}-funds-${{date}}.csv`;
      downloadText(filename, csv, 'text/csv;charset=utf-8');
    }});

    const bar = document.createElement('div');
    bar.className = 'table-download-bar';
    const label = document.createElement('span');
    label.className = 'muted';
    label.textContent = title;
    bar.appendChild(btn);
    bar.appendChild(label);

    wrap.parentNode.insertBefore(bar, wrap);
    wrap.dataset.downloadReady = '1';
  }});
}}

function applyFundFilter() {{
  const selected = getSelectedFunds();
  const selectedKeys = getSelectedFundKeys();
  const tables = document.querySelectorAll('table.data-table');

  tables.forEach(table => {{
    const headers = headerTexts(table);
    const fundCol = headers.indexOf('fund');
    const fund1Col = headers.indexOf('fund_1');
    const fund2Col = headers.indexOf('fund_2');

    const columnKeep = headers.map(h => !ALL_FUNDS_NORMALIZED.has(normalizeFundName(h)) || selectedKeys.has(normalizeFundName(h)));
    Array.from(table.rows).forEach(row => {{
      Array.from(row.children).forEach((cell, idx) => {{
        cell.style.display = columnKeep[idx] === false ? 'none' : '';
      }});
    }});

    const bodyRows = table.querySelectorAll('tbody tr');
    bodyRows.forEach(row => {{
      const cells = Array.from(row.children);
      let show = true;

      if (fundCol >= 0 && cells[fundCol]) {{
        const valueKey = normalizeFundName(cells[fundCol].textContent);
        if (ALL_FUNDS_NORMALIZED.has(valueKey)) show = selectedKeys.has(valueKey);
      }}

      if (fund1Col >= 0 && fund2Col >= 0 && cells[fund1Col] && cells[fund2Col]) {{
        const f1Key = normalizeFundName(cells[fund1Col].textContent);
        const f2Key = normalizeFundName(cells[fund2Col].textContent);
        if (ALL_FUNDS_NORMALIZED.has(f1Key) || ALL_FUNDS_NORMALIZED.has(f2Key)) {{
          show = selectedKeys.has(f1Key) && selectedKeys.has(f2Key);
        }}
      }}

      row.style.display = show ? '' : 'none';
    }});

    const wrapper = table.closest('.table-wrap');
    if (wrapper) {{
      let note = wrapper.querySelector('.table-empty-note');
      if (!note) {{
        note = document.createElement('div');
        note.className = 'table-empty-note';
        note.textContent = 'Geen zichtbare rijen voor de huidige fondsselectie in deze tabel.';
        wrapper.appendChild(note);
      }}
      const visibleRows = Array.from(bodyRows).filter(row => row.style.display !== 'none').length;
      note.style.display = visibleRows === 0 ? 'block' : 'none';
    }}
  }});

  const count = document.getElementById('selectedFundCount');
  if (count) count.textContent = `${{selected.size}} / ${{ALL_FUNDS.size}} geselecteerd`;

  updateCharts();
}}

window.addEventListener('resize', () => {{
  Object.values(ECHARTS_INSTANCES).forEach(chart => {{
    if (chart && chart.resize) chart.resize();
  }});
}});

document.addEventListener('DOMContentLoaded', () => {{
  addTableDownloadButtons();

  const select = document.getElementById('fundSelect');
  if (select) {{
    Array.from(select.options).forEach(option => option.selected = true);
    select.addEventListener('change', applyFundFilter);
  }}
  applyFundFilter();
}});
"""




def summarize_flow_diagnostics(flow: pd.DataFrame | None) -> pd.DataFrame:
    if flow is None or flow.empty:
        return pd.DataFrame()

    rows = []
    for fund, data in flow.groupby("fund"):
        data = data.sort_values("year")
        latest = data.iloc[-1]
        rows.append({
            "fund": fund,
            "first_year": int(data["year"].dropna().min()) if data["year"].notna().any() else pd.NA,
            "last_year": int(data["year"].dropna().max()) if data["year"].notna().any() else pd.NA,
            "latest_total_participants": latest.get("total_participants", np.nan),
            "latest_active_participants": latest.get("active_participants", np.nan),
            "latest_pensioners": latest.get("pensioners", np.nan),
            "latest_active_ratio": latest.get("active_ratio", np.nan),
            "latest_pensioner_ratio": latest.get("pensioner_ratio", np.nan),
            "latest_dependency_ratio": latest.get("dependency_ratio_pensioners_to_active", np.nan),
            "latest_total_premium": latest.get("total_premium", np.nan),
            "latest_premium_per_active_thousand_eur": latest.get("premium_per_active_participant_thousand_eur", np.nan),
            "participant_growth_since_first": (
                latest.get("total_participants", np.nan) / data.iloc[0].get("total_participants", np.nan) - 1.0
                if pd.notna(latest.get("total_participants", np.nan))
                and pd.notna(data.iloc[0].get("total_participants", np.nan))
                and data.iloc[0].get("total_participants", 0) != 0
                else np.nan
            ),
            "avg_annual_participant_growth": data.get("participant_growth", pd.Series(dtype=float)).mean(skipna=True),
            "avg_annual_premium_growth": data.get("total_premium_growth", pd.Series(dtype=float)).mean(skipna=True),
            "n_years": int(data["year"].nunique()),
        })
    return pd.DataFrame(rows).sort_values("fund").reset_index(drop=True)


def make_html_report(
    output_dir: Path,
    calculation_base: pd.DataFrame,
    ter_long: pd.DataFrame,
    ter_wide: pd.DataFrame,
    alpha: pd.DataFrame,
    pairwise: pd.DataFrame,
    data_quality: pd.DataFrame,
    annual_reconciliation: pd.DataFrame | None = None,
    corrected_annual_returns: pd.DataFrame | None = None,
    flow_diagnostics: pd.DataFrame | None = None,
    portfolio_exposure_diagnostics: pd.DataFrame | None = None,
    source_files: dict[str, str | None] | None = None,
    calculation_base_returns_display: pd.DataFrame | None = None,
    analysis_end_period: str | None = None,
    returns_display_end_period: str | None = None,
    repo_url: str | None = None,
    generated_branch_url: str | None = None,
) -> Path:
    returns_display_base = (
        calculation_base_returns_display
        if calculation_base_returns_display is not None and not calculation_base_returns_display.empty
        else calculation_base
    )

    fig_dir = output_dir / "figures"
    fig_dir.mkdir(parents=True, exist_ok=True)

    raw_chart = fig_dir / "cumulative_returns_raw.png"
    net_chart = fig_dir / "cumulative_returns_after_ter.png"
    raw_quarterly_chart = fig_dir / "quarterly_returns_raw.png"
    net_quarterly_chart = fig_dir / "quarterly_returns_after_ter.png"
    ter_chart = fig_dir / "ter_by_fund.png"
    alpha_chart = fig_dir / "alpha_by_fund.png"

    def img(path: Path, alt: str) -> str:
        rel = path.relative_to(output_dir).as_posix()
        return f'<img class="chart" src="{rel}" alt="{alt}">'

    save_cumulative_chart(returns_display_base, "return_quarterly", raw_chart, "Cumulatieve ruwe kwartaalrendementen")
    save_cumulative_chart(returns_display_base, "return_after_ter", net_chart, "Cumulatieve rendementen na TER-correctie")
    save_quarterly_return_chart(returns_display_base, "return_quarterly", raw_quarterly_chart, "Ruwe kwartaalrendementen per fonds")
    save_quarterly_return_chart(returns_display_base, "return_after_ter", net_quarterly_chart, "Kwartaalrendementen na TER per fonds")
    has_ter_chart = save_ter_chart(ter_long, ter_chart)
    has_alpha_chart = save_alpha_chart(alpha, alpha_chart)

    n_funds = returns_display_base["fund"].nunique()
    n_periods = returns_display_base["period"].nunique()
    period_min = returns_display_base["period"].min()
    period_max = returns_display_base["period"].max()
    analysis_period_min = calculation_base["period"].min()
    analysis_period_max = calculation_base["period"].max()

    raw_summary = summarize_returns(returns_display_base, "return_quarterly")
    net_summary = summarize_returns(returns_display_base, "return_after_ter")
    flow_summary = summarize_flow_diagnostics(flow_diagnostics)
    raw_returns_pct_wide = make_returns_percent_wide(returns_display_base, "return_quarterly")
    net_returns_pct_wide = make_returns_percent_wide(returns_display_base, "return_after_ter")
    portfolio_mix_timeseries = make_estimated_portfolio_mix_timeseries(calculation_base)

    fund_list = sorted(returns_display_base["fund"].dropna().astype(str).unique().tolist())
    fund_options_html = "\n".join(
        f'<option value="{escape_html(fund, quote=True)}">{escape_html(fund)}</option>'
        for fund in fund_list
    )
    fund_filter_js = make_fund_filter_js(fund_list)
    echarts_payload = make_echarts_chart_payload(returns_display_base, ter_long, alpha, portfolio_mix_timeseries)
    echarts_payload_json = json.dumps(echarts_payload, ensure_ascii=False)
    sources_html = make_sources_html(source_files, repo_url=repo_url, generated_branch_url=generated_branch_url)

    project_links = []
    if repo_url:
        project_links.append(f'<a href="{escape_html(repo_url)}" target="_blank" rel="noopener noreferrer">GitHub code</a>')
    if generated_branch_url:
        project_links.append(f'<a href="{escape_html(generated_branch_url)}" target="_blank" rel="noopener noreferrer">gegenereerde gh-pages branch</a>')
    project_links_html = ""
    if project_links:
        project_links_html = '<p class="project-links">' + " · ".join(project_links) + "</p>"

    if has_alpha_chart:
        alpha_section_html = f"""
      <p>
        <strong>Technisch:</strong> alpha is de intercept uit de factorregressie met HAC/Newey-West standaardfouten.
        De standaardfouten corrigeren voor autocorrelatie en heteroskedasticiteit in kwartaalrendementen.
      </p>
      <div class='formula-note'>
        <p><strong>In gewone taal:</strong></p>
        <p>
          Het model probeert eerst te verklaren welk rendement je zou verwachten op basis van de gemeten marktgevoeligheden:
          aandelen, duration/rente, credit, vastgoed en valuta. Wat daarna gemiddeld overblijft, noemen we <strong>alpha</strong>.
        </p>
        <ul>
          <li><strong>Positieve alpha:</strong> het fonds deed het beter dan je op basis van de gekozen factoren zou verwachten.</li>
          <li><strong>Negatieve alpha:</strong> het fonds deed het slechter dan je op basis van de gekozen factoren zou verwachten.</li>
          <li><strong>Alpha rond nul:</strong> het rendement is grotendeels verklaarbaar door de factorblootstellingen.</li>
          <li><strong>Lage p_alpha_holm:</strong> het signaal blijft ook na correctie voor veel gelijktijdige tests statistisch opvallend.</li>
        </ul>
        <p class='muted'>
          Alpha is dus geen direct bewijs van beleggingsvaardigheid. Het is een returns-based restterm binnen dit gekozen model.
          Een hoge of lage alpha kan ook ontstaan door missende factoren, datakwaliteit, definitiewijzigingen, kostenverschillen,
          illiquide beleggingen of een portefeuille die anders is dan de proxyfactoren.
        </p>
      </div>
      <div id='chart-alpha' class='echarts-chart tall'></div>
      <div class='fallback-static'>{img(alpha_chart, "Alpha per fonds")}</div>
    """
    else:
        alpha_section_html = '<p class="muted">Geen alpha-resultaten; geen factorbestand of onvoldoende data.</p>'

    if portfolio_mix_timeseries is not None and not portfolio_mix_timeseries.empty:
        portfolio_mix_section_html = """
    <div class="formula-note">
      <p><strong>Extra visualisatie bij selectie van één fonds:</strong></p>
      <p>
        Onderstaande charts tonen een <em>returns-based schatting</em> van de exposuremix. Per jaar schatten we een rolling regressie
        over de laatste 12 kwartalen. Daarna normaliseren we alleen de niet-negatieve componenten tot 100%.
      </p>
      <p><code>obligaties_share = max(beta_duration, 0) + max(beta_credit, 0)</code></p>
      <p><code>normalized_share_k = positive_component_k / sum(all positive components)</code></p>
      <p class="muted">
        Lees dit als een grove indicatie van marktgevoeligheden, niet als de echte holdings-allocatie.
        Derivaten, renteafdekking, illiquide beleggingen en modelkeuzes kunnen het beeld vertekenen.
      </p>
    </div>
    <div class="chart-grid-two">
      <div class="chart-panel">
        <h3>Geschatte mix door de tijd</h3>
        <div id="chart-portfolio-mix-area" class="echarts-chart"></div>
      </div>
      <div class="chart-panel">
        <h3>Geschatte mix in het laatste jaar</h3>
        <div id="chart-portfolio-mix-pie" class="echarts-chart"></div>
      </div>
    </div>
"""
    else:
        portfolio_mix_section_html = '<p class="muted">Geen returns-based portefeuillemix beschikbaar; de pension-factoren ontbreken of er is onvoldoende data per fonds.</p>'

    css = """
    :root { color-scheme: light; --page-pad:clamp(14px,3vw,32px); --radius:16px; }
    * { box-sizing:border-box; }
    html { overflow-y:auto; }
    body { margin:0; background:#f6f8fb; color:#172033; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif; line-height:1.45; }
    .container { width:min(100% - 2 * var(--page-pad), 1440px); margin:0 auto; padding:var(--page-pad) 0 56px; }
    .hero { background:linear-gradient(135deg,#0f2f4f,#174b7a); color:white; padding:clamp(24px,4vw,38px); border-radius:22px; margin-bottom:24px; box-shadow:0 18px 48px rgba(15,47,79,.18); }
    .hero h1 { margin:0 0 8px; font-size:clamp(24px,4vw,30px); } .hero p { margin:0; color:rgba(255,255,255,.82); }
    .hero .project-links { margin-top:12px; display:flex; gap:14px; flex-wrap:wrap; }
    .hero .project-links a { color:#bfdbfe; font-weight:700; text-decoration:none; border-bottom:1px solid rgba(191,219,254,.65); }
    .hero .project-links a:hover { color:white; border-bottom-color:white; }
    .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:16px; margin-bottom:24px; }
    .card, section { background:white; border:1px solid #d9e0ea; border-radius:var(--radius); padding:clamp(16px,2.4vw,22px); box-shadow:0 2px 12px rgba(16,24,40,.04); }
    .methodology-disclaimer { background:#fff7ed; border-color:#fed7aa; color:#431407; }
    .methodology-disclaimer h2 { color:#9a3412; }
    .methodology-disclaimer ul { margin-bottom:0; }
    .dnb-disclaimer { background:#eff6ff; border-color:#bfdbfe; color:#172554; }
    .dnb-disclaimer h2 { color:#1d4ed8; }
    .period-note { background:#f8fafc; border:1px solid #e2e8f0; border-radius:12px; padding:12px 14px; margin:12px 0; }
    .formula-note { background:#f8fafc; border-left:4px solid #93c5fd; padding:12px 14px; margin:12px 0 16px; border-radius:10px; }
    .formula-note p { margin:6px 0; }
    .formula-note ul { margin:6px 0 0 20px; padding:0; }
    section { margin-bottom:24px; scroll-margin-top:18px; } h2 { color:#123c69; margin:0 0 12px; letter-spacing:-.015em; } h3 { color:#123c69; margin-top:18px; }
    section > p { max-width:86ch; }
    .metric-label { color:#667085; font-size:13px; margin-bottom:6px; } .metric-value { color:#123c69; font-size:22px; font-weight:700; }
    .muted { color:#667085; font-size:14px; } code { background:#eef4fb; padding:2px 5px; border-radius:5px; }
    .chart { width:100%; max-width:1100px; border:1px solid #d9e0ea; border-radius:12px; background:white; padding:8px; }
    .echarts-chart { width:100%; min-height:clamp(340px,54vh,520px); border:1px solid #d9e0ea; border-radius:12px; background:white; padding:8px; margin-top:12px; }
    .echarts-chart.tall { min-height:clamp(440px,70vh,680px); }
    .chart-grid-two { display:grid; grid-template-columns:minmax(0,1.5fr) minmax(320px,1fr); gap:16px; align-items:start; margin-top:14px; }
    .chart-panel { min-width:0; }
    .chart-panel h3 { margin-bottom:8px; }
    .fallback-static { display:none; }
    .table-section { display:flex; flex-direction:column; gap:22px; }
    .stacked-tables { display:flex; flex-direction:column; gap:22px; }
    .table-panel { min-width:0; }
    .table-wrap {
      width:100%;
      max-width:100%;
      max-height:calc(100vh - 112px);
      overflow:auto;
      overscroll-behavior:contain;
      -webkit-overflow-scrolling:touch;
      border:1px solid #d9e0ea;
      border-radius:14px;
      margin-top:12px;
      background:white;
      box-shadow:inset 0 1px 0 rgba(255,255,255,.75);
      scrollbar-gutter:stable both-edges;
    }
    .table-wrap.compact-vertical {
      max-height:calc(100vh - 112px);
      overflow:auto;
    }
    table.data-table { border-collapse:separate; border-spacing:0; width:max-content; min-width:100%; font-size:13px; background:white; }
    .data-table th {
      position:sticky;
      top:0;
      z-index:3;
      background:rgba(238,244,251,.96);
      backdrop-filter:saturate(180%) blur(12px);
      color:#123c69;
      text-align:left;
      padding:10px 12px;
      border-bottom:1px solid #d9e0ea;
      white-space:nowrap;
      font-weight:700;
    }
    .data-table td { padding:8px 12px; border-bottom:1px solid #edf1f7; white-space:nowrap; vertical-align:top; }
    .data-table tbody tr:nth-child(even) td { background:#fbfdff; }
    .data-table tbody tr:hover td { background:#f1f7ff; }
    .fund-filter { position:sticky; top:12px; z-index:10; background:rgba(255,255,255,.94); backdrop-filter:saturate(180%) blur(18px); border:1px solid #d9e0ea; border-radius:18px; padding:18px; margin-bottom:24px; box-shadow:0 10px 32px rgba(16,24,40,.08); }
    .fund-filter select { width:100%; min-height:clamp(132px,22vh,210px); border:1px solid #cbd5e1; border-radius:12px; padding:8px; font-size:14px; background:white; }
    .filter-actions { display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-top:10px; }
    .filter-actions button { background:#123c69; color:white; border:0; border-radius:9px; padding:8px 12px; cursor:pointer; }
    .filter-actions button.secondary { background:#eef4fb; color:#123c69; border:1px solid #cbd5e1; }
    .table-download-bar { display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin:14px 0 8px; }
    .table-download-button { background:#123c69; color:white; border:0; border-radius:9px; padding:7px 11px; cursor:pointer; font-size:13px; }
    .table-download-button:hover { background:#0f2f4f; }
    .table-empty-note { display:none; padding:10px 12px; margin:10px 0 0; background:#fff7ed; color:#7c2d12; border:1px solid #fed7aa; border-radius:10px; font-size:13px; }
    .sources-list a { color:#123c69; }
    .sources-list li { margin:6px 0; }
    details.native-disclosure { border:1px solid #e2e8f0; border-radius:14px; padding:0; background:#fbfdff; margin:12px 0 18px; overflow:hidden; }
    details.native-disclosure > summary { cursor:pointer; user-select:none; list-style:none; padding:13px 16px; font-weight:700; color:#123c69; background:#f8fafc; }
    details.native-disclosure > summary::-webkit-details-marker { display:none; }
    details.native-disclosure > summary::after { content:"＋"; float:right; color:#64748b; }
    details.native-disclosure[open] > summary::after { content:"−"; }
    details.native-disclosure > .details-body { padding:14px 16px 16px; border-top:1px solid #e2e8f0; }
    .two-col { display:grid; grid-template-columns:repeat(auto-fit,minmax(min(100%,360px),1fr)); gap:18px; }
    @media (max-width:700px) {
      .container { width:100%; padding:14px 10px 42px; }
      .hero, .card, section, .fund-filter { border-radius:14px; }
      .fund-filter { position:static; }
      .chart-grid-two { grid-template-columns:1fr; }
      .table-wrap { margin-left:-2px; margin-right:-2px; max-height:82vh; border-radius:12px; }
      .data-table th, .data-table td { padding:7px 8px; font-size:12px; }
      .echarts-chart { min-height:340px; }
    }
    @media (prefers-reduced-motion: reduce) {
      * { scroll-behavior:auto !important; transition:none !important; animation:none !important; }
    }
    """

    html = f"""<!doctype html>
<html lang="nl">
<head>
  <meta charset="utf-8">
  <title>Pensioenfonds alpha-analyse</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>{css}</style>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/echarts/6.0.0/echarts.min.js" charset="utf-8"></script>
</head>
<body>
<div class="container">
  <div class="hero">
    <h1>Pensioenfonds alpha-analyse</h1>
    <p>Processingrapport op basis van gestandaardiseerde CSV-input.</p>
    {project_links_html}
  </div>

  <section class="methodology-disclaimer">
    <h2>Methodologische disclaimer</h2>
    <p>
      Deze analyse is bedoeld als exploratieve, returns-based vergelijking van pensioenfondsrendementen.
      Pensioenfondsrendementen zijn niet één-op-één vergelijkbaar met indexfondsen: pensioenfondsen hebben andere doelstellingen,
      verplichtingen, rente- en valutarisico's, liquiditeitsbehoeften, kostenstructuren en deelnemerspopulaties.
    </p>
    <ul>
      <li>Uit historische rendementen en alpha's kunnen geen harde conclusies over toekomstig resultaat of beleggingskwaliteit worden getrokken.</li>
      <li>Ook onderlinge vergelijking van pensioenfondsen is lastig: leeftijdsopbouw, rijpheid van het fonds, risicohouding en verplichtingen verschillen per fonds.</li>
      <li>Een fonds met oudere deelnemers zal rationeel vaak minder risico nemen dan een fonds met relatief jonge deelnemers; lager rendement kan dan deels beleidsmatig verklaarbaar zijn.</li>
      <li>De alpha in dit rapport is een factor-adjusted residual return op basis van geschatte rendementsexposures; het is geen volledige holdings-based performance-attributie.</li>
      <li>Flow- en deelnemersdiagnostics zijn toegevoegd als contextvariabelen en corrigeren de rendementen niet rechtstreeks.</li>
    </ul>
  </section>

  <section class="dnb-disclaimer">
    <h2>DNB-datakanttekeningen</h2>
    <p>
      De DNB-tabellen publiceren sinds 2015 een vaste set individuele pensioenfondsgegevens uit FTK-verslagstaten.
      De aangeleverde gegevens worden door DNB ongewijzigd overgenomen; DNB voert geen berekeningen of bewerkingen uit op deze aangeleverde data.
      Het doel van de publicatie is inzicht geven in de financiële situatie van een fonds op een specifiek moment.
    </p>
    <ul>
      <li>De cijfers kunnen afwijken van publicaties van pensioenfondsen zelf, bijvoorbeeld door andere definities in het FTK-rapportagekader.</li>
      <li>Variabelen kunnen door de tijd minder goed vergelijkbaar zijn door definitiewijzigingen, aanscherpingen of gewijzigde berekeningswijzen; daardoor kunnen statistische reeksbreuken ontstaan.</li>
      <li>Uitvoeringskosten worden gepubliceerd zoals fondsen die ook in eigen publicaties opnemen; niet alle fondsen hoeven exact dezelfde definitie te hanteren.</li>
      <li>Niet alle pensioenfondsen staan in de tabellen, onder meer vanwege privacy bij fondsen met minder dan 100 deelnemers of bij vergevorderde liquidatie.</li>
    </ul>
  </section>

  <div class="grid">
    <div class="card"><div class="metric-label">Fondsen in rendementen</div><div class="metric-value">{n_funds}</div></div>
    <div class="card"><div class="metric-label">Rendementskwartalen</div><div class="metric-value">{n_periods}</div></div>
    <div class="card"><div class="metric-label">Rendementsperiode</div><div class="metric-value">{period_min}–{period_max}</div></div>
    <div class="card"><div class="metric-label">Analyseperiode</div><div class="metric-value">{analysis_period_min}–{analysis_period_max}</div></div>
    <div class="card"><div class="metric-label">Regressies</div><div class="metric-value">{len(alpha) if alpha is not None else 0}</div></div>
  </div>

  <div class="fund-filter">
    <h2>Fondsenfilter voor tabellen</h2>
    <p class="muted">
      Kies één of meerdere fondsen. Gebruik Ctrl/Cmd of Shift voor multi-select.
      De filter werkt op alle HTML-tabellen én interactieve grafieken.
    </p>
    <select id="fundSelect" multiple>
      {fund_options_html}
    </select>
    <div class="filter-actions">
      <button type="button" onclick="setAllFunds(true)">Selecteer alles</button>
      <button type="button" class="secondary" onclick="setAllFunds(false)">Maak selectie leeg</button>
      <span id="selectedFundCount" class="muted"></span>
    </div>
  </div>

  <section>
    <h2>Data quality checks</h2>
    <div class="table-wrap">{df_to_html_table(data_quality, max_rows=50)}</div>
  </section>

  <section>
    <h2>Samenvatting</h2>
    <p>De analyse gebruikt <code>returns_quarterly.csv</code>, optioneel <code>ter_annual.csv</code> en optioneel <code>factors.csv</code>.</p>
    <div class="period-note">
      <strong>Periode-scheiding:</strong>
      de rendementstabellen en rendementsgrafieken tonen <code>{period_min}–{period_max}</code>.
      Alpha, pairwise alpha, jaarlijkse reconciliatie, data quality en audit/rekenbasis gebruiken <code>{analysis_period_min}–{analysis_period_max}</code>.
    </div>
    <div class="formula-note">
      <p><strong>Hoofdformule regressie:</strong></p>
      <p><code>r_after_ter[i,t] = r_reported[i,t] - ter_quarterly[i,t]</code></p>
      <p><code>excess_return[i,t] = r_after_ter[i,t] - rf[t]</code></p>
      <p><code>excess_return[i,t] = alpha[i] + beta[i]' * factors[t] + epsilon[i,t]</code></p>
      <p class="muted">Alle rendementen in de berekening zijn decimalen: <code>0.025 = 2.5%</code>.</p>
    </div>
  </section>

  <section>
    <h2>Cumulatieve rendementen</h2>
    <p class="muted">Interactieve grafieken volgen dezelfde fondsenselectie als de tabellen.</p>
    <div class="formula-note">
      <p><strong>Formules:</strong></p>
      <p><code>cum_return[t] = product(1 + r[q]) - 1</code> voor alle kwartalen <code>q</code> tot en met <code>t</code>.</p>
      <p><code>cum_return_after_ter[t] = product(1 + r_after_ter[q]) - 1</code>.</p>
    </div>
    <h3>Ruwe kwartaalrendementen</h3>
    <div id="chart-cumulative-raw" class="echarts-chart"></div>
    <div class="fallback-static">{img(raw_chart, "Ruwe cumulatieve rendementen")}</div>
    <h3>Na TER-correctie</h3>
    <div id="chart-cumulative-net" class="echarts-chart"></div>
    <div class="fallback-static">{img(net_chart, "Cumulatieve rendementen na TER")}</div>
  </section>

  <section>
    <h2>Kwartaalrendementen in %</h2>
    <p>
      Deze sectie toont de gewone, niet-cumulatieve rendementen per kwartaal en mag verder lopen dan de alpha-analyse.
      De waarden in de tabellen zijn percentages: <code>2.34%</code> betekent een kwartaalrendement van <code>0.0234</code> in de CSV.
      Let op: rendementen na TER gebruiken het gekozen TER-missingbeleid wanneer jaarkosten voor de nieuwste jaren nog ontbreken.
    </p>
    <div class="formula-note">
      <p><strong>Formules:</strong></p>
      <p><code>quarterly_return_percent = 100 * r_reported</code></p>
      <p><code>ter_quarterly = (1 + ter_annual)^(1/4) - 1</code></p>
      <p><code>return_after_ter = r_reported - ter_quarterly</code></p>
    </div>
    <h3>Ruwe kwartaalrendementen</h3>
    <div id="chart-quarterly-raw" class="echarts-chart"></div>
    <div class="fallback-static">{img(raw_quarterly_chart, "Ruwe kwartaalrendementen per fonds")}</div>
    <div class="table-wrap">{df_to_html_percent_table(raw_returns_pct_wide, max_rows=120)}</div>

    <h3>Kwartaalrendementen na TER</h3>
    <div id="chart-quarterly-net" class="echarts-chart"></div>
    <div class="fallback-static">{img(net_quarterly_chart, "Kwartaalrendementen na TER per fonds")}</div>
    <div class="table-wrap">{df_to_html_percent_table(net_returns_pct_wide, max_rows=120)}</div>
  </section>

  <section>
    <h2>Rendementssamenvatting</h2>
    <div class="formula-note">
      <p><strong>Formules per fonds:</strong></p>
      <p><code>cumulative_return = product(1 + r[t]) - 1</code></p>
      <p><code>annualized_return = (1 + cumulative_return)^(4 / n_quarters) - 1</code></p>
      <p><code>quarterly_volatility = standard_deviation(r[t])</code></p>
      <p class="muted">De volatiliteit is kwartaalvolatiliteit, niet geannualiseerd.</p>
    </div>
    <div class="stacked-tables">
      <div class="table-panel">
        <h3>Ruw</h3>
        <div class="table-wrap">{df_to_html_table(raw_summary, max_rows=None, percent_cols=["cumulative_return","annualized_return","quarterly_volatility"])}</div>
      </div>
      <div class="table-panel">
        <h3>Na TER</h3>
        <div class="table-wrap">{df_to_html_table(net_summary, max_rows=None, percent_cols=["cumulative_return","annualized_return","quarterly_volatility"])}</div>
      </div>
    </div>
  </section>

  <section>
    <h2>Flow- en deelnemersdiagnostics</h2>
    <p>
      Deze sectie gebruikt jaargegevens zoals premies en deelnemersaantallen als context voor schaal, groei en fondsrijpheid.
      Dit corrigeert de rendementen niet direct, maar helpt verklaren of lage/hoge alpha samenhangt met instroom, uitstroom,
      gesloten fondsen, rijpere deelnemerspopulaties of schaalverschillen.
    </p>
    <div class="formula-note">
      <p><strong>Formules:</strong></p>
      <p><code>active_ratio = active_participants / total_participants</code></p>
      <p><code>pensioner_ratio = pensioners / total_participants</code></p>
      <p><code>dependency_ratio = pensioners / active_participants</code></p>
      <p><code>participant_growth[t] = total_participants[t] / total_participants[t-1] - 1</code></p>
      <p><code>premium_per_active = total_premium / active_participants</code></p>
    </div>
    {('<h3>Samenvatting per fonds</h3><div class="table-wrap">' + df_to_html_table(flow_summary, max_rows=None, percent_cols=["latest_active_ratio","latest_pensioner_ratio","participant_growth_since_first","avg_annual_participant_growth","avg_annual_premium_growth"], float_cols=["latest_dependency_ratio","latest_premium_per_active_thousand_eur"]) + '</div><h3>Jaarlijkse flow-diagnostics</h3><div class="table-wrap compact-vertical">' + df_to_html_table(flow_diagnostics, max_rows=None, percent_cols=["active_ratio","deferred_ratio","pensioner_ratio","participant_growth","active_participant_growth","total_premium_growth"], float_cols=["dependency_ratio_pensioners_to_active","premium_per_active_participant_thousand_eur","premium_per_total_participant_thousand_eur"]) + '</div>') if flow_diagnostics is not None and not flow_diagnostics.empty else '<p class="muted">Geen flow_diagnostics.csv meegegeven of gevonden.</p>'}
  </section>

  <section>
    <h2>Jaarlijkse reconciliatie</h2>
    <p>
      Deze tabel vergelijkt gecompounde DNB-kwartaalrendementen met officiële jaarrendementen waar beschikbaar.
      De ingebouwde statische correctie bevat ABP 2015–2025 op basis van ABP's gepubliceerde rendementsreeks.
      Deze correctie wordt alleen gebruikt voor jaarlijkse audit-output; kwartaaldata en regressies worden niet stil overschreven.
    </p>
    <div class="formula-note">
      <p><strong>Formules:</strong></p>
      <p><code>dnb_compounded_annual_return[y] = product(1 + r[q]) - 1</code> voor de kwartalen in jaar <code>y</code>.</p>
      <p><code>difference_pp = 100 * (dnb_compounded_annual_return - official_annual_return)</code></p>
      <p><code>corrected_annual_return = official_annual_return</code> als officiële waarde beschikbaar is; anders de DNB-compound waarde.</p>
    </div>
    <h3>DNB versus officieel</h3>
    <div class="table-wrap">{df_to_html_return_table(annual_reconciliation, max_rows=None, pct_cols=["dnb_compounded_annual_return", "official_annual_return"])}</div>
    <h3>Corrected annual returns</h3>
    <div class="table-wrap">{df_to_html_return_table(corrected_annual_returns, max_rows=None, pct_cols=["dnb_compounded_annual_return", "official_annual_return", "corrected_annual_return"])}</div>
  </section>

  <section>
    <h2>TER-breakdown per jaar en fonds</h2>
    <div class="formula-note">
      <p><strong>Formules:</strong></p>
      <p><code>ter_annual = asset_management_costs + transaction_costs</code></p>
      <p><code>ter_quarterly = (1 + ter_annual)^(1/4) - 1</code></p>
      <p class="muted">De TER-correctie is een benadering; de DNB-rendementspost kan qua kostendefinitie afwijken van fonds-eigen publicaties.</p>
    </div>
    {("<p>Totale TER-like kostenratio per fonds.</p><div id='chart-ter' class='echarts-chart'></div><div class='fallback-static'>" + img(ter_chart, "TER per fonds") + "</div>") if has_ter_chart else '<p class="muted">Geen TER beschikbaar.</p>'}
    <h3>Long-format</h3>
    <div class="table-wrap compact-vertical">{df_to_html_table(ter_long, max_rows=None, percent_cols=["asset_management_costs","transaction_costs","ter_annual","ter_quarterly"])}</div>
    <h3>Wide-format</h3>
    <div class="table-wrap">{df_to_html_table(ter_wide, max_rows=80)}</div>
  </section>

  <section>
    <h2>Alpha-resultaten per fonds</h2>
    <div class="formula-note">
      <p><strong>Regressieformule per fonds:</strong></p>
      <p><code>excess_return[i,t] = alpha[i] + beta_equity[i] * equity[t] + beta_duration[i] * duration[t] + ... + epsilon[i,t]</code></p>
      <p><code>alpha_annualized = (1 + alpha_quarterly)^4 - 1</code></p>
      <p><code>alpha_annualized_ci_low/high</code> is het 95%-confidence interval voor geannualiseerde alpha, gebaseerd op de HAC/Newey-West standaardfout.</p>
      <p><code>p_alpha_holm</code> is de Holm-gecorrigeerde p-waarde over alle fonds-alpha-tests.</p>
      <p class="muted">Een positieve alpha betekent: hoger rendement dan verwacht op basis van de gebruikte factorblootstellingen; geen bewijs op zichzelf voor beleggingsvaardigheid. Een breed confidence interval betekent dat de schatting onzeker is.</p>
    </div>
    {alpha_section_html}
    <div class="table-wrap">{df_to_html_table(alpha, max_rows=None, percent_cols=["alpha_quarterly","alpha_quarterly_ci_low","alpha_quarterly_ci_high","alpha_annualized","alpha_annualized_ci_low","alpha_annualized_ci_high"], float_cols=["t_alpha","p_alpha","p_alpha_holm","r2"])}</div>
  </section>

  <section>
    <h2>Returns-based portefeuilleprofiel</h2>
    <p>
      Deze tabel probeert niet de werkelijke holdings te reconstrueren. De tabel gebruikt de geschatte beta's uit de factorregressie
      als proxy voor marktgevoeligheden. Dit kan helpen verklaren of rendementen vooral samenhangen met brede risky exposure,
      duration/rentegevoeligheid, credit, vastgoed, valuta of juist een alpha-residu. Alpha is daarbij het deel dat niet door
      deze gemeten factorblootstellingen wordt verklaard.
    </p>
    <div class="formula-note">
      <p><strong>Belangrijke formules:</strong></p>
      <p><code>broad_risky_beta = beta_equity + beta_credit + beta_real_estate</code></p>
      <p><code>loading_share_factor = max(beta_factor, 0) / sum(max(beta_positive_factors, 0))</code></p>
      <p><code>avg_annualized_simple_contribution_factor = 4 * beta_factor * average(factor_return)</code></p>
      <p class="muted">
        Dit is returns-based exposure-diagnostiek. Een hoge equity-loading betekent niet exact een hoge aandelenweging;
        rentehedges, derivaten, illiquide beleggingen en rapportagedefinities kunnen de beta's beïnvloeden.
      </p>
    </div>
    {portfolio_mix_section_html}
        {('<div class="table-wrap">' + df_to_html_table(portfolio_exposure_diagnostics, max_rows=None, percent_cols=["alpha_annualized","alpha_annualized_ci_low","alpha_annualized_ci_high","p_alpha_holm","loading_share_equity","loading_share_credit","loading_share_real_estate","loading_share_duration","loading_share_fx_abs"] + [c for c in portfolio_exposure_diagnostics.columns if c.startswith("avg_annualized_simple_contribution_")], float_cols=["broad_risky_beta","interest_rate_beta","currency_beta","beta_equity","beta_credit","beta_real_estate","beta_duration","beta_fx","r2"]) + '</div>') if portfolio_exposure_diagnostics is not None and not portfolio_exposure_diagnostics.empty else '<p class="muted">Geen portefeuilleprofiel beschikbaar; alpha-resultaten ontbreken.</p>'}
  </section>

  <section>
    <h2>Pairwise alpha-resultaten</h2>
    <p>
      Deze tabel vergelijkt fondsen paarsgewijs. De kolom <code>pair</code> heeft de vorm
      <code>fund_1 minus fund_2</code>. Een positieve pairwise alpha betekent dat <code>fund_1</code>
      na TER en na correctie voor de gebruikte factorblootstellingen hoger presteert dan <code>fund_2</code>.
      Een negatieve waarde betekent het omgekeerde.
    </p>
    <div class="formula-note">
      <p><strong>Pairwise regressieformule:</strong></p>
      <p><code>diff_return[i,j,t] = excess_return[i,t] - excess_return[j,t]</code></p>
      <p><code>diff_return[i,j,t] = alpha[i,j] + beta[i,j]' * factors[t] + epsilon[i,j,t]</code></p>
      <p><code>alpha_annualized[i,j] = (1 + alpha_quarterly[i,j])^4 - 1</code></p>
      <ul>
        <li><code>fund_1</code>: eerste fonds in de vergelijking.</li>
        <li><code>fund_2</code>: tweede fonds in de vergelijking.</li>
        <li><code>p_alpha</code>: gewone p-waarde voor de pairwise alpha.</li>
        <li><code>p_alpha_holm</code>: Holm-gecorrigeerde p-waarde over alle pairwise tests; gebruik deze voor voorzichtige significantie-interpretatie.</li>
        <li><code>r2</code>: welk deel van het verschilrendement door de factorverschillen wordt verklaard.</li>
      </ul>
      <p class="muted">
        Pairwise alpha is geen ranglijst van fondsbeheerkwaliteit; het is een returns-based verschiltest binnen het gekozen factorraamwerk.
        Deze tabel wordt volledig in het rapport geladen zodat de fondsfilter ook werkt bij kleine selecties; bij veel fondsen kan de tabel groot zijn.
      </p>
    </div>
    <div class="table-wrap compact-vertical">{df_to_html_table(pairwise, max_rows=None, percent_cols=["alpha_quarterly","alpha_quarterly_ci_low","alpha_quarterly_ci_high","alpha_annualized","alpha_annualized_ci_low","alpha_annualized_ci_high"], float_cols=["t_alpha","p_alpha","p_alpha_holm","r2"])}</div>
  </section>

  <section>
    <h2>Audit: rekenbasis analyseperiode</h2>
    <p>Deze tabel toont de exacte inputs per fonds/kwartaal voor de analyseperiode, niet noodzakelijk alle getoonde rendementskwartalen.</p>
    <div class="formula-note">
      <p><strong>Kolomformules:</strong></p>
      <p><code>return_after_ter = return_quarterly - ter_quarterly</code></p>
      <p><code>excess_return_after_ter = return_after_ter - rf</code></p>
      <p><code>used_ter_year</code> toont welk TER-jaar is gebruikt; bij missende jaren volgt dit uit <code>--ter-missing-policy</code>.</p>
    </div>
    <div class="table-wrap compact-vertical">{df_to_html_table(calculation_base, max_rows=None, percent_cols=["return_quarterly","asset_management_costs","transaction_costs","ter_annual","ter_quarterly","return_after_ter","rf","excess_return_after_ter"])}</div>
  </section>

  <section class="sources-list">
    <h2>Bronnen en links</h2>
    {sources_html}
  </section>
</div>
<script>
const ECHARTS_DATA = {echarts_payload_json};
</script>
<script>{fund_filter_js}</script>
</body>
</html>
"""

    path = output_dir / "report.html"
    path.write_text(html, encoding="utf-8")
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verwerk pensioenfondsrendementen, TER en factoren.")
    parser.add_argument("--returns", type=Path, required=True, help="returns_quarterly.csv")
    parser.add_argument("--ter", type=Path, default=None, help="ter_annual.csv")
    parser.add_argument("--factors", type=Path, default=None, help="factors.csv")
    parser.add_argument(
        "--flow-diagnostics",
        type=Path,
        default=None,
        help="Optionele flow_diagnostics.csv uit gather_dnb_pension_data.py. Als niet opgegeven wordt naast returns gezocht.",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("analysis_output"))
    parser.add_argument("--maxlags", type=int, default=2)
    parser.add_argument(
        "--analysis-end-period",
        default=None,
        help=(
            "Laatste kwartaal voor alpha/pairwise/reconciliatie/audit, bv. 2024Q4. "
            "Rendementen kunnen apart langer getoond worden met --returns-display-end-period."
        ),
    )
    parser.add_argument(
        "--returns-display-end-period",
        default=None,
        help=(
            "Laatste kwartaal dat in rendementstabellen/grafieken wordt getoond, bv. 2025Q4. "
            "Default: zelfde als volledige returns input."
        ),
    )
    parser.add_argument(
        "--factor-model",
        choices=["pension", "ken_french", "custom", "all"],
        default="all",
        help=(
            "Welke factor-kolommen in de regressie worden gebruikt. "
            "Aanbevolen hoofdmodel: pension = equity,duration,credit,real_estate,fx. "
            "ken_french gebruikt alleen ff_* factoren; custom gebruikt --factor-columns; "
            "all behoudt backwards-compatible gedrag."
        ),
    )
    parser.add_argument(
        "--factor-columns",
        default=None,
        help="Komma-gescheiden factorlijst voor --factor-model custom, bv. equity,duration,credit.",
    )
    parser.add_argument(
        "--ter-missing-policy",
        choices=["ffill", "nearest", "nearest_zero", "error", "zero", "drop"],
        default="ffill",
        help=(
            "Beleid voor fund/year zonder TER. "
            "Default: ffill gebruikt laatste eerdere TER per fonds. "
            "Voor all-funds runs is nearest_zero vaak praktischer."
        ),
    )
    parser.add_argument("--no-html-report", action="store_true")
    parser.add_argument(
        "--repo-url",
        default=None,
        help="Optionele GitHub-repositorylink die in het HTML-rapport wordt getoond.",
    )
    parser.add_argument(
        "--generated-branch-url",
        default=None,
        help="Optionele link naar de branch met gegenereerde statische output, bv. gh-pages.",
    )
    parser.add_argument(
        "--official-annual-returns",
        type=Path,
        default=None,
        help=(
            "Optionele CSV met officiële jaarrendementen: "
            "fund,year,official_annual_return. Decimal returns, bv. -0.176 = -17.6%%."
        ),
    )
    parser.add_argument(
        "--no-static-abp-official-returns",
        action="store_true",
        help="Zet de ingebouwde ABP officiële jaarrendementen 2015-2025 uit.",
    )
    parser.add_argument(
        "--annual-reconciliation-tolerance-pp",
        type=float,
        default=0.50,
        help="Warningdrempel in procentpunten voor annual_reconciliation.csv.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    returns = read_returns(args.returns)
    ter = read_ter(args.ter)
    factors_raw = read_factors(args.factors)
    factors, factor_model_meta = apply_factor_model(
        factors_raw,
        factor_model=args.factor_model,
        factor_columns_arg=args.factor_columns,
    )
    factor_model_meta_path = args.output_dir / "factor_model_used.csv"
    factor_model_meta.to_csv(factor_model_meta_path, index=False)
    print(f"Saved: {factor_model_meta_path}")

    flow_diagnostics_path = args.flow_diagnostics
    if flow_diagnostics_path is None:
        candidate_flow_path = args.returns.parent / "flow_diagnostics.csv"
        flow_diagnostics_path = candidate_flow_path if candidate_flow_path.exists() else None
    flow_diagnostics = read_flow_diagnostics(flow_diagnostics_path)

    returns_after_ter = apply_ter(returns, ter, missing_policy=args.ter_missing_policy)
    calculation_base_all_periods = add_factors(returns_after_ter, factors)
    calculation_base_returns_display = filter_by_end_period(
        calculation_base_all_periods,
        args.returns_display_end_period,
        "--returns-display-end-period",
    )
    calculation_base = filter_by_end_period(
        calculation_base_all_periods,
        args.analysis_end_period,
        "--analysis-end-period",
    )

    if args.analysis_end_period or args.returns_display_end_period:
        print(
            "Period split: "
            f"returns display {calculation_base_returns_display['period'].min()}–{calculation_base_returns_display['period'].max()}, "
            f"analysis {calculation_base['period'].min()}–{calculation_base['period'].max()}"
        )

    display_base_path = args.output_dir / "calculation_base_returns_display.csv"
    calculation_base_returns_display.to_csv(display_base_path, index=False)
    print(f"Saved: {display_base_path}")

    custom_official_annual_returns = read_official_annual_returns(args.official_annual_returns)
    official_annual_returns = combine_official_annual_returns(
        custom=custom_official_annual_returns,
        include_static_abp=not args.no_static_abp_official_returns,
    )

    annual_raw, annual_after_ter, annual_reconciliation = make_annual_reconciliation(
        calculation_base=calculation_base,
        official_annual_returns=official_annual_returns,
        tolerance_pp=args.annual_reconciliation_tolerance_pp,
    )
    corrected_annual_returns = make_corrected_annual_returns(
        annual_raw=annual_raw,
        official_annual_returns=official_annual_returns,
    )

    official_annual_returns_path = args.output_dir / "official_annual_returns.csv"
    official_annual_returns.to_csv(official_annual_returns_path, index=False)
    print(f"Saved: {official_annual_returns_path}")

    annual_returns_raw_path = args.output_dir / "annual_returns_raw_from_dnb_quarters.csv"
    annual_returns_after_ter_path = args.output_dir / "annual_returns_after_ter_from_dnb_quarters.csv"
    annual_reconciliation_path = args.output_dir / "annual_reconciliation.csv"
    corrected_annual_returns_path = args.output_dir / "annual_returns_corrected_static.csv"

    annual_raw.to_csv(annual_returns_raw_path, index=False)
    annual_after_ter.to_csv(annual_returns_after_ter_path, index=False)
    annual_reconciliation.to_csv(annual_reconciliation_path, index=False)
    corrected_annual_returns.to_csv(corrected_annual_returns_path, index=False)

    print(f"Saved: {annual_returns_raw_path}")
    print(f"Saved: {annual_returns_after_ter_path}")
    print(f"Saved: {annual_reconciliation_path}")
    print(f"Saved: {corrected_annual_returns_path}")

    data_quality = make_data_quality_checks(
        calculation_base,
        factors,
        annual_reconciliation=annual_reconciliation,
    )
    data_quality_path = args.output_dir / "data_quality_checks.csv"
    data_quality.to_csv(data_quality_path, index=False)
    print(f"Saved: {data_quality_path}")

    flow_output_path = None
    if flow_diagnostics is not None and not flow_diagnostics.empty:
        flow_output_path = args.output_dir / "flow_diagnostics.csv"
        flow_diagnostics.to_csv(flow_output_path, index=False)
        print(f"Saved: {flow_output_path}")

    calculation_base_path = args.output_dir / "calculation_base_long.csv"
    calculation_base.to_csv(calculation_base_path, index=False)
    print(f"Saved: {calculation_base_path}")

    calculation_base_wide = make_wide_audit(calculation_base)
    calculation_base_wide_path = args.output_dir / "calculation_base_wide.csv"
    calculation_base_wide.to_csv(calculation_base_wide_path, index=False)
    print(f"Saved: {calculation_base_wide_path}")

    returns_after_ter_wide = returns_long_to_wide(calculation_base, "return_after_ter")
    returns_after_ter_wide_path = args.output_dir / "returns_after_ter_wide.csv"
    returns_after_ter_wide.to_csv(returns_after_ter_wide_path, index=False)
    print(f"Saved: {returns_after_ter_wide_path}")

    returns_quarterly_pct_wide = make_returns_percent_wide(calculation_base, "return_quarterly")
    returns_quarterly_pct_wide_path = args.output_dir / "returns_quarterly_percent_wide.csv"
    returns_quarterly_pct_wide.to_csv(returns_quarterly_pct_wide_path, index=False)
    print(f"Saved: {returns_quarterly_pct_wide_path}")

    returns_after_ter_pct_wide = make_returns_percent_wide(calculation_base, "return_after_ter")
    returns_after_ter_pct_wide_path = args.output_dir / "returns_after_ter_percent_wide.csv"
    returns_after_ter_pct_wide.to_csv(returns_after_ter_pct_wide_path, index=False)
    print(f"Saved: {returns_after_ter_pct_wide_path}")

    ter_long, ter_wide = make_ter_breakdown(ter)
    ter_long_path = args.output_dir / "ter_breakdown_long.csv"
    ter_wide_path = args.output_dir / "ter_breakdown_wide.csv"
    ter_long.to_csv(ter_long_path, index=False)
    ter_wide.to_csv(ter_wide_path, index=False)
    print(f"Saved: {ter_long_path}")
    print(f"Saved: {ter_wide_path}")

    alpha = run_alpha_regressions(calculation_base, factors, args.maxlags)
    alpha_path = args.output_dir / "alpha_results.csv"
    alpha.to_csv(alpha_path, index=False)
    print(f"Saved: {alpha_path}")

    portfolio_exposure_diagnostics = make_portfolio_exposure_diagnostics(alpha, calculation_base)
    portfolio_exposure_path = args.output_dir / "portfolio_exposure_diagnostics.csv"
    portfolio_exposure_diagnostics.to_csv(portfolio_exposure_path, index=False)
    print(f"Saved: {portfolio_exposure_path}")

    pairwise = run_pairwise_alpha(calculation_base, factors, args.maxlags)
    pairwise_path = args.output_dir / "pairwise_alpha_results.csv"
    pairwise.to_csv(pairwise_path, index=False)
    print(f"Saved: {pairwise_path}")

    if not args.no_html_report:
        report_path = make_html_report(
            args.output_dir,
            calculation_base,
            ter_long,
            ter_wide,
            alpha,
            pairwise,
            data_quality,
            annual_reconciliation,
            corrected_annual_returns,
            flow_diagnostics,
            portfolio_exposure_diagnostics,
            source_files={
                "returns_quarterly.csv": str(args.returns),
                "ter_annual.csv": str(args.ter) if args.ter else None,
                "factors.csv": str(args.factors) if args.factors else None,
                "factor_model_used.csv": str(factor_model_meta_path),
                "flow_diagnostics.csv": str(flow_diagnostics_path) if flow_diagnostics_path else None,
                "official_annual_returns.csv": str(args.official_annual_returns) if args.official_annual_returns else "static ABP built-in table",
                "alpha_results.csv": str(alpha_path),
                "portfolio_exposure_diagnostics.csv": str(portfolio_exposure_path),
                "pairwise_alpha_results.csv": str(pairwise_path),
                "calculation_base_long.csv": str(calculation_base_path),
                "data_quality_checks.csv": str(data_quality_path),
                "calculation_base_returns_display.csv": str(display_base_path),
                "flow_diagnostics output": str(flow_output_path) if flow_output_path else None,
            },
            calculation_base_returns_display=calculation_base_returns_display,
            analysis_end_period=args.analysis_end_period,
            returns_display_end_period=args.returns_display_end_period,
            repo_url=args.repo_url,
            generated_branch_url=args.generated_branch_url,
        )
        print(f"Saved: {report_path}")


if __name__ == "__main__":
    try:
        main()
    except InputFormatError as exc:
        raise SystemExit(str(exc)) from None
