# note記事の生成・保存と、(任意で)note自動投稿・X(Twitter)自動告知
#
# 【重要・先に読んでください】
# - note.comは自動ログイン・自動投稿を公式には想定していません。パスワードを保存して
#   自動ログインする実装(下のNotePoster)は、noteの利用規約に触れる可能性があり、
#   二段階認証や画面変更で突然動かなくなることもあります。実運用の前に、まず自分の
#   アカウントで少数回、手動確認しながら試すことを強く勧めます。
# - 有料記事として販売する場合、「当たる」ことを保証しない旨(参考情報である旨)を
#   記事内に明記してください。景品表示法・消費者契約法などに触れない表現かどうかは、
#   最終的にご自身で確認してください。
# - X(Twitter)の自動投稿は公式API(有料プランあり)を使うので、note側より安定です。
#
# 必要なライブラリ:
#   pip install playwright tweepy
#   playwright install chromium
#
# 環境変数(GitHubのSecretsに設定):
#   NOTE_EMAIL, NOTE_PASSWORD          … noteのログイン情報(自動投稿を使う場合)
#   TWITTER_API_KEY, TWITTER_API_SECRET
#   TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_SECRET   … X APIの認証情報(自動告知を使う場合)

import glob
import os
import re
from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))
DATA_DIR = os.environ.get("DATA_DIR", "data")
ARTICLE_DIR = os.path.join(DATA_DIR, "articles")
STATE_PATH = os.path.join(DATA_DIR, "note_state.json")   # noteのログインCookieを保存(任意)


# ================= 記事の生成 =================
def build_article(by_venue):
    """会場ごとの本命・荒れをもとに、note記事の(タイトル, 本文)を作る"""
    jst = datetime.now(JST)
    title = f"{jst.month}/{jst.day} 競輪 本日の厳選予想【{len(by_venue)}会場】"
    out = [f"# {jst.month}月{jst.day}日 競輪予想", "",
           "本日出走の中から、データ分析にもとづく本命レース・荒れそうなレースをまとめました。", ""]
    for venue, picks in by_venue.items():
        out.append(f"## {venue}競輪")
        for kind, x in picks:
            icon = "🎯本命" if kind == "honmei" else "🌪荒れ"
            axis = x["honmei_axis"] if kind == "honmei" else x["ara_axis"]
            partners = x["honmei_partners"] if kind == "honmei" else x["ara_partners"]
            tri = x["honmei_tri"] if kind == "honmei" else x["ara_tri"]
            who = (x["by_car"].get(axis) or {}).get("name", "")
            flow = (x["honmei_flow"] if kind == "honmei" else x["ara_flow"])[:2]
            out.append(f"### {x['race']}R（{icon}）")
            out += ["- " + t for t in flow]
            out.append(f"- 軸: {axis}番 {who}")
            out.append(f"- 2車単: {axis}→{'・'.join(map(str, partners))}")
            out.append(f"- 3連単フォーメーション: {tri['first']}→"
                       f"{'・'.join(map(str, tri['second']))}→{'・'.join(map(str, tri['third']))}")
            out.append("")
    out += ["---", "※本記事は参考情報であり、的中・回収を保証するものではありません。",
            "馬券・車券の購入は、ご自身の判断と責任でお願いします。"]
    return title, "\n".join(out)


def save_article(title, body):
    """記事を data/articles/YYYYMMDD_HHMM.md として保存する(note投稿の有無に関わらず残す)"""
    os.makedirs(ARTICLE_DIR, exist_ok=True)
    now = datetime.now(JST)
    path = os.path.join(ARTICLE_DIR, now.strftime("%Y%m%d_%H%M") + ".md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# {title}\n\n{body}\n")
    print(f"記事を保存しました: {path}")
    return path


def latest_article():
    """一番新しい記事ファイルの(タイトル, 本文, パス)を返す"""
    files = sorted(glob.glob(os.path.join(ARTICLE_DIR, "*.md")))
    if not files:
        return None
    path = files[-1]
    text = open(path, encoding="utf-8").read()
    m = re.match(r"# (.+)\n\n(.*)", text, re.S)
    title, body = (m.group(1), m.group(2)) if m else (os.path.basename(path), text)
    return title, body, path


# ================= X(Twitter)への告知投稿 =================
def post_tweet(text):
    """X APIでツイートする。tweepyと4つの認証情報が必要"""
    import tweepy
    client = tweepy.Client(
        consumer_key=os.environ["TWITTER_API_KEY"],
        consumer_secret=os.environ["TWITTER_API_SECRET"],
        access_token=os.environ["TWITTER_ACCESS_TOKEN"],
        access_token_secret=os.environ["TWITTER_ACCESS_SECRET"],
    )
    resp = client.create_tweet(text=text[:270])
    print("X投稿:", resp)
    return resp


# ================= noteへの自動投稿(実験的・要検証) =================
class NoteAutomationError(RuntimeError):
    pass


def post_to_note(title, body, publish=False, headless=True):
    """Playwrightでnoteにログインし、記事を作成する。
    publish=False なら下書き保存のみ(まずはここから試すことを勧めます)。
    ログインCookieを data/note_state.json に保存し、次回以降は再ログインを省く"""
    from playwright.sync_api import sync_playwright

    email = os.environ.get("NOTE_EMAIL")
    password = os.environ.get("NOTE_PASSWORD")
    if not email or not password:
        raise NoteAutomationError("NOTE_EMAIL / NOTE_PASSWORD が未設定です")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx_kwargs = {}
        if os.path.exists(STATE_PATH):
            ctx_kwargs["storage_state"] = STATE_PATH
        context = browser.new_context(**ctx_kwargs)
        page = context.new_page()

        page.goto("https://note.com/login", timeout=30000)
        if page.locator('input[name="email"]').count() > 0:
            page.fill('input[name="email"]', email)
            page.fill('input[name="password"]', password)
            page.click('button[type="submit"]')
            page.wait_for_load_state("networkidle", timeout=30000)
            os.makedirs(DATA_DIR, exist_ok=True)
            context.storage_state(path=STATE_PATH)   # 次回のためにログイン状態を保存

        page.goto("https://note.com/notes/new", timeout=30000)
        page.wait_for_selector('[contenteditable="true"]', timeout=30000)

        editors = page.locator('[contenteditable="true"]')
        editors.nth(0).click()
        editors.nth(0).type(title, delay=10)
        if editors.count() > 1:
            editors.nth(1).click()
        else:
            page.keyboard.press("Enter")
        page.keyboard.type(body, delay=5)

        if publish:
            page.click('text="公開に進む"')
            page.wait_for_timeout(2000)
            page.click('text="投稿する"')
        else:
            page.wait_for_timeout(2000)   # noteは自動で下書き保存される

        url = page.url
        browser.close()
        return url


# ================= 全体の流れ =================
def main():
    """最新の記事を note に投稿し、X に告知する。個別に手動実行して確認する用"""
    art = latest_article()
    if not art:
        print("記事がありません。先に配信(keirin_run.py)を実行してください")
        return
    title, body, path = art
    print(f"記事: {path}")

    note_url = None
    if os.environ.get("NOTE_EMAIL"):
        try:
            note_url = post_to_note(title, body, publish=os.environ.get("NOTE_PUBLISH") == "1")
            print("note投稿:", note_url)
        except Exception as e:
            print("note投稿に失敗しました(手動で確認してください):", e)
    else:
        print("NOTE_EMAIL未設定のため、note投稿はスキップします")

    if os.environ.get("TWITTER_API_KEY"):
        text = f"【本日の厳選競輪予想】公開しました！\n{title}"
        if note_url:
            text += f"\n{note_url}"
        try:
            post_tweet(text)
        except Exception as e:
            print("X投稿に失敗しました:", e)
    else:
        print("TWITTER_API_KEY未設定のため、X投稿はスキップします")


if __name__ == "__main__":
    main()
