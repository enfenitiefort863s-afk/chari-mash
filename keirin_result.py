# レース結果を集計して、的中率・回収率をLINEに報告する
import keirin_web as kw
from keirin_line import send_line
from keirin_track import build_report, process_pending


def main():
    rows = process_pending()
    if not rows:
        print("新しく判定できるレースはありませんでした")
        return
    print(f"{len(rows)}件を判定しました")
    text = build_report(rows)
    try:
        kw.publish(text, label="結果報告")
    except Exception as e:
        print("ページの更新に失敗(LINE配信は続けます):", e)
    try:
        send_line(text)
    except Exception as e:
        print("LINE送信に失敗しました(ページには反映済みです):", e)


if __name__ == "__main__":
    main()
