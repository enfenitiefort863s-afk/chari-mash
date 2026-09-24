# レース結果を集計して、的中率・回収率をLINEに報告する
import keirin_web as kw
from keirin_line import send_line
from keirin_track import build_report, has_today_rows, process_pending


def main():
    rows = process_pending()
    print(f"{len(rows)}件を新しく判定しました" if rows else "新しく判定できるレースはありませんでした")

    # 的中速報ですでに全部拾われていて「新しく判定するもの」が0件でも、
    # 本日ぶんの記録があれば、1日の結果報告は必ず送る
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


if __name__ == "__main__":
    main()
