# レースが終わるたびに、的中したものだけをすぐLINEで知らせる
# 1日の終わりの結果報告(keirin_result.py)とは別に、頻繁に実行する
from keirin_line import send_line
from keirin_track import build_alert, process_pending


def main():
    rows = process_pending()
    hits = [r for r in rows if str(r.get("status")) == "ok" and str(r.get("hit")) == "1"]
    if not hits:
        print("新しい的中はありませんでした")
        return
    print(f"的中 {len(hits)}件、速報を送ります")
    send_line(build_alert(hits))


if __name__ == "__main__":
    main()
