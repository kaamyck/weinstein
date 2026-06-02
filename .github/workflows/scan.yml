name: Update Weinstein data

on:
  workflow_dispatch:
  schedule:
    - cron: "0 7 * * 1-5"

permissions:
  contents: write

concurrency:
  group: update-weinstein-data
  cancel-in-progress: true

jobs:
  update-data:
    runs-on: ubuntu-latest
    timeout-minutes: 45

    steps:
      - name: Checkout repo
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Show files
        run: |
          pwd
          ls -la
          echo "Workflow folder:"
          ls -la .github/workflows || true

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install yfinance pandas lxml html5lib beautifulsoup4 requests

      - name: Run Weinstein scanner full universe
        run: |
          python weinstein_scan.py --batch 5 --pause 3 --debug

      - name: Check data.json is not empty
        run: |
          python - <<'PY'
          import json

          with open("data.json", "r", encoding="utf-8") as f:
              d = json.load(f)

          stocks = d.get("stocks", [])
          meta = d.get("meta", {})

          print("META:", meta)
          print("STOCKS:", len(stocks))

          if len(stocks) == 0:
              raise SystemExit("BŁĄD: data.json ma 0 spółek — nie publikuję pustego pliku.")

          print("FIRST STOCK:", stocks[0].get("ticker", "BRAK"))
          PY

      - name: Commit and push data.json
        run: |
          git config user.name "github-actions"
          git config user.email "github-actions@github.com"

          git status

          git add data.json

          if git diff --cached --quiet; then
            echo "No changes in data.json to commit."
            exit 0
          fi

          git commit -m "Update data.json"

          git pull --rebase origin main

          git push origin HEAD:main
