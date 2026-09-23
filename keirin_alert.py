# レースが終わるたびに、的中したものだけをすぐLINEで知らせる
# 1日の終わりの結果報告(keirin_result.py)とは別に、頻繁に実行する
import keirin_web as kw
from keirin_line import send_line
from keirin_track import build_alert, process_pending


def main():
    rows = process_pending()
    hits = [r for r in rows if str(r.get("status")) == "ok" and str(r.get("hit")) == "1"]
    if not hits:
        print("新しい的中はありませんでした")
        return
    print(f"的中 {len(hits)}件、速報を送ります")
    text = build_alert(hits)
    try:
        kw.publish(text, label="的中速報")
    except Exception as e:
        print("ページの更新に失敗(LINE配信は続けます):", e)
    try:
        send_line(text)
    except Exception as e:
        print("LINE送信に失敗しました(ページには反映済みです):", e)


if __name__ == "__main__":
    main()
