# 現在の構成(このファイルは記録用。動作には関係ありません)

## ファイル
- keirin_line.py   … WinTicket取得・分析・評価・LINE文面づくり(共通ロジック)
- keirin_run.py    … 定時配信の入口(会場ごとの振り分け、送信数の管理、買い目/学習データ/記事の保存)
- keirin_track.py  … 買い目の保存、結果の判定、的中率・回収率の集計、好走ピックアップ
- keirin_learn.py  … 過去の結果から評価の重み(data/model.json)を学習し直す
- keirin_result.py … 結果報告(LINE)の入口
- keirin_publish.py… note記事の生成・保存、(任意)note自動投稿・X自動告知 ※要検証

## 配信スケジュール(GitHub Actions, JST)
- 8:00 / 20:00      … keirin-line.yml (予想配信)
- 23:40 / 翌7:20    … keirin-result.yml (結果報告)
- 23:50             … keirin-learn.yml (重みの学習)

## データ(リポジトリの data/ に保存)
- data/picks/*.json   … 配信した買い目
- data/results.csv    … 判定済みの結果、的中・回収率の集計元
- data/learn/*.jsonl  … 学習用の特徴量(配信時点のもの)
- data/model.json     … 学習済みの重み(150レース以上たまると使われる)
- data/articles/*.md  … note用に生成した記事
- data/note_state.json… noteの自動ログイン用Cookie(note連携を使う場合)

## 評価の考え方
- 選手の得点に、3連対率・決まり手・直近成績・並び(番手の差し/地元/飛びつき)による
  補正を加えたものを「評価点」とする。重みは data/model.json があればそれを使い、
  無ければ初期値(PRIOR_WEIGHTS)を使う。
- ガールズ競輪はラインが無いため、個人の力量(得点・決まり手・直近成績)だけで評価し、
  上位選手どうしの力量比較を文章で示す。

## note / X連携について(未検証)
- keirin_publish.py に実装済みだが、実際のnote.comへの自動ログイン・投稿は未検証。
  ToS・二段階認証・画面変更のリスクがあるため、まず手動実行で少数回試すこと。
- X(Twitter)投稿は公式APIを使うため、noteより安定。
