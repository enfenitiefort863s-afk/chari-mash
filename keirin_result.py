name: keirin-result

on:
  schedule:
    # 日本時間 23:40(その日の結果)と 7:20(深夜レースの取りこぼし)
    - cron: "40 14 * * *"
    - cron: "20 22 * * *"
  workflow_dispatch: {}

permissions:
  contents: write

jobs:
  result:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: "pip"
      - run: pip install -r requirements.txt
      - run: python keirin_result.py
        env:
          LINE_CHANNEL_ACCESS_TOKEN: ${{ secrets.LINE_CHANNEL_ACCESS_TOKEN }}
          LINE_USER_ID: ${{ secrets.LINE_USER_ID }}
          WORKERS: ${{ vars.WORKERS }}
      - name: Save data
        if: always()
        run: |
          mkdir -p data
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add data
          if ! git diff --cached --quiet; then
            git commit -m "update results"
            git pull --rebase
            git push
          fi
