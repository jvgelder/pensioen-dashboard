# GitHub Actions

This folder contains the workflow that builds and publishes the static report.

## Workflow

```text
build-commit-gh-pages-deploy.yml
```

What it does:

```text
1. checks out main
2. restores cached DNB/factor inputs if available
3. optionally refreshes DNB data and factor data
4. builds analysis_output/report.html
5. moves report.html to index.html
6. commits generated static output to gh-pages
7. deploys the same static output to GitHub Pages
```

The report itself is the site root:

```text
analysis_output/report.html -> index.html
```

## Branch strategy

```text
main      source code, config and documentation
gh-pages  generated static report output
```

Generated data and report files are not committed to `main`.

## Pages setup

Use:

```text
Settings -> Pages -> Build and deployment -> Source -> GitHub Actions
```

## Permissions

The workflow needs:

```yaml
permissions:
  contents: write
  pages: write
  id-token: write
```

Why:

```text
contents: write   push generated static output to gh-pages
pages: write      deploy GitHub Pages
id-token: write   authenticate official Pages deployment
```

If pushes to `gh-pages` fail, check:

```text
Settings -> Actions -> General -> Workflow permissions
```

and allow workflows to write repository contents.

## Triggers

The workflow runs on:

```text
manual workflow_dispatch
pushes to main that change source/config/workflow files
```

The workflow is not scheduled by default. This avoids unattended calls to DNB and market-data endpoints in a public repository.

## Manual inputs

```text
refresh_inputs              true or false
analysis_end_period         e.g. 2024Q4
returns_display_end_period  e.g. 2025Q4
```

Use:

```text
refresh_inputs = true
```

when DNB data, factor data, or resource IDs changed.

Use:

```text
refresh_inputs = false
```

when only report layout/code changed and cached inputs are good enough.

## Caching

The workflow uses two caches:

```text
pip dependency cache
DNB/factor input cache for data/ and factors/
```

The pip cache is handled by `actions/setup-python`.

The data cache is handled by `actions/cache`.

## DNB key

For public repositories, do not commit the DNB subscription key.

If the DNB endpoint requires a key, configure this repository secret:

```text
DNB_STATPUB_KEY
```

under:

```text
Settings -> Secrets and variables -> Actions
```

## Common failure modes

### Push to gh-pages is denied

Check workflow permissions:

```text
Settings -> Actions -> General -> Workflow permissions
```

### Pages deploy fails

Check that Pages source is set to:

```text
GitHub Actions
```

### Cached inputs are missing

Run the workflow manually with:

```text
refresh_inputs = true
```

### Python syntax or CLI error

Run locally:

```bash
python src/gather_dnb_pension_data.py --help
python src/gather_factors.py --help
python src/process_pension_alpha.py --help
```
