# レース結果を集計して、的中率・回収率をLINEに報告する
import os
from datetime import datetime

import keirin_web as kw
from keirin_line import send_report
from keirin_track import (JST, already_reported, build_report, has_today_rows,
                           mark_reported, performance_stats, process_pending,
                           read_rows, report_date_str)

# 23:40の実行か、7:20の実行かを、ワークフロー側(github.event.schedule)から受け取る。
# 実行時刻に頼ると、実行が深夜まで遅れたときに日付を取り違えるため
SLOT = os.environ.get("SLOT") or None


def report_stamp(target_date, rows, all_rows):
    """「報告済み」の印に使う日付 = 今回実際に報告した内容の日付。
    対象日の行が記録にあるなら対象日。無い場合(前日分の取りこぼしだけを報告した等)は、
    報告した行のうち一番新しい日付にする。実行時の対象日をそのまま焼いてしまうと、
    昼間のテスト実行がその日の本来の報告を「送信済み」と誤判定させてしまうため
    (2026-11-05 に実際に発生した不具合の対策)"""
    if any(str(r.get("date")) == str(target_date) for r in all_rows):
        return target_date
    dates = [str(r.get("date")) for r in rows if r.get("date")]
    return max(dates) if dates else target_date


def main():
    rows = process_pending()
    print(f"{len(rows)}件を新しく判定しました" if rows else "新しく判定できるレースはありませんでした")

    try:
        kw.publish_perf(performance_stats())
    except Exception as e:
        print("成績グラフのページ更新に失敗(集計自体には影響しません):", e)

    target_date = report_date_str(datetime.now(JST), SLOT)
    print(f"対象の日付: {target_date} (slot={SLOT})")

    if not rows and already_reported(target_date):
        print("この日はすでに結果報告を送っており、新しい判定も無いため送信しません")
        return
    if not rows and not has_today_rows(SLOT):
        print("本日ぶんの記録がまだ無いため、結果報告は送りません")
        return

    text = build_report(rows)
    try:
        kw.publish(text, label="結果報告")
    except Exception as e:
        print("ページの更新に失敗(LINE配信は続けます):", e)
    try:
        send_report(rows, text)
    except Exception as e:
        print("送信に失敗しました(ページには反映済みです):", e)
    mark_reported(report_stamp(target_date, rows, read_rows()))


if __name__ == "__main__":
    main()
