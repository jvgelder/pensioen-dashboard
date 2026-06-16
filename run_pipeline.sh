#!/usr/bin/env bash
set -euo pipefail

pipenv run python src/gather_dnb_pension_data.py \
  --dnb-config config/dnb_resources.json \
  --output-dir data \
  --fund-selection all

pipenv run python src/gather_factors.py \
  --output-dir factors \
  --output-file factors.csv \
  --first-period 2015Q1

pipenv run python src/process_pension_alpha.py \
  --returns data/returns_quarterly.csv \
  --ter data/ter_annual.csv \
  --factors factors/factors.csv \
  --flow-diagnostics data/flow_diagnostics.csv \
  --ter-missing-policy nearest_zero \
  --factor-model pension \
  --analysis-end-period 2024Q4 \
  --returns-display-end-period 2025Q4 \
  --output-dir analysis_output
