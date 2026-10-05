name: keirin-bot-ping

# RenderのBotが無料プランでスリープしないよう、定期的に起こしに行くだけのワークフロー。
# これにより、LINEでキーワードを送ったときの応答も速くなる。

on:
  schedule:
    # 10分おき(JST 7:00〜25:00ごろ=UTC 22:00〜16:00)
    - cron: "*/10 22-23,0-15 * * *"
  workflow_dispatch: {}

jobs:
  ping:
    runs-on: ubuntu-latest
    timeout-minutes: 2
    steps:
      - name: Wake up Render
        run: |
          curl -s -o /dev/null -w "status: %{http_code}\n" "${{ secrets.BOT_URL }}" || true
