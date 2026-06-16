# Pensioen Dashboard

Public, reproducible pipeline for a returns-based comparison of Dutch pension fund performance using DNB individual pension fund data, optional market/factor proxies, and a generated static HTML report.

The published report is the GitHub Pages site. The repository keeps source code on `main`; generated static output is published separately to `gh-pages`.

## Important caveat

This project is an exploratory, returns-based analysis. Alpha is the intercept/residual from a chosen factor model. It is **not** direct proof of investment skill, governance quality, or fund superiority.

The results depend on:

- the DNB return definition used;
- the selected factor model;
- proxy quality for equity, duration, credit, real estate and currency exposure;
- TER/cost assumptions;
- data availability and reporting definitions;
- the chosen analysis period.

Use the report as a diagnostic and comparison tool, not as investment advice or a formal performance attribution.

## Repository layout

```text
src/
├── gather_dnb_pension_data.py
├── gather_factors.py
└── process_pension_alpha.py

config/
└── dnb_resources.json

.github/workflows/
├── build-commit-gh-pages-deploy.yml
└── README.md
```

Generated folders are intentionally not committed to `main`:

```text
data/
factors/
analysis_output/
site/
public/
docs/
```

## Quick start

Install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Most common local run:

```bash
python src/gather_dnb_pension_data.py \
  --dnb-config config/dnb_resources.json \
  --output-dir data \
  --fund-selection all

python src/gather_factors.py \
  --output-dir factors \
  --output-file factors.csv \
  --first-period 2015Q1

python src/process_pension_alpha.py \
  --returns data/returns_quarterly.csv \
  --ter data/ter_annual.csv \
  --factors factors/factors.csv \
  --flow-diagnostics data/flow_diagnostics.csv \
  --ter-missing-policy nearest_zero \
  --factor-model pension \
  --analysis-end-period 2024Q4 \
  --returns-display-end-period 2025Q4 \
  --repo-url "https://github.com/jvgelder/pensioen-dashboard" \
  --generated-branch-url "https://github.com/jvgelder/pensioen-dashboard/tree/gh-pages" \
  --output-dir analysis_output
```

Report output:

```text
analysis_output/report.html
```

For GitHub Pages, the workflow publishes this as:

```text
index.html
```

## Scripts and main options

### `src/gather_dnb_pension_data.py`

Downloads or reads DNB pension fund data and standardizes the input files used by the analysis.

Main outputs:

```text
data/returns_quarterly.csv
data/ter_annual.csv
data/flow_diagnostics.csv
```

Important options:

```text
--dnb-config          Path to tracked DNB resource config.
--output-dir          Output folder, usually data.
--fund-selection      default or all.
--include-funds       Comma-separated fund/reporter filter when using all.
--fund-map            Custom CSV mapping: fund,dnb_name.
--quarterly-file      Use a local quarterly DNB file instead of downloading.
--annual-file         Use a local annual DNB file instead of downloading.
--skip-quarterly      Only process annual/TER data.
--skip-annual         Only process quarterly returns.
```

Selected-funds example:

```bash
python src/gather_dnb_pension_data.py \
  --dnb-config config/dnb_resources.json \
  --output-dir data \
  --fund-selection all \
  --include-funds "ABP,Zorg en Welzijn,Particuliere Beveiliging"
```

### `src/gather_factors.py`

Builds quarterly factor data.

Main output:

```text
factors/factors.csv
```

Important options:

```text
--market-source          yahoo or none.
--market-factor-mode     excess or raw.
--rf-mode                xeon or zero.
--rf-source              market, ken_french or zero.
--include-ken-french     Add Ken French factors.
--ken-french-region      europe or developed.
--ken-french-model       3 or 5.
--ken-french-file        Local Ken French file.
--allow-partial-missing  Allow missing factor values.
```

Recommended pension proxy factor command:

```bash
python src/gather_factors.py \
  --output-dir factors \
  --output-file factors.csv \
  --first-period 2015Q1 \
  --market-source yahoo \
  --market-factor-mode excess \
  --rf-mode xeon \
  --rf-source market
```

This produces the pension-model factor set when data are available:

```text
rf,equity,duration,credit,real_estate,fx
```

### `src/process_pension_alpha.py`

Builds after-TER returns, factor regressions, pairwise alpha results, diagnostics and the HTML report.

Main outputs:

```text
analysis_output/report.html
analysis_output/alpha_results.csv
analysis_output/pairwise_alpha_results.csv
analysis_output/calculation_base_long.csv
analysis_output/factor_model_used.csv
```

Important options:

```text
--factor-model                 pension, ken_french, custom or all.
--factor-columns               Comma-separated factor list for custom model.
--ter-missing-policy           ffill, nearest, nearest_zero, zero, drop or error.
--analysis-end-period          Last quarter for alpha/pairwise/audit.
--returns-display-end-period   Last quarter shown in return tables/charts.
--maxlags                      HAC/Newey-West lag count.
--repo-url                     GitHub repository link shown in report.
--generated-branch-url         gh-pages branch link shown in report.
--official-annual-returns      Optional official annual return CSV.
```

Recommended report command:

```bash
python src/process_pension_alpha.py \
  --returns data/returns_quarterly.csv \
  --ter data/ter_annual.csv \
  --factors factors/factors.csv \
  --flow-diagnostics data/flow_diagnostics.csv \
  --ter-missing-policy nearest_zero \
  --factor-model pension \
  --analysis-end-period 2024Q4 \
  --returns-display-end-period 2025Q4 \
  --output-dir analysis_output
```

## TER missing policy

For public all-fund reports, the recommended option is:

```text
--ter-missing-policy nearest_zero
```

Policy meanings:

```text
ffill         Use the latest earlier TER for the same fund.
nearest       Use nearest available TER year for the same fund.
nearest_zero  Use nearest available TER; if no TER exists for the fund, use zero.
zero          Use zero whenever TER is missing.
drop          Drop observations with missing TER.
error         Stop if TER is missing.
```

## Factor model choices

Recommended main model:

```text
--factor-model pension
```

Uses:

```text
equity,duration,credit,real_estate,fx
```

Other options:

```text
ken_french  Uses available Ken French factors, excluding rf.
custom      Uses --factor-columns exactly.
all         Uses available factors, mainly for diagnostics/backward compatibility.
```

## DNB configuration and keys

DNB public resource identifiers are tracked in:

```text
config/dnb_resources.json
```

The real DNB subscription key, if needed, must not be committed. Keep this in config:

```json
"subscription_key": null
```

Provide the key locally as:

```bash
export DNB_STATPUB_KEY="..."
```

or in GitHub Actions as a repository secret named:

```text
DNB_STATPUB_KEY
```

## Report layout

The report uses native HTML/CSS for layout improvements:

```text
large tables are scroll panes
table headers are sticky
fund selector is sticky on desktop
long context uses native details/summary
```

The main table rule is:

```css
.table-wrap {
  max-height: calc(100vh - 112px);
  overflow: auto;
}
```

## Public repository checklist

Before publishing or updating the public repository:

- confirm no API/subscription key is committed;
- confirm no generated data is committed to `main`;
- confirm GitHub Pages source is set to GitHub Actions;
- confirm the generated site is published from `gh-pages`;
- confirm the caveat remains visible at the top of the README and report.
