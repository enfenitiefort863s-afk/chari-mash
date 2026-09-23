# LINEに送るはずの文面を、鍵つきのページ(GitHub Pages)にも残す。
# パスワードを知らないと読めないよう、中身そのものを暗号化する(見た目のロックだけではない)。
# GitHub Pagesの設定: リポジトリを Public にし、Settings > Pages で
#   Source: Deploy from a branch / Branch: main, フォルダ: /docs を選ぶ。
import base64
import json
import os
import shutil
from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))
DATA_DIR = os.environ.get("DATA_DIR", "data")
DOCS_DIR = "docs"
LOG_PATH = os.path.join(DATA_DIR, "web_log.json")
MAX_ENTRIES = 150     # ページに残す件数(古いものから捨てる)


def _load_log():
    if os.path.exists(LOG_PATH):
        try:
            return json.load(open(LOG_PATH, encoding="utf-8"))
        except Exception:
            return []
    return []


def _save_log(entries):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(entries[-MAX_ENTRIES:], f, ensure_ascii=False, indent=1)


def publish(texts, label=""):
    """LINEに送る(はずの)文面を、ページ用のログに追記して暗号化する。
    LINE送信が失敗しても、これは独立して呼び出すこと"""
    if isinstance(texts, str):
        texts = [texts]
    now = datetime.now(JST)
    entries = _load_log()
    for t in texts:
        entries.append({"time": now.strftime("%m/%d %H:%M"), "label": label, "text": t})
    entries = entries[-MAX_ENTRIES:]
    _save_log(entries)
    _encrypt_and_write(entries)


def _encrypt_and_write(entries):
    password = os.environ.get("PAGE_PASSWORD")
    if not password:
        print("PAGE_PASSWORD が未設定のため、ページの更新はスキップします")
        return
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        from cryptography.hazmat.primitives import hashes
    except ImportError:
        print("cryptography が入っていないため、ページの更新はスキップします(requirements.txtを確認)")
        return

    salt = os.urandom(16)
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=100_000)
    key = kdf.derive(password.encode("utf-8"))
    iv = os.urandom(12)
    plaintext = json.dumps(entries, ensure_ascii=False).encode("utf-8")
    ciphertext = AESGCM(key).encrypt(iv, plaintext, None)   # 末尾にGCMの認証タグが付く(ブラウザ側と同じ形式)

    os.makedirs(DOCS_DIR, exist_ok=True)
    payload = {
        "salt": base64.b64encode(salt).decode(),
        "iv": base64.b64encode(iv).decode(),
        "data": base64.b64encode(ciphertext).decode(),
        "updated": datetime.now(JST).strftime("%m/%d %H:%M"),
    }
    with open(os.path.join(DOCS_DIR, "data.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f)

    shell = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_shell.html")
    if os.path.exists(shell):
        shutil.copy(shell, os.path.join(DOCS_DIR, "index.html"))
    nojekyll = os.path.join(DOCS_DIR, ".nojekyll")
    if not os.path.exists(nojekyll):
        open(nojekyll, "w").close()
    print(f"ページを更新しました({len(entries)}件)")
