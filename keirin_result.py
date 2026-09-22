# レース結果を集計して、的中率・回収率をLINEに報告する
from keirin_line import send_line
from keirin_track import build_report, process_pending


def main():
    rows = process_pending()
    if not rows:
        print("新しく判定できるレースはありませんでした")
        return
    print(f"{len(rows)}件を判定しました")
    send_line(build_report(rows))


if __name__ == "__main__":
    main()
