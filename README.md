# Pension Alpha

Returns-based alpha analysis for Dutch pension fund DNB data.


## Public repository notes

This repository is designed to be public.

Safe to commit:

```text
src/
config/dnb_resources.json
requirements.txt
README.md
NOTICE.md
SECURITY.md
PUBLICATION_CHECKLIST.md
.github/workflows/
```

Do not commit:

```text
DNB/API keys
.env files
data/
factors/
analysis_output/
docs/
site/
public/
```

The generated site is committed by GitHub Actions to `gh-pages`, not to `main`.


### No hardcoded DNB key

There is no hardcoded DNB subscription key in the source code or config. The only supported ways to provide a key are:

```bash
export DNB_STATPUB_KEY="..."
```

or a GitHub Actions secret named:

```text
DNB_STATPUB_KEY
```

Keep `config/dnb_resources.json` as:

```json
"subscription_key": null
```

### DNB key handling

`config/dnb_resources.json` stores public DNB resource identifiers and the name of the environment variable used for the subscription key:

```text
DNB_STATPUB_KEY
```

Keep this field `null` in public repos:

```json
"subscription_key": null
```

If the DNB endpoint requires a key, configure it as a GitHub Actions secret:

```text
Settings -> Secrets and variables -> Actions -> New repository secret -> DNB_STATPUB_KEY
```

### No default scheduled refresh

The default workflow does not include a scheduled run. It runs manually or when relevant source/config files are pushed to `main`. This avoids an unattended public repository repeatedly calling DNB or market-data endpoints.

## Branch strategy

- `main`: source code and workflow only.
- `gh-pages`: generated static site only.
- GitHub Pages source: `GitHub Actions`.

The report itself is the static site root: the workflow copies `report.html` to `index.html`.

The generated site includes links back to:

- GitHub code on `main`
- generated static output on `gh-pages`

## Source layout

```text
src/
├── process_pension_alpha.py
├── gather_dnb_pension_data.py
└── gather_factors.py

config/
└── dnb_resources.json
```

No package subfolder is used. The workflow runs the scripts directly with `python src/<script>.py`.

## No generated data on `main`

Generated/local folders are ignored:

```text
data/
factors/
analysis_output/
docs/
site/
public/
```

The generated static site is committed only to `gh-pages`.


## DNB resource configuration

DNB dataset/resource identifiers are tracked in:

```text
config/dnb_resources.json
```

This file contains:

```text
resources.quarterly_individual_pension_funds.resource_id
resources.annual_individual_pension_funds.resource_id
api.resourcefile_url
api.subscription_key_env
```

The resource IDs identify the DNB datasets. They are safe and useful to keep in git because changing them changes the input universe.

The `ocp-apim-subscription-key` value is the DNB/API-management subscription key used in the HTTP header. Do **not** commit a real/private key. Use a GitHub Secret or local environment variable instead:

```text
DNB_STATPUB_KEY
```

The workflow triggers automatically on pushes that change `config/dnb_resources.json` or the source scripts. A config change therefore refreshes DNB data, rebuilds the report, commits the generated static site to `gh-pages`, and deploys the same site to GitHub Pages.

## Workflow

```text
.github/workflows/build-commit-gh-pages-deploy.yml
```

What it does:

```text
1. checks out main
2. optionally downloads/refreshes DNB data
3. optionally downloads/refreshes factor data
4. builds the report
5. copies report.html to index.html
6. injects links to main and gh-pages into the static report
7. commits static output to gh-pages
8. deploys the same static output with GitHub Pages Actions
```

There is no separate compile/syntax CI step. If the scripts fail, the build fails at the actual pipeline step.

## GitHub permissions

Use this setting:

```text
Settings -> Pages -> Build and deployment -> Source -> GitHub Actions
```

The workflow declares:

```yaml
permissions:
  contents: write
  pages: write
  id-token: write
```

`contents: write` is needed to push the generated site to `gh-pages`.
`pages: write` and `id-token: write` are needed for the GitHub Pages deployment action.

If your repository or organization restricts workflow write permissions, enable write permissions under:

```text
Settings -> Actions -> General -> Workflow permissions
```

## DNB and factor collection

The data collection scripts are included:

```text
src/gather_dnb_pension_data.py
src/gather_factors.py
```

The workflow has a manual input:

```text
refresh_inputs = true / false
```

- `true`: download/refresh DNB and factor input data before building.
- `false`: restore the most recent cached `data/` and `factors/` inputs.

Use `true` when DNB data/resource IDs or factor data need refreshing.
Use `false` when you only changed report layout/code.

## Local run

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt

./run_pipeline.sh
```

Or, if you already have `data/` and `factors/` locally:

```bash
./run_report_from_existing_inputs.sh
```

## Required generated inputs

The report step expects:

```text
data/returns_quarterly.csv
data/ter_annual.csv
data/flow_diagnostics.csv
factors/factors.csv
```

These are generated by:

```bash
python src/gather_dnb_pension_data.py --dnb-config config/dnb_resources.json --output-dir data --fund-selection all
python src/gather_factors.py --output-dir factors --output-file factors.csv --first-period 2015Q1
```



## Simplified static-site publishing

The generated report is the site root. During deployment the workflow moves:

```text
analysis_output/report.html -> index.html
```

The report generator itself adds links to:

```text
https://github.com/jvgelder/pensioen-dashboard
https://github.com/jvgelder/pensioen-dashboard/tree/gh-pages
```

The workflow no longer injects HTML into the report. It only builds, renames `report.html` to `index.html`, commits the static files to `gh-pages`, and deploys the same static files to GitHub Pages.

## Caching strategy

The workflow uses two caches:

```text
1. pip dependency cache via actions/setup-python
2. DNB/factor input cache via actions/cache
```

The pip cache avoids repeated dependency downloads. `pip install -r requirements.txt` still runs, but most package downloads should come from cache when `requirements.txt` has not changed.

The DNB/factor cache stores:

```text
data/
factors/
```

Use:

```text
refresh_inputs = true
```

to fetch fresh DNB and factor data and update the cache.

Use:

```text
refresh_inputs = false
```

to rebuild the report from cached inputs. This is useful for report layout/code changes when the data itself has not changed.

## Command cookbook

### 1. Full fresh local pipeline

Use this when you want to refresh DNB data, refresh factors and rebuild the report.

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
  --output-dir analysis_output
```

### 2. Fund selection

#### All DNB reporters with fund-risk quarterly returns

```bash
python src/gather_dnb_pension_data.py \
  --dnb-config config/dnb_resources.json \
  --output-dir data \
  --fund-selection all
```

#### Default mapped fund set

The default mapped set is useful for a smaller reproducible test run.

```bash
python src/gather_dnb_pension_data.py \
  --dnb-config config/dnb_resources.json \
  --output-dir data \
  --fund-selection default
```

#### Include only selected funds

Use `--fund-selection all` plus `--include-funds`. Matching is done on the normalized fund/DNB reporter name.

```bash
python src/gather_dnb_pension_data.py \
  --dnb-config config/dnb_resources.json \
  --output-dir data \
  --fund-selection all \
  --include-funds "ABP,Zorg en Welzijn,Particuliere Beveiliging"
```

#### Custom fund map

Use a CSV with this structure:

```csv
fund,dnb_name
ABP,ABP
PFZW,Zorg en Welzijn
PMT,Metaal en Techniek
```

Run:

```bash
python src/gather_dnb_pension_data.py \
  --dnb-config config/dnb_resources.json \
  --output-dir data \
  --fund-map fund_map.csv
```

### 3. DNB input refresh options

#### Use already downloaded DNB files

Useful when the DNB endpoint is unavailable or when you want to rerun from a fixed snapshot.

```bash
python src/gather_dnb_pension_data.py \
  --output-dir data \
  --quarterly-file data/dnb_quarterly_raw_download.csv \
  --annual-file data/dnb_annual_raw_download.xlsx \
  --fund-selection all
```

#### Quarterly only

```bash
python src/gather_dnb_pension_data.py \
  --dnb-config config/dnb_resources.json \
  --output-dir data \
  --fund-selection all \
  --skip-annual
```

#### Annual TER/flow only

```bash
python src/gather_dnb_pension_data.py \
  --dnb-config config/dnb_resources.json \
  --output-dir data \
  --annual-file data/dnb_annual_raw_download.xlsx \
  --skip-quarterly
```

### 4. Factor data commands

#### Pension proxy factors, recommended main model

This produces:

```text
rf,equity,duration,credit,real_estate,fx
```

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

Default proxy tickers are:

```text
rf:          XEON.DE
equity:      IWDA.AS
duration:    IBGL.AS
credit:      IEAC.AS
real_estate: IPRP.AS
fx:          EURUSD=X, stored as negative EUR/USD return
```

#### Override factor proxy tickers

```bash
python src/gather_factors.py \
  --output-dir factors \
  --output-file factors.csv \
  --first-period 2015Q1 \
  --equity IWDA.AS \
  --duration IBGL.AS \
  --credit IEAC.AS \
  --real-estate IPRP.AS \
  --eurusd EURUSD=X \
  --rf-ticker XEON.DE
```

#### Zero risk-free rate

Use this if you want no cash/risk-free correction.

```bash
python src/gather_factors.py \
  --output-dir factors \
  --output-file factors.csv \
  --first-period 2015Q1 \
  --rf-source zero \
  --rf-mode zero
```

#### Raw factor returns instead of excess factor returns

Default is `excess`, meaning market proxy factors are written net of `rf` where applicable.

```bash
python src/gather_factors.py \
  --output-dir factors \
  --output-file factors.csv \
  --first-period 2015Q1 \
  --market-factor-mode raw
```

#### Include Ken French factors

```bash
python src/gather_factors.py \
  --output-dir factors \
  --output-file factors.csv \
  --first-period 2015Q1 \
  --include-ken-french \
  --ken-french-region europe \
  --ken-french-model 5 \
  --ken-french-momentum
```

#### Use a local Ken French file

```bash
python src/gather_factors.py \
  --output-dir factors \
  --output-file factors.csv \
  --first-period 2015Q1 \
  --include-ken-french \
  --ken-french-region europe \
  --ken-french-model 5 \
  --ken-french-file data/raw/Europe_5_Factors.csv
```

### 5. Factor model types in the alpha report

The factor file may contain many factor columns. The `--factor-model` option controls which factors are actually used in the regressions.

#### Recommended pension model

```bash
python src/process_pension_alpha.py \
  --returns data/returns_quarterly.csv \
  --ter data/ter_annual.csv \
  --factors factors/factors.csv \
  --flow-diagnostics data/flow_diagnostics.csv \
  --factor-model pension \
  --ter-missing-policy nearest_zero \
  --analysis-end-period 2024Q4 \
  --returns-display-end-period 2025Q4 \
  --output-dir analysis_output
```

Uses:

```text
equity,duration,credit,real_estate,fx
```

#### Ken French model

```bash
python src/process_pension_alpha.py \
  --returns data/returns_quarterly.csv \
  --ter data/ter_annual.csv \
  --factors factors/factors.csv \
  --flow-diagnostics data/flow_diagnostics.csv \
  --factor-model ken_french \
  --ter-missing-policy nearest_zero \
  --analysis-end-period 2024Q4 \
  --returns-display-end-period 2025Q4 \
  --output-dir analysis_output_ken_french
```

Uses available Ken French factor columns, excluding `rf`.

#### Custom factor columns

```bash
python src/process_pension_alpha.py \
  --returns data/returns_quarterly.csv \
  --ter data/ter_annual.csv \
  --factors factors/factors.csv \
  --flow-diagnostics data/flow_diagnostics.csv \
  --factor-model custom \
  --factor-columns equity,duration,credit \
  --ter-missing-policy nearest_zero \
  --analysis-end-period 2024Q4 \
  --returns-display-end-period 2025Q4 \
  --output-dir analysis_output_custom
```

#### All available factors

This is mainly for diagnostics/backward compatibility. It is not recommended as the main interpretation model because factor overlap can make individual beta coefficients hard to interpret.

```bash
python src/process_pension_alpha.py \
  --returns data/returns_quarterly.csv \
  --ter data/ter_annual.csv \
  --factors factors/factors.csv \
  --flow-diagnostics data/flow_diagnostics.csv \
  --factor-model all \
  --ter-missing-policy nearest_zero \
  --analysis-end-period 2024Q4 \
  --returns-display-end-period 2025Q4 \
  --output-dir analysis_output_all_factors
```

### 6. TER missing policy / zero-fill options

The TER file is annual. When a fund-year has no exact TER observation, the policy is controlled by `--ter-missing-policy`.

Recommended for all-fund public reports:

```bash
--ter-missing-policy nearest_zero
```

Available policies:

```text
ffill        Use the latest earlier TER for the same fund.
nearest      Use nearest available TER year for the same fund.
nearest_zero Use nearest available TER year; if no TER exists for that fund, use zero.
zero         Always use zero when TER is missing.
drop         Drop fund/quarter observations with missing TER.
error        Stop if TER is missing.
```

Examples:

#### Conservative public report policy

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

#### Strict audit mode

```bash
python src/process_pension_alpha.py \
  --returns data/returns_quarterly.csv \
  --ter data/ter_annual.csv \
  --factors factors/factors.csv \
  --flow-diagnostics data/flow_diagnostics.csv \
  --ter-missing-policy error \
  --factor-model pension \
  --analysis-end-period 2024Q4 \
  --returns-display-end-period 2025Q4 \
  --output-dir analysis_output_strict
```

#### Zero-fill all missing TER

Use only if you explicitly want missing TER to mean no TER deduction.

```bash
python src/process_pension_alpha.py \
  --returns data/returns_quarterly.csv \
  --ter data/ter_annual.csv \
  --factors factors/factors.csv \
  --flow-diagnostics data/flow_diagnostics.csv \
  --ter-missing-policy zero \
  --factor-model pension \
  --analysis-end-period 2024Q4 \
  --returns-display-end-period 2025Q4 \
  --output-dir analysis_output_zero_ter
```

### 7. Period split

Use a shorter analysis window when annual TER/flow data are only available through 2024, while quarterly returns already continue into 2025.

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

Meaning:

```text
Rendementsgrafieken/tables:  up to 2025Q4
Alpha/pairwise/audit model:  up to 2024Q4
```

### 8. GitHub Actions examples

#### Manual run with fresh DNB/factor refresh

In GitHub:

```text
Actions -> Build report, publish static site branch, and deploy Pages -> Run workflow
refresh_inputs = true
analysis_end_period = 2024Q4
returns_display_end_period = 2025Q4
```

#### Manual run using cached inputs

Use this after layout/report-code changes when you do not need to refresh DNB or factor data.

```text
Actions -> Run workflow
refresh_inputs = false
```

#### Automatic rebuild when DNB resource config changes

Changing this file triggers the workflow automatically:

```text
config/dnb_resources.json
```

That refreshes the DNB inputs, rebuilds the report, commits the static output to `gh-pages`, and deploys the site.

## Caveat

This is an exploratory, returns-based model. Alpha is a residual within the chosen factor model, not direct proof of investment skill. Factor loadings are proxies for market sensitivities and should not be interpreted as exact holdings.
