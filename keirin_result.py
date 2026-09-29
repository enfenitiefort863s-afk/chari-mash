# レース結果を集計して、的中率・回収率をLINEに報告する
from datetime import datetime

import keirin_web as kw
from keirin_line import send_line
from keirin_track import (JST, already_reported, build_report, has_today_rows,
                           mark_reported, process_pending, report_date_str)


def main():
    rows = process_pending()
    print(f"{len(rows)}件を新しく判定しました" if rows else "新しく判定できるレースはありませんでした")

    target_date = report_date_str(datetime.now(JST))

    if not rows and already_reported(target_date):
        print("この日はすでに結果報告を送っており、新しい判定も無いため送信しません")
        return
    if not rows and not has_today_rows():
        print("本日ぶんの記録がまだ無いため、結果報告は送りません")
        return

    text = build_report(rows)
    try:
        kw.publish(text, label="結果報告")
    except Exception as e:
        print("ページの更新に失敗(LINE配信は続けます):", e)
    try:
        send_line(text)
    except Exception as e:
        print("LINE送信に失敗しました(ページには反映済みです):", e)
    mark_reported(target_date)


if __name__ == "__main__":
    main()
