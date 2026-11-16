# 競輪 出走表取得 + 本命/荒れ分析 + LINE配信
# 必要: requests, beautifulsoup4 (Colabには入っています)
import json
import os
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
import requests
from bs4 import BeautifulSoup, NavigableString

BASE = "https://www.chariloto.com"   # データ取得元(2026-11-15 WinTicket のアクセス制限(403)に伴い chariloto へ移行)
KEIRIN_JP_TOP = "https://www.keirin.jp/pc/top"   # 開催列挙用(公式サイト。GET のみで取得できる)
HEADERS = {"User-Agent": "Mozilla/5.0"}
WORKERS = int(os.environ.get("WORKERS") or 4)       # 同時に取得するページ数(1にすると1ページずつ)
FETCH_DELAY = 0.3                                   # 1つの取得ごとの待ち時間(秒)
LINE_LIMIT = 4500                                   # LINEの1通あたりの上限(UTF-16の文字数。絵文字は2文字分)
ENRICH = os.environ.get("ENRICH", "1") != "0"        # 選手ページの成績も使う(0で無効)
ENRICH_BUDGET = int(os.environ.get("ENRICH_BUDGET", "420"))  # 選手ページ取得の制限時間(秒)
VENUES = [v.strip() for v in os.environ.get("VENUES", "").split(",") if v.strip()]  # 例: toyama,高知(空=全会場)


_tls = threading.local()


def _session():
    s = getattr(_tls, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update(HEADERS)
        _tls.s = s
    return s


def fetch(url, retries=2):
    """HTMLを取得。失敗したら接続を作り直して再試行する
    (死んだ keep-alive 接続を掴んだまま同じセッションでリトライしても、同じ失敗を繰り返すため)"""
    for i in range(retries + 1):
        try:
            r = _session().get(url, timeout=20)
            if r.status_code == 200:
                return r.text
            print(f"  status {r.status_code}: {url}")
        except Exception as e:
            print(f"  error {e}: {url}")
        _tls.s = None   # 次の試行は新しいセッション(新しい接続)で
        time.sleep(2)
    return None


def line_len(text):
    """LINEが数える文字数(UTF-16。絵文字は2文字分)"""
    return len(text.encode("utf-16-le")) // 2


def fit_text(text, limit=LINE_LIMIT):
    """上限を超えるときは、行の区切りで後ろを省く"""
    if line_len(text) <= limit:
        return text
    note = "\n…(文字数の上限で省略)"
    lines = text.split("\n")
    while lines and line_len("\n".join(lines) + note) > limit:
        lines.pop()
    if not lines:                       # 改行が無いまま長い場合は、文字で切る
        t = text
        while t and line_len(t + note) > limit:
            t = t[:-50]
        return t + note
    return "\n".join(lines) + note


def clean(s):
    return unicodedata.normalize("NFKC", "".join(s.split()))


def to_float(s):
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def to_int(s):
    try:
        return int(s)
    except (ValueError, TypeError):
        return None


def _venues_today(date_s):
    """本日(date_s = 'YYYY-MM-DD')開催がある会場コード(jocd)の一覧。
    主: 公式 keirin.jp トップの埋め込みJSON(pc0101_json の RaceList。GETのみで取れる)
    予備: chariloto のどこか1会場の一覧ページ(当日の全会場へのリンクを含む)"""
    ymd = date_s.replace("-", "")
    html = fetch(KEIRIN_JP_TOP, retries=1)
    if html:
        m = re.search(r"pc0101_json\s*=\s*", html)
        if m:
            try:
                obj, _ = json.JSONDecoder().raw_decode(html[m.end():])
                codes = []
                for r in obj.get("RaceList") or []:
                    cd = str(r.get("naibuKeirinCd") or "")
                    if str(r.get("kaisaiDate")) == ymd and cd and cd not in codes:
                        codes.append(cd)
                if codes:
                    return codes
            except Exception as e:
                print("keirin.jp 開催一覧の解析に失敗:", e)
    for seed in ("28", "34", "42", "31", "86"):
        html = fetch(f"{BASE}/keirin/athletes/{date_s}/{seed}", retries=0)
        if not html:
            continue
        codes = sorted(set(re.findall(
            rf"/keirin/athletes/{re.escape(date_s)}/(\d+)(?:[\"/?])", html)))
        if codes:
            return codes
    return []


def get_today_races():
    """今日の全レースを集める。各レースのURLは chariloto の出走表ページ。
    会場の列挙は keirin.jp(公式)、レース番号は chariloto の会場別一覧ページから取る。
    日次(day)は一覧では分からないため None にし、parse_race が race_info から補う"""
    date_s = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")
    races = []
    for jocd in _venues_today(date_s):
        venue = JOCD_VENUE.get(jocd)
        if not venue:
            continue
        html = fetch(f"{BASE}/keirin/athletes/{date_s}/{jocd}", retries=1)
        if not html:
            continue
        pat = re.compile(rf"^/keirin/athletes/{re.escape(date_s)}/{jocd}/(\d+)$")
        soup = BeautifulSoup(html, "html.parser")
        seen = set()
        for a in soup.find_all("a", href=True):
            m = pat.match(a["href"])
            if m and a["href"] not in seen:
                seen.add(a["href"])
                races.append({
                    "venue": venue,
                    "cup": date_s.replace("-", ""),   # 開催の識別子(結果取得は日付ベースなので、日付で十分)
                    "day": None,
                    "race": int(m.group(1)),
                    "url": BASE + a["href"],
                    "date": date_s,
                    "jocd": jocd,
                })
        time.sleep(FETCH_DELAY)
    races.sort(key=lambda x: (x["venue"], x["race"]))
    return races

def _day_number(spent_days_string):
    """'2日目' -> 2 / '初日' -> 1 / '最終日' など数値化できないものは None"""
    s = spent_days_string or ""
    if "初日" in s:
        return 1
    m = re.search(r"(\d+)\s*日目", s)
    return int(m.group(1)) if m else None


def form_from_history(a, n=10):
    """chariloto race_info 内蔵の直近成績(history_data1〜3)から、
    旧 compute_form と同じ形の form {n, avg, win, top2, top3, acc, trend, kim} を作る。
    history は開催単位で新しい順(data1 が直近。data1 は開催中の当開催分を含む)、
    開催内は時系列順(予→準→決)に並んでいるので、開催内を逆順にして新しい順にそろえる。
    事故(落車・失格・棄権)は short_boxes の note の「落/失/棄」から拾う(rank=0 は着順なし)。
    連対時の決まり手内訳(kim)は元データに無いため空(現行でも実質未使用)"""
    entries = []          # (着順 or None, 事故か)
    for key in ("history_data1", "history_data2", "history_data3"):
        boxes = (a.get(key) or {}).get("short_boxes") or []
        for b in reversed(boxes):
            rank = to_int(b.get("rank"))
            note = b.get("note") or ""
            acc = any(c in note for c in ("落", "失", "棄"))
            entries.append((rank, acc))
    if not entries:
        return None
    recent = entries[:n]
    orders = [rk if rk and rk >= 1 else 9 for rk, _ in recent]   # 着順なしは9着扱い(旧 compute_form と同じ)
    cnt = len(orders)
    form = {
        "n": cnt,
        "avg": sum(orders) / cnt,
        "win": sum(1 for o in orders if o == 1) / cnt,
        "top2": sum(1 for o in orders if o <= 2) / cnt,
        "top3": sum(1 for o in orders if o <= 3) / cnt,
        "acc": sum(1 for rk, ac in recent if ac or not rk),
        "trend": 0.0,   # プラス=上り調子(直近5走が、その前より良い)
        "kim": {},
    }
    if cnt >= 8:
        head, tail = orders[:5], orders[5:]
        form["trend"] = sum(tail) / len(tail) - sum(head) / len(head)
    return form


def rider_from_athlete(a):
    """chariloto race_info の選手データ1件を、分析層が使う従来形式の rider dict に変換する。
    ついでに直近成績(form)を _FORM_CACHE に、縁故(rel)は取得できないため空で _REL_CACHE に入れる"""
    car = to_int(a.get("entry_num"))
    if car is None:
        return None
    pid = str(a.get("un_id")) if a.get("un_id") is not None else None
    r = {
        "車": car,
        "選手名": a.get("name") or "",
        "name": clean(a.get("name") or ""),
        "pref": a.get("pref_name") or "",
        "grade": clean((a.get("class_name") or "") + (a.get("group_name") or "")),   # 全角Ａ１ -> A1
        "age": to_int(a.get("age")),
        "term": to_int(a.get("graduate_period")),
        "score": to_float(a.get("average_point_formatted")),   # 競走得点(生値はx1000なので formatted を使う)
        "競走得点": a.get("average_point_formatted"),
        "3連対率": a.get("average_rentai3_formatted"),
        "脚": (a.get("leg_quality") or "").strip(),
        "逃": to_int(a.get("nige_count")) or 0,
        "捲": to_int(a.get("makuri_count")) or 0,
        "差": to_int(a.get("sashi_count")) or 0,
        "マ": to_int(a.get("mark_count")) or 0,
        "ギヤ": a.get("gear_ratio"),
        "pid": pid,
    }
    if pid:
        if pid not in _FORM_CACHE:
            _FORM_CACHE[pid] = form_from_history(a)
        if pid not in _REL_CACHE:
            _REL_CACHE[pid] = {}   # 縁故は新ソースでは取得できない(フェーズ2)。空なら jump_features は加点0で安全
    return r


def parse_lines_html(html):
    """出走表ページの『周回予想』テーブルから並び [[5,6],[2,3],...] を作る。
    車番のセルがラインの並び、空セル(span.p10)がグループの区切り。末尾の空セルは桁そろえ用"""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup.find_all("table"):
        if "周回予想" not in t.get_text():
            continue
        inner = t.find("table")
        if inner is None:
            continue
        lines, cur = [], []
        for td in inner.find_all("td"):
            v = to_int(td.get_text(strip=True))
            if v is None:
                if cur:
                    lines.append(cur)
                    cur = []
            else:
                cur.append(v)
        if cur:
            lines.append(cur)
        return lines or None
    return None


_RACE_URL_PAT = re.compile(r"/keirin/athletes/(\d{4}-\d{2}-\d{2})/(\d+)/(\d+)$")


def parse_race(url):
    """chariloto の出走表1レースを取得する。
    選手データは JSON API (race_info)、並びは出走表ページHTMLの周回予想から取る"""
    m = _RACE_URL_PAT.search(url or "")
    if not m:
        return None
    date_s, jocd, race_no = m.group(1), m.group(2), int(m.group(3))
    raw = fetch(f"{BASE}/api/keirin/race_info"
                f"?open_day={date_s}&vel_code={jocd}&race_num={race_no}")
    if not raw:
        return None
    try:
        info = json.loads(raw)
    except Exception as e:
        print("race_info の解析に失敗:", e)
        return None
    box = info.get("box") or {}
    riders = []
    for a in (info.get("race_athletes") or {}).values():
        r = rider_from_athlete(a)
        if r:
            riders.append(r)
    riders.sort(key=lambda r: r["車"])
    if not riders:
        return None
    html = fetch(url)
    lines = parse_lines_html(html) if html else None
    # 締切は sale_to_at (ISO8601) から。旧形式の title 文字列(deadline_min / is_midnight_race が
    # 読む形)に合わせて組み立て、分析層・セッション振り分けを無変更で動かす
    dl_txt = ""
    try:
        dt = datetime.fromisoformat(box.get("sale_to_at") or "")
        dl_txt = f"締切 {dt.hour}:{dt.minute:02d}"
    except Exception:
        pass
    title = " ".join(p for p in (
        box.get("vel_name") or "", box.get("open_grade_code") or "",
        box.get("race_name") or "", dl_txt,
        "ミッドナイト" if box.get("night_race_type") == "midnight" else "",
    ) if p)
    return {
        "url": url,
        "title": title,
        "riders": riders,
        "lines": lines,
        "venue": JOCD_VENUE.get(jocd),
        "race": race_no,
        "day": _day_number(box.get("spent_days_string")),
        "cup": (box.get("open_day") or date_s).replace("-", ""),
        "date": box.get("open_day") or date_s,
        "jocd": jocd,
    }

def is_upcoming(deadline, now=None):
    """締切(0時からの分)がまだ先か。取れなかったときは先とみなす"""
    if deadline is None:
        return True
    now = now_min_jst() if now is None else now
    d = deadline
    if now >= 18 * 60 and d < 6 * 60:
        d += 24 * 60          # 夜の配信では、深夜(0〜6時)のレースは翌日扱い
    return d > now


def fetch_all_today(limit=None, only_upcoming=True):
    """今日の全レースを取得して返す。
    会場ごとに並行して取得し、各会場は後ろのレースから順に見て、
    締切済みのレースに当たったらそこで止める(それより前も締切済みのため)"""
    races = [r for r in get_today_races() if venue_ok(r["venue"])]
    if limit:
        races = races[:limit]
    by_venue = {}
    for r in races:
        by_venue.setdefault(r["venue"], []).append(r)
    now = now_min_jst()
    counter = {"n": 0}

    def work(rcs):
        out = []
        for rc in sorted(rcs, key=lambda x: -x["race"]):
            data = parse_race(rc["url"])
            counter["n"] += 1
            if data:
                dl = deadline_min(data["title"])
                if only_upcoming and not is_upcoming(dl, now):
                    break                       # これより前のレースは締切済み
                data.update({"venue": rc["venue"], "race": rc["race"], "cup": rc["cup"],
                             "date": rc.get("date")})
                if data.get("day") is None:
                    data["day"] = rc.get("day")   # 日次は race_info 側を優先(一覧では取れない)
                out.append(data)
            time.sleep(FETCH_DELAY)
        return out

    results = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for out in ex.map(work, list(by_venue.values())):
            results += out
    results.sort(key=lambda x: (x["venue"], x["race"]))
    print(f"レースページ {counter['n']}/{len(races)}件を取得、対象 {len(results)}レース")
    return results

# ================= 選手ページ(直近成績) =================

def fetch_player_form(pid):
    """直近成績は race_info に同梱されており、parse_race の時点で _FORM_CACHE に入っている。
    ここではキャッシュを返すだけ(選手ページへの追加アクセスはしない)。
    縁故(relations)は新ソースでは取得できないため、常に空(フェーズ2で keirin.jp 併用を検討)"""
    if pid not in _REL_CACHE:
        _REL_CACHE[pid] = {}
    return _FORM_CACHE.get(pid)

_FORM_CACHE = {}
_REL_CACHE = {}


# ================= 分析 =================

VENUE_JP = {
    "hakodate": "函館", "aomori": "青森", "iwakidaira": "いわき平",
    "yahiko": "弥彦", "maebashi": "前橋", "toride": "取手",
    "utsunomiya": "宇都宮", "omiya": "大宮", "seibuen": "西武園",
    "keiokaku": "京王閣", "tachikawa": "立川", "matsudo": "松戸",
    "chiba": "千葉", "kawasaki": "川崎", "hiratsuka": "平塚",
    "odawara": "小田原", "ito": "伊東", "shizuoka": "静岡",
    "nagoya": "名古屋", "gifu": "岐阜", "ogaki": "大垣",
    "toyohashi": "豊橋", "toyama": "富山", "matsusaka": "松阪",
    "yokkaichi": "四日市", "fukui": "福井", "nara": "奈良",
    "mukomachi": "向日町", "wakayama": "和歌山", "kishiwada": "岸和田",
    "tamano": "玉野", "hiroshima": "広島", "hofu": "防府",
    "takamatsu": "高松", "komatsushima": "小松島", "kochi": "高知",
    "matsuyama": "松山", "kokura": "小倉", "kurume": "久留米",
    "takeo": "武雄", "sasebo": "佐世保", "beppu": "別府", "kumamoto": "熊本",
}

VENUE_JOCD = {   # 会場スラッグ -> 公式会場コード(jocd)。chariloto / keirin.jp のURLで使う
    "hakodate": "11", "aomori": "12", "iwakidaira": "13", "yahiko": "21",
    "maebashi": "22", "toride": "23", "utsunomiya": "24", "omiya": "25",
    "seibuen": "26", "keiokaku": "27", "tachikawa": "28", "matsudo": "31",
    "chiba": "32", "kawasaki": "34", "hiratsuka": "35", "odawara": "36",
    "ito": "37", "shizuoka": "38", "nagoya": "42", "gifu": "43",
    "ogaki": "44", "toyohashi": "45", "toyama": "46", "matsusaka": "47",
    "yokkaichi": "48", "fukui": "51", "nara": "53", "mukomachi": "54",
    "wakayama": "55", "kishiwada": "56", "tamano": "61", "hiroshima": "62",
    "hofu": "63", "takamatsu": "71", "komatsushima": "73", "kochi": "74",
    "matsuyama": "75", "kokura": "81", "kurume": "83", "takeo": "84",
    "sasebo": "85", "beppu": "86", "kumamoto": "87",
}
JOCD_VENUE = {v: k for k, v in VENUE_JOCD.items()}


VENUE_BANK = {   # (周長m, みなし直線m)。出典: 各競輪場公式コース案内(oddspark等)。
    # 直線が長い/カントが緩いバンクほど差し・追い込みが決まりやすく、
    # 直線が短い/カントがきついバンクほど先行・自在が決まりやすい傾向がある
    "hakodate": (400, 51.3), "aomori": (400, 58.9), "iwakidaira": (400, 62.7),
    "yahiko": (400, 63.1), "maebashi": (335, 46.6), "toride": (400, 54.8),
    "utsunomiya": (500, 63.3), "omiya": (500, 66.7), "seibuen": (400, 47.6),
    "keiokaku": (400, 51.5), "tachikawa": (400, 58.0), "matsudo": (333, 38.2),
    "chiba": (500, 65.0), "kawasaki": (400, 58.0), "hiratsuka": (400, 54.2),
    "odawara": (333, 36.1), "ito": (335, 46.6), "shizuoka": (400, 56.4),
    "nagoya": (400, 58.8), "gifu": (400, 59.3), "ogaki": (400, 56.0),
    "toyohashi": (400, 60.3), "toyama": (333, 43.0), "matsusaka": (400, 61.5),
    "yokkaichi": (400, 62.4), "fukui": (400, 52.8), "nara": (400, 56.0),
    "mukomachi": (400, 47.3), "wakayama": (400, 59.9), "kishiwada": (400, 56.7),
    "tamano": (400, 47.9), "hiroshima": (400, 57.9), "hofu": (333, 42.5),
    "takamatsu": (400, 54.8), "komatsushima": (400, 55.5), "kochi": (400, 56.0),
    "matsuyama": (400, 58.6), "kokura": (400, 56.9), "kurume": (400, 50.7),
    "takeo": (400, 64.4), "sasebo": (400, 40.2), "beppu": (400, 59.9),
    "kumamoto": (500, 65.0),
}
BANK_AVG_STRAIGHT = {333: 42.0, 335: 42.0, 400: 56.0, 500: 65.0}   # 周長ごとの、みなし直線の目安平均


def bank_bias(venue_key):
    """このバンクが、先行型寄り(マイナス)か差し・追い込み型寄り(プラス)かを -1.0〜+1.0 で返す。
    周長そのもの(333/335mは先行寄り、500mは差し寄り)を主要因とし、
    同じ周長の中での直線の長さで微調整する。データが無い会場は 0.0(中立)"""
    b = VENUE_BANK.get(venue_key)
    if not b:
        return 0.0
    length, straight = b
    base = {333: -0.35, 335: -0.35, 400: 0.0, 500: 0.35}.get(length, 0.0)
    avg = BANK_AVG_STRAIGHT.get(length, 56.0)
    extra = (straight - avg) / 30.0
    return max(-1.0, min(1.0, base + extra))


def bank_bonus(riders, venue_key):
    """バンク適性による加点。逃げ型は先行有利バンクで、追い込み型は差し有利バンクで加点する。
    決まり手の実績(自力/マーク)が多い選手ほど、恩恵・不利とも大きくする。
    戻り値: ({車番: 加点}, [(重み, 説明), ...])"""
    bias = bank_bias(venue_key)
    if abs(bias) < 0.1:
        return {}, []
    bonus, notes = {}, []
    for r in riders:
        kyaku = r.get("脚")
        if kyaku == "逃":
            factor = min(1.5, 0.3 + 0.15 * jiriki_wins(r))
            b = -bias * factor
        elif kyaku == "追":
            factor = min(1.5, 0.3 + 0.15 * follow_wins(r))
            b = bias * factor
        else:
            b = bias * 0.3
        b = round(b, 2)
        if abs(b) >= 0.3:
            bonus[r["車"]] = b
    if bonus:
        top_car = max(bonus, key=lambda c: bonus[c])
        if bonus[top_car] > 0:
            notes.append(("bank", bonus[top_car],
                          f"直線が長め・差し追い込み向きのバンク、{circ(top_car)}{name_of({r['車']: r for r in riders}, top_car)}に追い風"))
        elif min(bonus.values()) < 0 and bank_bias(venue_key) < 0:
            lead = [r for r in riders if r.get("脚") == "逃"]
            if lead:
                notes.append(("bank", abs(bias),
                              f"直線が短め・先行向きのバンク、{circ(lead[0]['車'])}{lead[0]['name']}が残しやすい"))
    return bonus, notes


VENUE_PREF = {   # 会場の都道府県(選手の登録地と同じ表記。地元選手の判定に使う)
    "hakodate": "北海道", "aomori": "青森", "iwakidaira": "福島",
    "yahiko": "新潟", "maebashi": "群馬", "toride": "茨城",
    "utsunomiya": "栃木", "omiya": "埼玉", "seibuen": "埼玉",
    "keiokaku": "東京", "tachikawa": "東京", "matsudo": "千葉",
    "chiba": "千葉", "kawasaki": "神奈川", "hiratsuka": "神奈川",
    "odawara": "神奈川", "ito": "静岡", "shizuoka": "静岡",
    "nagoya": "愛知", "gifu": "岐阜", "ogaki": "岐阜",
    "toyohashi": "愛知", "toyama": "富山", "matsusaka": "三重",
    "yokkaichi": "三重", "fukui": "福井", "nara": "奈良",
    "mukomachi": "京都", "wakayama": "和歌山", "kishiwada": "大阪",
    "tamano": "岡山", "hiroshima": "広島", "hofu": "山口",
    "takamatsu": "香川", "komatsushima": "徳島", "kochi": "高知",
    "matsuyama": "愛媛", "kokura": "福岡", "kurume": "福岡",
    "takeo": "佐賀", "sasebo": "長崎", "beppu": "大分", "kumamoto": "熊本",
}


def venue_ok(key):
    """VENUESが空なら全会場。指定があれば、ローマ字(toyama)でも日本語(富山)でも可"""
    if not VENUES:
        return True
    return key in VENUES or VENUE_JP.get(key, key) in VENUES


def is_midnight_race(title):
    """ミッドナイト競輪(21時以降〜早朝に締切があるレース)かどうか"""
    if not title:
        return False
    if "ミッドナイト" in title:
        return True
    d = deadline_min(title)
    if d is None:
        return False
    return d >= 21 * 60 or d < 6 * 60   # 21時以降、または深夜〜早朝(前日からの続き)


def deadline_min(title):
    """タイトルから締切時刻(0時からの分)を取る。取れなければNone"""
    m = re.search(r"締切\s*(\d{1,2}):(\d{2})", title or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def now_min_jst():
    n = datetime.now(timezone(timedelta(hours=9)))
    return n.hour * 60 + n.minute


# 評価点 = 競走得点 + Σ(重み × 特徴量)。重みの初期値(点数換算)。
# data/model.json があれば、過去の結果から学習した重みに置き換わる(keirin_learn.py)
PRIOR_WEIGHTS = {"t3": 1.0, "jiriki": 2.0, "mark": 1.5,
                 "pos_follow": 2.5, "pos_local": 1.0, "jump": 1.5, "pos_drop": 1.5}
LEARN_KEYS = list(PRIOR_WEIGHTS)
MODEL_PATH = os.environ.get("MODEL_PATH") or "data/model.json"
MIN_MODEL_RACES = 150          # 学習結果を使うのに必要なレース数


def load_weights(path=None):
    """(男子の重み, ガールズの重み, 学習に使ったレース数)。学習結果が無い/少ないときは初期値"""
    men, girls, n = dict(PRIOR_WEIGHTS), dict(PRIOR_WEIGHTS), 0
    try:
        with open(path or MODEL_PATH, encoding="utf-8") as f:
            m = json.load(f)
        if int(m.get("n_races", 0)) >= MIN_MODEL_RACES:
            n = int(m["n_races"])
            men.update({k: float(v) for k, v in (m.get("men") or {}).items() if k in men})
            girls.update({k: float(v) for k, v in (m.get("girls") or {}).items() if k in girls})
    except Exception:
        pass
    return men, girls, n


WEIGHTS, WEIGHTS_GIRLS, MODEL_RACES = load_weights()


def load_min_score(path=None):
    """実際の的中率・回収率から学習した、配信してよいスコアの下限値(keirin_learn.py が作る)"""
    try:
        with open(path or MODEL_PATH, encoding="utf-8") as f:
            m = json.load(f)
        return {k: float(v) for k, v in (m.get("min_score") or {}).items()}
    except Exception:
        return {}


MIN_SCORE = load_min_score()


def load_min_score_venue(path=None):
    """会場別に学習したスコアの下限値。無い会場は全体の下限値を使う"""
    try:
        with open(path or MODEL_PATH, encoding="utf-8") as f:
            m = json.load(f)
        raw = m.get("min_score_venue") or {}
        return {kind: {v: float(t) for v, t in (per or {}).items()}
                for kind, per in raw.items()}
    except Exception:
        return {}


MIN_SCORE_VENUE = load_min_score_venue()


def rider_feats(r):
    """選手ごとの特徴量。3連対率・自力(逃/捲)・差し/マークの実績"""
    def k(key):
        return to_int(r.get(key)) or 0

    t3 = to_float(r.get("3連対率"))
    jf = min(1.0, (k("逃") + k("捲")) / 6.67)
    mf = min(1.0, (k("差") + k("マ")) / 10.0)
    style = r.get("脚")
    if style == "逃":
        j, m = jf, 0.0
    elif style == "追":
        j, m = 0.0, mf
    else:
        j, m = 0.6 * jf, 0.6 * mf
    return {"t3": max(-2.0, min(2.0, (t3 - 40) / 20)) if t3 is not None else 0.0,
            "jiriki": j, "mark": m}


def rider_rating(r, girls=False):
    """評価点 = 競走得点 + 重み×特徴量 + 直近成績の補正(重みは仮。学習で更新)"""
    w = WEIGHTS_GIRLS if girls else WEIGHTS
    f = rider_feats(r)
    adj = sum(w[key] * v for key, v in f.items())
    fm = r.get("form")
    if fm and fm["n"] >= 5:
        adj += max(-1.5, min(1.5, (5.0 - fm["avg"]) * 0.4))      # 直近の平均着順
        if fm["n"] >= 8:
            adj += max(-1.4, min(1.4, fm["trend"] * 0.45))         # 上り調子/下り調子を強めに反映(2026-11-10 ユーザー要望)
        adj -= min(1.0, 0.5 * fm["acc"])                           # 落車・失格など
    r["rating"] = r["score"] + adj
    return r["rating"]


def name_of(by_car, car):
    r = by_car.get(car)
    return r["name"] if r else ""


def is_girls_race(race, riders):
    """ガールズケイリンか(規則上ラインを組めず、個人の力量と作戦だけで決まる)。
    判定は、出走する選手の級班(L1級)だけで行う。
    ※タイトルの『ガールズ』は使わない。『F2ミッドナイトガールズ』のように、
      イベント全体の名前に入っているだけで、同じ開催の男子レースまでガールズ扱いになってしまうため。
    級班の読み取りミスで1人だけ誤って判定されても全体がガールズ扱いにならないよう、
    半数以上がL1級のときだけガールズと判定する"""
    if not riders:
        return False
    n_l1 = sum(1 for r in riders if str(r.get("grade") or "") == "L1")
    return n_l1 * 2 >= len(riders)


def jiriki_wins(r):
    return (to_int(r.get("逃")) or 0) + (to_int(r.get("捲")) or 0)


def follow_wins(r):
    return (to_int(r.get("差")) or 0) + (to_int(r.get("マ")) or 0)


REL_WEIGHT = {"師匠": 0.6, "弟子": 0.6, "縁故選手": 0.6, "練習仲間": 0.4}   # 関係の強さ(仮)


def jump_features(lines, by_car):
    """単騎の選手が、縁故・師弟・練習仲間の関係にある別ラインの選手に飛びつく可能性(0〜1)。
    関係が無い選手は見ない。飛びつきの成功率そのものは、公開データに無い"""
    pid_to_car = {r.get("pid"): c for c, r in by_car.items() if r.get("pid")}
    line_of = {c: i for i, ln in enumerate(lines) for c in ln}
    out, notes = {}, []
    for ln in lines:
        if len(ln) != 1:
            continue
        car = ln[0]
        r = by_car.get(car)
        if not r:
            continue
        best = None                      # (関係の強さ, 相手の車番, 関係)
        for typ, ids in (r.get("rel") or {}).items():
            w = REL_WEIGHT.get(typ)
            for pid in ids:
                t = pid_to_car.get(pid)
                if w and t and line_of.get(t) != line_of.get(car) and (best is None or w > best[0]):
                    best = (w, t, typ)
        if best:
            w, t, typ = best
            tr = by_car.get(t) or {}
            strength = w + (0.3 if (tr.get("脚") == "逃" or jiriki_wins(tr) >= 3) else 0.0)
            scale = 0.7 + min(0.6, 0.1 * follow_wins(r))     # 差し・マークの実績が多いほど確からしい
            f = min(1.0, strength * scale / 1.5)
            out[car] = f
            notes.append(("jump", f * 1.5, f"単騎{circ(car)}: {circ(t)}と{typ}の関係(飛びつき期待)"))
    return out, notes


DROP_GAP = 6.0   # 先頭と番手の評価点差がこれ以上なら、捲られたとき千切れるリスクを考え始める


def position_feats(lines, by_car, local_pref):
    """ラインの並び(先頭・番手・3番手)から、位置ごとの特徴量と展開メモを作る。
    戻り値: ({車番: {特徴量名: 値}}, [(種類, 重み, 説明), ...])"""
    feats, notes = {}, []
    for ln in lines:
        head = by_car.get(ln[0]) if ln else None
        if not head or len(ln) < 2:
            continue
        head_power = jiriki_wins(head)
        head_front = head.get("脚") == "逃" or head_power >= 3      # 先頭が自力型か
        for pos, car in enumerate(ln[1:3], start=2):               # 2=番手, 3=3番手
            r = by_car.get(car)
            if not r:
                continue
            f = feats.setdefault(car, {})
            gap = head["rating"] - r["rating"]         # 先頭と、この選手との実力差
            if head_front:
                # 先行選手の番手は、差し・マークで決まりやすい。
                # 先頭の自力の実績と、本人の差し・マークの実績が多いほど大きい
                raw = min(1.5, 0.25 * head_power) + min(1.0, 0.15 * follow_wins(r))
                if pos == 3:
                    raw *= 0.5
                # ただし、先頭が強すぎて本人が力量的に見劣りする場合は、
                # 外から捲られたときに付いていけず千切れるリスクがある。実力差が大きいほど割り引く
                if gap >= DROP_GAP:
                    drop = min(0.75, (gap - DROP_GAP) * 0.08)
                    raw *= (1 - drop)
                    f["pos_drop"] = -min(1.0, (gap - DROP_GAP) * 0.12)
                    if gap >= DROP_GAP + 4:
                        role = "番手" if pos == 2 else "3番手"
                        notes.append(("drop", gap,
                                      f"{circ(ln[0])}が強すぎて{role}{circ(car)}は捲られると千切れるリスク"))
                f["pos_follow"] = raw / 2.5
                if raw >= 0.8:
                    role = "番手" if pos == 2 else "3番手"
                    notes.append(("follow", raw, f"先行{circ(ln[0])}の{role}{circ(car)}に差し・マーク期待"))
            # 地元選手が、地元以外の先頭の後ろ(番手・3番手)にいる → 番手捲り・自力の可能性
            if local_pref and r.get("pref") == local_pref and head.get("pref") != local_pref:
                lb = 1.0 if pos == 2 else 0.6
                f["pos_local"] = lb
                notes.append(("local", lb, f"地元{circ(car)}が{'番手' if pos == 2 else '3番手'}(番手捲りも)"))
    jf, jn = jump_features(lines, by_car)
    for c, v in jf.items():
        feats.setdefault(c, {})["jump"] = v
    return feats, notes + jn


def position_bonus(lines, by_car, local_pref, weights):
    """並びによる加点(点数)。1人3点まで"""
    feats, notes = position_feats(lines, by_car, local_pref)
    bonus = {c: min(3.0, sum(weights[k] * v for k, v in f.items())) for c, f in feats.items()}
    return bonus, notes


def pick_notes(notes, limit=3):
    """展開メモを最大limit個。千切れリスク・地元番手・飛びつき・車番隣接・バンク適性を優先し、
    残りは加点の大きい順"""
    drop = sorted((n for n in notes if n[0] == "drop"), key=lambda n: -n[1])[:1]
    bank = sorted((n for n in notes if n[0] == "bank"), key=lambda n: -n[1])[:1]
    local = sorted((n for n in notes if n[0] == "local"), key=lambda n: -n[1])[:1]
    jump = sorted((n for n in notes if n[0] == "jump"), key=lambda n: -n[1])[:1]
    adjacent = sorted((n for n in notes if n[0] == "adjacent"), key=lambda n: -n[1])[:1]
    follow = sorted((n for n in notes if n[0] == "follow"), key=lambda n: -n[1])
    used = len(drop) + len(bank) + len(local) + len(jump) + len(adjacent)
    follow = follow[:max(0, limit - used)]
    return [n[2] for n in follow + local + jump + adjacent + bank + drop]


def girls_compare(riders, n=3):
    """ガールズ: ラインが無いので、上位選手の力量の根拠(得点順位・決まり手・直近成績)を1行ずつ示す"""
    score_rank = {r["車"]: i for i, r in enumerate(sorted(riders, key=lambda r: -r["score"]), 1)}
    out = []
    for r in riders[:n]:
        parts = [f"得点{r['score']:.1f}({score_rank[r['車']]}位)"]
        j, m = jiriki_wins(r), follow_wins(r)
        if j >= 3:
            parts.append(f"逃げ・捲り勝ち{j}")
        if m >= 3:
            parts.append(f"差し・マーク勝ち{m}")
        fm = r.get("form")
        if fm and fm["n"] >= 3:
            mood = "↗上り調子" if fm["trend"] >= 1.0 else ("↘下り調子" if fm["trend"] <= -1.0 else "")
            parts.append(f"直近平均{fm['avg']:.1f}着{mood}")
        elif r.get("3連対率"):
            parts.append(f"3連対{r['3連対率']}%")
        pb = r.get("pos_bonus") or 0
        if pb >= 0.3:
            parts.append(f"車番隣接+{pb:.1f}")
        style = r.get("脚", "")
        out.append((f"{circ(r['車'])} {r['name']}" + (f"({style})" if style else ""), " / ".join(parts)))
    return out


def girls_adjacency_bonus(riders):
    """ガールズ: ラインが無い分、有力選手(評価トップ)に車番が近い選手は、
    枠入りの位置関係からその後ろについて『マーク』できる可能性が高くなる。
    本人のマークの上手さ(差し・マークの実績)が高いほど、恩恵を大きくする。
    戻り値: ({車番: 加点}, [(重み, 説明), ...])"""
    if len(riders) < 2:
        return {}, []
    ace = max(riders, key=lambda r: r["rating"])
    bonus, notes = {}, []
    for r in riders:
        if r is ace:
            continue
        dist = abs(r["車"] - ace["車"])
        if dist == 1:
            base = 1.0
        elif dist == 2:
            base = 0.4
        else:
            continue
        mark_factor = 0.5 + min(1.0, follow_wins(r) * 0.15)   # マークが上手い選手ほど恩恵が大きい
        b = round(base * mark_factor, 2)
        if b >= 0.3:
            bonus[r["車"]] = b
            notes.append(("adjacent", b,
                          f"{circ(r['車'])}{r['name']}が{circ(ace['車'])}{ace['name']}と車番隣接、マークのチャンス"))
    return bonus, notes


def cross_line_candidate(lines, by_car, exclude=()):
    """複数ラインの先頭同士が拮抗しているとき、それぞれの番手が競り合って
    抜け出す『交差決着』も候補にする。ライン決着だけに偏らないための保険"""
    heads = []   # (先頭の評価点, ライン, 番手選手)
    for ln in lines:
        if len(ln) < 2:
            continue
        h, b = by_car.get(ln[0]), by_car.get(ln[1])
        if h and b and ln[0] not in exclude and ln[1] not in exclude:
            heads.append((h["rating"], ln, b))
    if len(heads) < 2:
        return None
    heads.sort(key=lambda t: -t[0])
    if heads[0][0] - heads[1][0] > 3.0:
        return None   # 先頭の実力差がありすぎるなら、素直に強いほうのラインで決まりやすい
    b1, b2 = heads[0][2], heads[1][2]
    axis, other = (b1, b2) if b1["rating"] >= b2["rating"] else (b2, b1)
    return {
        "axis": axis["車"], "partner": other["車"],
        "heads": (heads[0][1][0], heads[1][1][0]),
    }


def line_strength(ln, by_car):
    """ラインの強さ = 選手の評価点の平均 + 先頭の自力の実績"""
    rs = [by_car[c] for c in ln if c in by_car]
    if not rs:
        return -1e9
    head = by_car.get(ln[0])
    return sum(r["rating"] for r in rs) / len(rs) + (0.3 * jiriki_wins(head) if head else 0)


def _tactic(r):
    """今期の決まり手別勝ち数から、その選手の主戦法を返す (種類, 勝ち数)"""
    cs = [("逃", to_int(r.get("逃")) or 0), ("捲", to_int(r.get("捲")) or 0),
          ("差", to_int(r.get("差")) or 0), ("マ", to_int(r.get("マ")) or 0)]
    cs.sort(key=lambda t: -t[1])
    return cs[0]


def _front_sentence(by_car, lines, main):
    """先行争いの具体化: 誰が・今期どれだけ逃げて連対しているかで主導権を読む"""
    line_of = {c: i for i, ln in enumerate(lines) for c in ln}
    fronts = [(car, r) for car, r in by_car.items() if r.get("脚") == "逃"]
    fronts.sort(key=lambda t: (-(to_int(t[1].get("逃")) or 0), -(t[1].get("score") or 0)))
    if not fronts:
        return "先行型がおらず、誰が風を切るかで隊列が大きく変わる"
    c0, f0 = fronts[0]
    ev0 = to_int(f0.get("逃")) or 0
    if len(fronts) == 1:
        if c0 == main[0]:
            if ev0:
                return (f"先行型は{circ(c0)}{_short_name(f0.get('name', ''))}だけ"
                        f"(今期逃げ切り{ev0}勝)。すんなり主導権を握る公算大")
            return (f"先行型は{circ(c0)}{_short_name(f0.get('name', ''))}だけ。"
                    "逃げ実績は乏しいが、他に先行型がおらず主導権は取りやすい")
        return (f"先行型は別ラインの{circ(c0)}{_short_name(f0.get('name', ''))}だけ。"
                "主力ラインはその直後を取る形になりやすい")
    c1, f1 = fronts[1]
    ev1 = to_int(f1.get("逃")) or 0
    if line_of.get(c0) == line_of.get(c1):
        return (f"先行型2人が同じライン({circ(c0)}・{circ(c1)})におり、"
                "隊列はそのライン中心に組み上がる")
    strong = c0 if (f0.get("score") or 0) >= (f1.get("score") or 0) else c1
    return (f"先行争いは{circ(c0)}{_short_name(f0.get('name', ''))}(今期逃げ{ev0}勝)と"
            f"{circ(c1)}{_short_name(f1.get('name', ''))}(同{ev1}勝)。"
            f"競走得点で上回る{circ(strong)}のラインが主導権優勢。"
            "競り合えばハイペースになり、差し・捲りに展開が向く")


def _form_clause(r):
    """直近成績(選手ページ取得時だけ付く)から、状態を一言で"""
    f = r.get("form")
    if not f:
        return ""
    if f.get("acc", 0) >= 2:
        return f"近走に落車・事故が{f['acc']}回あるのは不安材料"
    if f.get("trend", 0) >= 1.5:
        return "近走ははっきり上り調子"
    if f.get("top3", 0) >= 0.6:
        return f"近走10走の3着内{round(f['top3'] * 100)}%と安定"
    if f.get("win", 0) >= 0.3:
        return f"近走10走で{round(f['win'] * 10)}勝と勝ち切れている"
    if f.get("trend", 0) <= -1.5:
        return "近走は下降気味"
    return ""


def _axis_sentence(x, kind, main, lines):
    """軸の勝ち筋を、その選手の決まり手内訳と直近成績で裏付けて書く"""
    by_car = x["by_car"]
    axis = x[f"{kind}_axis"]
    r = by_car.get(axis) or {}
    nm = _short_name(r.get("name", ""))
    tac, wins = _tactic(r)
    fw, jw = follow_wins(r), jiriki_wins(r)
    ln = next((l for l in lines if axis in l), None)
    parts = []
    if ln and main and ln == main:
        pos = ln.index(axis)
        if pos == 0:
            if r.get("脚") == "逃":
                parts.append(f"◎{circ(axis)}{nm}が先行して押し切る形")
            else:
                parts.append(f"◎{circ(axis)}{nm}がライン先頭から押し切る形")
            if tac == "逃" and wins:
                parts.append(f"今期の逃げ切り{wins}勝がその裏付け")
        elif pos == 1:
            parts.append(f"◎{circ(axis)}{nm}が番手から差す形")
            if fw >= jw and fw:
                parts.append(f"今期は差し・マークで{fw}勝の追い込み型")
            elif jw:
                parts.append(f"自力も今期{jw}勝あり、番手捲りも視野")
        else:
            parts.append(f"◎{circ(axis)}{nm}がライン{pos + 1}番手から差し込む形")
            if fw:
                parts.append(f"今期の差し・マーク{fw}勝の脚が武器")
    elif ln:
        parts.append(f"◎{circ(axis)}{nm}は別ラインの先頭から自力勝負")
        if jw:
            parts.append(f"今期の逃げ・捲りで{jw}勝の実績")
        else:
            parts.append("自力の実績は乏しく、位置取りが鍵")
    else:
        parts.append(f"◎{circ(axis)}{nm}は単騎で捲り・差し狙い")
        if tac in ("捲", "差") and wins:
            parts.append(f"主戦法は{tac}(今期{wins}勝)")
    fc = _form_clause(r)
    if fc:
        parts.append(fc)
    return "。".join(parts)


def _bank_clause(x):
    """バンク特性(直線の長さ)が展開に効く会場だけ、一言添える"""
    b = VENUE_BANK.get(x.get("venue_key") or "")
    if not b:
        return ""
    bias = bank_bias(x["venue_key"])
    _length, straight = b
    if bias >= 0.35:
        return f"{x['venue']}は直線{straight}mと長めで、差し・追い込みが届きやすい"
    if bias <= -0.35:
        return f"{x['venue']}は直線{straight}mと短めで、先行が残りやすい"
    return ""


def _risk_sentence(x, kind, main, ranked):
    """展開が崩れる条件を1つだけ挙げる(千切れ・対抗ラインの先行力)"""
    by_car = x["by_car"]
    axis = x[f"{kind}_axis"]

    def val(r):
        return r["rating"] if r.get("rating") is not None else (r.get("score") or 0)

    if main and len(main) >= 2:
        gap = val(by_car.get(main[0]) or {}) - val(by_car.get(main[1]) or {})
        if gap >= 10.0:   # 評価は番手にも加点されるため、配信文では大きめの差だけ拾う
            if axis == main[0]:
                return (f"番手{circ(main[1])}との力差が大きく、ペースが上がると"
                        "千切れて単騎戦になるリスク")
            if axis == main[1]:
                return f"先頭{circ(main[0])}との力差が大きく、離れず付けられるかが焦点"
            return "主力ラインは先頭と番手の力差が大きく、ちぎれると隊形が崩れる"
    if len(ranked) >= 2 and main:
        rh = by_car.get(ranked[1][0]) or {}
        mh = by_car.get(main[0]) or {}
        if (to_int(rh.get("逃")) or 0) >= (to_int(mh.get("逃")) or 0) + 3:
            return (f"対抗ラインの先頭{circ(ranked[1][0])}の方が逃げ実績で上。"
                    "主導権を取られると苦しい展開")
    return ""


def flow_lines(lines, by_car, girls, riders, n_front, notes):
    """展開予想を、短い文の並びにする(並び予想が無いレースは、個人の力量で書く)"""
    out = []
    if not lines:
        fr = [r for r in riders if r.get("脚") == "逃"][:3]
        if fr:
            out.append("ライン無し。" + "・".join(f"{circ(r['車'])}{r['name']}" for r in fr) + "の仕掛け次第")
        else:
            out.append("ライン無し。個人の力量と位置取りで決まる")
    else:
        cand = [ln for ln in lines if any(c in by_car for c in ln)]
        multi = [ln for ln in cand if len(ln) >= 2]          # 主導権は2人以上のラインから選ぶ
        ranked = sorted(multi or cand, key=lambda ln: -line_strength(ln, by_car))
        if ranked:
            main = ranked[0]
            h = by_car.get(main[0]) or {}
            seg = "-".join(map(str, main))
            if h.get("脚") == "逃":
                out.append(f"主導権は{circ(main[0])}{h.get('name', '')}が握る見込み({seg})")
            else:
                out.append(f"{circ(main[0])}{h.get('name', '')}のライン({seg})が主力")
            if len(ranked) >= 2 and len(ranked[1]) >= 2:
                out.append("対抗は" + "-".join(map(str, ranked[1])) + "ライン")
            out.append(_front_sentence(by_car, lines, main))
    if not lines:
        if n_front >= 3:
            out.append(f"先行型が{n_front}人で主導権争い、ペースは速め")
        elif n_front == 0:
            out.append("先行型が不在で、ペースは落ち着く見込み")
    out += notes
    return out


def analyze(race):
    """1レースを分析して、本命度・荒れ度・買い目・評価コメントを返す"""
    riders = [r for r in race["riders"]
              if r.get("score") is not None and r.get("車") is not None]
    if len(riders) < 5:
        return None
    girls = is_girls_race(race, riders)
    for r in riders:
        rider_rating(r, girls)
        r["pos_bonus"] = 0.0
    by_car = {r["車"]: r for r in riders}

    # ガールズは規則上ラインを組めないため、常にライン無し(個人の力量)で評価する。
    # 男子は、実際に chariloto にある並び予想(周回予想)をそのまま使う(単騎だけの選手がいてもよい)
    lines = [] if girls else (race.get("lines") or [])
    pos_notes = []
    if lines:
        bonus, pos_notes = position_bonus(lines, by_car, VENUE_PREF.get(race["venue"]),
                                          WEIGHTS_GIRLS if girls else WEIGHTS)
        for car, bb in bonus.items():
            if car in by_car:
                by_car[car]["pos_bonus"] = bb
                by_car[car]["rating"] += bb
    else:
        # ラインが無いレース(ガールズなど)は、車番の近さ×マークの上手さで評価する
        bonus, pos_notes = girls_adjacency_bonus(riders)
        for car, bb in bonus.items():
            if car in by_car:
                by_car[car]["pos_bonus"] = bb
                by_car[car]["rating"] += bb

    # バンクの特性(直線の長さ)による適性。ラインの有無に関わらず、脚質に応じて加点する
    bbonus, bnotes = bank_bonus(riders, race["venue"])
    for car, bb in bbonus.items():
        if car in by_car:
            by_car[car]["pos_bonus"] = by_car[car].get("pos_bonus", 0.0) + bb
            by_car[car]["rating"] += bb
    pos_notes += bnotes

    riders.sort(key=lambda r: -r["rating"])
    sc = [r["rating"] for r in riders]      # 以降の計算は評価点で行う
    line_of = {c: i for i, ln in enumerate(lines) for c in ln}

    top, second = riders[0]["車"], riders[1]["車"]
    top_line = lines[line_of[top]] if top in line_of else [top]
    same_line = (top in line_of and second in line_of
                 and line_of[top] == line_of[second])

    gap12 = sc[0] - sc[1]          # 1位と2位の差
    gap14 = sc[0] - sc[3]          # 1位と4位の差
    spread = sc[0] - sc[4]         # 上位5人のばらつき
    n_lines = len(lines)
    n_single = sum(1 for ln in lines if len(ln) == 1)
    n_front = sum(1 for r in riders if r.get("脚") == "逃")

    # ---- 本命度・荒れ度(重みは仮。的中結果を見て調整する) ----
    if not lines:
        honmei = gap12 * 2.0 + gap14 * 0.5 - max(0, n_front - 2) * 1.0
        ara = (max(0, 10 - spread) + max(0, n_front - 2) * 1.5 + max(0, 3 - gap12))
    else:
        honmei = (gap12 * 2.0 + gap14 * 0.5
                  + (3 if same_line else 0)
                  + (2 if len(top_line) >= 2 else 0)
                  - max(0, n_front - 1) * 1.5)
        ara = (max(0, 10 - spread)
               + max(0, n_lines - 3) * 2.0
               + n_single * 1.5
               + max(0, n_front - 1) * 1.5
               + max(0, 3 - gap12))

    # ---- 理由タグ ----
    honmei_tags, ara_tags = [], []
    if girls:
        tag = "👩ガールズ(並びあり)" if lines else "👩ガールズ(ライン無し・個人の力量)"
        honmei_tags.append(tag)
        ara_tags.append(tag)
    if gap12 >= 3:
        honmei_tags.append(f"評価差{gap12:.1f}")
    if lines:
        if same_line:
            honmei_tags.append("評価1・2位が同ライン")
        elif len(top_line) >= 2:
            honmei_tags.append(f"{len(top_line)}車ライン")
    if n_front <= 1:
        honmei_tags.append("先行少なめ")
    if n_front >= 3:
        ara_tags.append(f"先行{n_front}人")
    if n_lines >= 4:
        ara_tags.append(f"{n_lines}分戦")
    if n_single >= 1:
        ara_tags.append(f"単騎{n_single}")
    if spread < 4:
        ara_tags.append("評価団子")

    # ---- 買い目(本命向き): 評価1位を軸、同ラインと評価上位を相手 ----
    if not lines:
        partners = [r["車"] for r in riders[1:4]]
    else:
        partners = [c for c in top_line if c != top]
        for r in riders[1:]:
            if r["車"] not in partners and r["車"] != top:
                partners.append(r["車"])
        partners = partners[:3]
    honmei_bet = f"{top}→{','.join(map(str, partners))} (2車単 軸流し)"

    # ---- 買い目(荒れ向き): 先行選手の番手(差し・マーク)を狙う ----
    front_axis = None
    cands = []                      # (番手の評価点, 番手, 先頭)
    for ln in lines:
        h = by_car.get(ln[0]) if len(ln) >= 2 else None
        bnt = by_car.get(ln[1]) if len(ln) >= 2 else None
        if h and bnt and h.get("脚") == "逃" and ln[1] != top:
            cands.append((bnt["rating"], ln[1], ln[0]))
    cross = cross_line_candidate(lines, by_car) if lines else None
    if cands:
        _, ara_axis, front_axis = max(cands)
        ara_partners = [c for c in (front_axis, top) if c != ara_axis]
        ara_kind = "番手狙い"
    elif cross:
        ara_axis, front_axis = cross["axis"], None
        ara_partners = [cross["partner"]]
        ara_kind = "交差決着"
    else:
        fronts = [r for r in riders if r.get("脚") == "逃" and r["車"] != top]
        if lines and fronts and fronts[0]["車"] in line_of:
            ara_axis = fronts[0]["車"]      # 番手のいない(単騎の)先行選手そのものを狙う
            front_axis = ara_axis
            ara_partners = [c for c in (top,) if c != ara_axis] or [second]
            ara_kind = "先行狙い"
        else:
            ara_axis = riders[1]["車"]
            ara_partners = [top, riders[2]["車"]]
            ara_kind = "2位軸"
    ara_partners = [c for c in dict.fromkeys(ara_partners) if c != ara_axis]   # 重複・軸自身の混入を除く
    ara_bet = f"{ara_axis}→{','.join(map(str, ara_partners))} (2車単 {ara_kind})"

    # ---- 展開予想 ----
    notes_txt = pick_notes(pos_notes)
    base_flow = flow_lines(lines, by_car, girls, riders, n_front, notes_txt)
    honmei_flow = list(base_flow)
    if cross and ara_kind != "交差決着":
        h1, h2 = cross["heads"]
        honmei_flow.append(f"{circ(h1)}・{circ(h2)}の先頭が拮抗、番手同士の交差決着にも注意")
    ara_line = None
    if ara_kind == "番手狙い" and front_axis:
        ara_line = f"{circ(ara_axis)}{name_of(by_car, ara_axis)}が{circ(front_axis)}の番手から差し・マークで抜け出す形"
    elif ara_kind == "交差決着":
        h1, h2 = cross["heads"]
        ara_line = (f"{circ(h1)}と{circ(h2)}の先頭が拮抗、番手{circ(ara_axis)}・{circ(cross['partner'])}の"
                   f"交差決着(ライン決着にならない可能性)")
    elif ara_kind == "先行狙い" and front_axis:
        ara_line = f"{circ(front_axis)}{name_of(by_car, front_axis)}が単騎で先行して粘る形"
    # 荒れ向きは、狙いの説明を先頭の次に置く(文の数を絞っても消えないように)
    ara_flow = base_flow[:1] + ([ara_line] if ara_line else []) + base_flow[1:]
    if n_single >= 1 and lines:
        ara_flow.append("単騎が展開をかき回す可能性")

    # ---- 3連単(軸→2着候補→3着候補のフォーメーション) ----
    def make_tri(axis, prs):
        others = [r["車"] for r in riders if r["車"] != axis and r["車"] not in prs]
        c2 = list(prs[:2])
        c3 = (list(prs) + others)[:4]
        pts = sum(1 for b_ in c2 for c_ in c3 if c_ != b_)
        return {"first": axis, "second": c2, "third": c3, "points": pts}

    honmei_tri = make_tri(top, partners)
    ara_tri = make_tri(ara_axis, ara_partners)

    # ---- 総合評価 ----
    honmei_verdict = ("堅い本命レース" if honmei >= 16
                      else "本命サイドが有利" if honmei >= 8 else "やや本命寄り")
    ara_verdict = ("波乱含み・高配当狙い" if ara >= 16
                   else "荒れる可能性あり" if ara >= 10 else "やや荒れ模様")
    # 狙い目度: 軸の信頼度(本命度)を土台に、波乱度(配当妙味)を少し足したもの。
    # 「荒れ」狙いは狙いにくいため、第2枠はこの指標で選ぶ (2026-11-10 ユーザー要望)
    nerai = honmei * 0.6 + min(ara, 14) * 0.4
    nerai_verdict = ("軸が信頼できて配当も狙える狙い目" if nerai >= 13
                     else "堅さと配当のバランスが良い狙い目" if nerai >= 9.5
                     else "一発に賭ける狙い目")

    return {
        "venue": VENUE_JP.get(race["venue"], race["venue"]),
        "venue_key": race["venue"],
        "cup": race.get("cup"), "day": race.get("day"),
        "race": race["race"],
        "deadline": deadline_min(race.get("title")),
        "girls": girls, "girls_cmp": [] if lines else girls_compare(riders),
        "honmei": honmei, "ara": ara, "nerai": nerai, "nerai_verdict": nerai_verdict,
        "by_car": by_car, "lines": lines,
        "honmei_axis": top, "honmei_partners": partners,
        "honmei_bet": honmei_bet, "honmei_tags": honmei_tags,
        "honmei_flow": honmei_flow, "honmei_tri": honmei_tri,
        "honmei_verdict": honmei_verdict,
        "ara_axis": ara_axis, "ara_partners": ara_partners,
        "ara_bet": ara_bet, "ara_tags": ara_tags,
        "ara_flow": ara_flow, "ara_tri": ara_tri,
        "ara_verdict": ara_verdict,
    }


def pick(results, only_upcoming=True, n=5):
    now = now_min_jst()
    rows = []
    for rc in results:
        a = analyze(rc)
        if not a:
            continue
        if only_upcoming and not is_upcoming(a["deadline"], now):
            continue  # 締切済みは除外
        rows.append(a)
    honmei = sorted(rows, key=lambda x: -x["honmei"])[:n]
    used = {(x["venue"], x["race"]) for x in honmei}
    rest = [x for x in rows if (x["venue"], x["race"]) not in used]
    # 第2枠は「狙い目」: 狙い目度で選び、そのレースの本命サイドの軸・買い目で提示する
    ara = [nerai_view(x) for x in sorted(rest, key=lambda x: -x["nerai"])[:n]]
    return honmei, ara


def nerai_view(x):
    """狙い目レース用の見せ方に組み替えた分析結果を返す。
    軸・相手・買い目・展開はそのレースの本命サイドを使い、スコアは狙い目度にする。
    配信の記録・結果判定もこの内容で統一される (save_picks は ara_* を読むため)。
    元の荒れ度は ara_raw に残し、学習しきい値の判定にはそちらを使う"""
    y = dict(x)
    y["ara_raw"] = x["ara"]
    y["ara"] = x["nerai"]
    for key in ("axis", "partners", "bet", "tags", "flow", "tri"):
        y[f"ara_{key}"] = x[f"honmei_{key}"]
    y["ara_verdict"] = x["nerai_verdict"]
    return y


def fmt_time(m):
    return f"{m // 60}:{m % 60:02d}" if m is not None else "--:--"


CIRC = "①②③④⑤⑥⑦⑧⑨"


def circ(n):
    return CIRC[n - 1] if isinstance(n, int) and 1 <= n <= 9 else str(n)


def stars(score):
    """得点を★1〜5にする目安(4点刻み)"""
    n = max(1, min(5, int(score // 4) + 1))
    return "★" * n + "☆" * (5 - n)


def cs(cars):
    """車番を丸数字でつなぐ(例: ④①⑤)"""
    return "".join(circ(c) for c in cars)


def tri_text(t):
    return f"{circ(t['first'])}→{cs(t['second'])}→{cs(t['third'])} ({t['points']}点)"


def _cw(ch):
    """表示幅(全角・記号・絵文字=2、英数字=1)"""
    return 1 if ord(ch) < 0x2000 else 2


def wrap_text(text, width=36, indent="　"):
    """携帯の画面幅(全角18字ほど)で折り返す。2行目以降は字下げする"""
    lines, cur, w = [], "", 0
    for i, ch in enumerate(text):
        cw = _cw(ch)
        rest = sum(_cw(c) for c in text[i:])
        if w + cw > width and cur.strip() and rest > 6:      # 残りが3文字ほどなら、はみ出しても折り返さない
            lines.append(cur.rstrip())
            cur, w = indent, sum(_cw(c) for c in indent)
        cur += ch
        w += cw
    lines.append(cur.rstrip())
    return lines


def axis_lines(r, car, level):
    """軸選手の表示。名前・脚質・評価点・直近成績(選手コメントは出さない)"""
    if not r:
        return [f"◎{circ(car)}"]
    kyaku = r.get("脚", "")
    out = [f"◎{circ(car)} {r['name']}" + (f"({kyaku})" if kyaku else "")]
    ev = f"　評価{r['rating']:.1f}"
    pb = r.get("pos_bonus") or 0
    if abs(pb) >= 0.3:
        ev += f"(並び{pb:+.1f}込み)"
    out.append(ev)
    f = r.get("form")
    if level >= 2 and f and f["n"] >= 3:
        mood = "(上り調子)" if f["trend"] >= 1.0 else ("(下り調子)" if f["trend"] <= -1.0 else "")
        out.append(f"　📈直近{f['n']}走 平均{f['avg']:.1f}着")
        out.append(f"　　3着内{f['top3'] * 100:.0f}%{mood}")
        if f["acc"]:
            out.append(f"　⚠直近に事故{f['acc']}回")
    return out


SEP = "━━━━━━━━━━━"


def race_block(x, kind, idx, level=3):
    """1レースぶん。idxがNoneのときは会場名を省く(会場別の配信用)"""
    if kind == "honmei":
        axis, partners, flow, tri = x["honmei_axis"], x["honmei_partners"], x["honmei_flow"], x["honmei_tri"]
        tags, verdict, score, icon = x["honmei_tags"], x["honmei_verdict"], x["honmei"], "🎯"
    else:
        axis, partners, flow, tri = x["ara_axis"], x["ara_partners"], x["ara_flow"], x["ara_tri"]
        tags, verdict, score, icon = x["ara_tags"], x["ara_verdict"], x["ara"], "🔥"
    place = f"{x['venue']}" if idx is not None else ""
    n_flow = {3: 4, 2: 3, 1: 2}.get(level, 1)
    reason = "・".join(t for t in tags[:3] if "ガールズ" not in t)

    # 先頭の空行2つで、前のレースとの間をはっきり空ける
    out = ["", "", SEP, f"{icon} {place}{x['race']}R　⏰{fmt_time(x['deadline'])}締切", SEP]
    out += ["", "📈展開予想"]
    for t in flow[:n_flow]:
        out += wrap_text("・" + t)
    if x.get("girls_cmp") and level >= 1:
        out += ["", "📊力量比較(ライン無し)"]
        for title, detail in x["girls_cmp"]:
            out.append(title)
            out += wrap_text("　" + detail, indent="　")
    out += ["", "👤軸選手"] + axis_lines(x["by_car"].get(axis), axis, level)
    out += ["", "🎫3連単予想", "　" + tri_text(tri)]
    out += ["", "🎫2車単予想", f"　{circ(axis)}→{cs(partners)}"]
    out += ["", f"⭐総合評価 {stars(score)}", f"　{verdict}"]
    if not x["lines"]:
        out += wrap_text("　個人の力量(得点・決まり手・直近成績)の総合", indent="　")
    if reason:
        out += wrap_text(f"　({reason})", indent="　")
    if x["lines"] and level >= 1:
        out += ["", "🧭並び " + " ｜ ".join("-".join(map(str, ln)) for ln in x["lines"])]
    return "\n".join(out)


def footer():
    lines = ["🎯=本命サイド　🔥=狙い目", "評価=得点+成績などの補正"]
    if MODEL_RACES:
        lines.append(f"評価は過去{MODEL_RACES}レースの結果で調整済み")
    lines.append("※参考情報です。的中や回収を保証するものではありません。")
    return "\n".join(lines)


def page_url():
    """GitHub Pagesの閲覧URL。Actions実行中は GITHUB_REPOSITORY (owner/repo) から自動で組み立てる"""
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo or "/" not in repo:
        return None
    owner, name = repo.split("/", 1)
    return f"https://{owner}.github.io/{name}/"


def pick_list(picks, show_venue):
    """『おすすめレース』の一覧(締切順)"""
    out = ["🏁おすすめレース"]
    for kind, x in picks:
        icon = "🎯" if kind == "honmei" else "🔥"
        score = x["honmei"] if kind == "honmei" else x["ara"]
        place = x["venue"] if show_venue else ""
        out.append(f"{icon}{place}{x['race']}R　⏰{fmt_time(x['deadline'])}　{stars(score)}")
    return out


def sort_picks(honmei, ara):
    picks = [("honmei", x) for x in honmei] + [("ara", x) for x in ara]
    return sorted(picks, key=lambda t: (t[1]["deadline"] if t[1]["deadline"] is not None else 9999, t[1]["race"]))


def forecast_pickup_lines(honmei, ara, limit=3):
    """予想時点のピックアップ選手。おすすめレースの軸選手から、
    総合評価(星) → 軸の評価点 → 直近の3着内率 の順で上位を選ぶ。
    結果後の好走ピックアップ(keirin_track.pickup_riders)とは別物で、
    レース前に「今日はこの選手に注目」を出すための欄。"""
    cand = []
    for kind, x in sort_picks(honmei, ara):
        axis = x["honmei_axis"] if kind == "honmei" else x["ara_axis"]
        r = x["by_car"].get(axis)
        if not r or not r.get("name"):
            continue
        score = x["honmei"] if kind == "honmei" else x["ara"]
        f = r.get("form") or {}
        top3 = f["top3"] if f.get("n", 0) >= 3 and f.get("top3") is not None else -1
        key = (stars(score).count("★"), r.get("rating") or 0, top3)
        cand.append((key, kind, x, axis, r, score))
    cand.sort(key=lambda t: t[0], reverse=True)   # 星→評価点→3着内率の順で降順
    seen, chosen = set(), []
    for item in cand:
        name = item[4]["name"]
        if name in seen:
            continue
        seen.add(name)
        chosen.append(item)
        if len(chosen) >= limit:
            break
    if not chosen:
        return []
    out = ["", "⭐本日のピックアップ選手"]
    for _, kind, x, axis, r, score in chosen:
        icon = "🎯本命" if kind == "honmei" else "🔥狙い目"
        verdict = x["honmei_verdict"] if kind == "honmei" else x["ara_verdict"]
        kyaku = r.get("脚", "")
        out.append(f"・{x['venue']}{x['race']}R　⏰{fmt_time(x['deadline'])}　{icon}")
        out.append(f"　◎{circ(axis)} {r['name']}" + (f"({kyaku})" if kyaku else "")
                   + f"　評価{r['rating']:.1f}")
        out.append(f"　総合{stars(score)} {verdict}")
        f = r.get("form")
        if f and f.get("n", 0) >= 3:
            mood = "(上り調子)" if f["trend"] >= 1.0 else ("(下り調子)" if f["trend"] <= -1.0 else "")
            out.append(f"　📈直近{f['n']}走 平均{f['avg']:.1f}着 3着内{f['top3'] * 100:.0f}%{mood}")
    return out


def _short_name(name):
    return (name or "").split()[0]


def smart_flow_lines(x, kind):
    """展開予想(スマート版): 役割チェーン + データ裏付けの分析文。
    先行争いは今期の逃げ実績、軸は決まり手内訳と直近成績、さらにバンク特性と
    ラインの崩壊リスクまで見て書く(並びからの定型文にしない)"""
    by_car = x["by_car"]
    axis = x[f"{kind}_axis"]
    lines = [ln for ln in (x.get("lines") or []) if any(c in by_car for c in ln)]
    if not lines:
        r = by_car.get(axis) or {}
        tac, wins = _tactic(r)
        s = f"◎{circ(axis)}{_short_name(r.get('name', ''))}が中心"
        if wins:
            s += f"。主戦法は{tac}(今期{wins}勝)"
        fc = _form_clause(r)
        if fc:
            s += f"。{fc}"
        return ["🔮展開：ライン無し。個人の力量と位置取り勝負", "　" + s]
    multi = [ln for ln in lines if len(ln) >= 2]
    ranked = sorted(multi or lines, key=lambda ln: -line_strength(ln, by_car))
    main = ranked[0]
    roles = ["先行", "番手", "3番手", "4番手", "5番手"]
    chain = []
    for i, car in enumerate(main):
        r = by_car.get(car) or {}
        role = roles[i] if i < len(roles) else f"{i + 1}番手"
        if i == 0 and r.get("脚") != "逃":
            role = "先頭"
        chain.append(f"{circ(car)}{_short_name(r.get('name', ''))}({role})")
    out = ["🔮展開：" + " → ".join(chain)]
    body = [_front_sentence(by_car, lines, main),
            _axis_sentence(x, kind, main, lines)]
    bank = _bank_clause(x)
    if bank:
        body.append(bank)
    risk = _risk_sentence(x, kind, main, ranked)
    if risk:
        body.append(risk)
    # Embedの文字数に収めるため、長いときは後ろ(リスク→バンク)から落とす
    while len(body) > 2 and sum(len(t) for t in body) > 330:
        body.pop()
    out += ["　" + t for t in body if t]
    return out


def smart_race_block(x, kind, level=3):
    """1レースのスマート版ブロック: 見出し→並び→展開→選手(評価順)→買い目→一言"""
    score = x["honmei"] if kind == "honmei" else x["ara"]
    label = "🎯本命" if kind == "honmei" else "🔥狙い目"
    out = [f"🏁 {VENUE_JP.get(x['venue'], x['venue'])}{x['race']}R "
           f"⏰{fmt_time(x.get('deadline'))} {label} {stars(score)}"]
    if x.get("lines"):
        out.append("並び：" + " ｜ ".join(
            "-".join(circ(c) for c in ln) for ln in x["lines"]))
    out += smart_flow_lines(x, kind)
    axis = x[f"{kind}_axis"]
    partners = x[f"{kind}_partners"]
    riders = sorted(x["by_car"].items(), key=lambda kv: -(kv[1].get("rating") or 0))[:5]
    for car, r in riders:
        m = "◎" if car == axis else ("○" if partners and car == partners[0] else
                                      ("▲" if car in partners else "　"))
        s = f"{m}{circ(car)} {r['name']}（{r['脚']}） 評価{r['rating']:.0f}"
        form = r.get("form") or {}
        if level >= 2 and form.get("n"):
            s += f"｜3着内{form['top3'] * 100:.0f}%"
        out.append(s)
    if partners:
        out.append(f"**2車単：{circ(axis)}→{''.join(circ(c) for c in partners)}**")
    tri = x.get(f"{kind}_tri")
    if tri:
        out.append(f"**3連単：{circ(tri['first'])}→"
                   f"{''.join(circ(c) for c in tri['second'])}→"
                   f"{''.join(circ(c) for c in tri['third'])}（{tri['points']}点）**")
    verdict = x.get(f"{kind}_verdict")
    if verdict:
        out.append(f"💬 {verdict}")
    return "\n".join(out)


def build_message(honmei, ara, level=3, enforce_limit=True):
    """スマート版フル文面。Discord配信とページ(GitHub Pages)に使う"""
    jst = datetime.now(timezone(timedelta(hours=9)))
    picks = sort_picks(honmei, ara)
    out = [f"🏇 **本日のおすすめ**（{jst.month}/{jst.day} {jst.hour}:{jst.minute:02d}時点）", ""]
    out += pick_list(picks, True)
    out += forecast_pickup_lines(honmei, ara)
    out.append("")
    for kind, x in picks:
        out.append(smart_race_block(x, kind, level))
        out.append("")
    out.append("※参考情報です。的中や回収を保証するものではありません。")
    msg = "\n".join(out)
    if enforce_limit and line_len(msg) > LINE_LIMIT and level > 0:
        return build_message(honmei, ara, level - 1, enforce_limit)
    return msg


def build_short_message(honmei, ara, url=None):
    """LINE用の短い版。おすすめレースの一覧だけを出し、詳細はページのURLに誘導する"""
    jst = datetime.now(timezone(timedelta(hours=9)))
    picks = sort_picks(honmei, ara)
    out = [f"📅 {jst.month}/{jst.day} {jst.hour}:{jst.minute:02d} 時点", ""]
    out += pick_list(picks, True)
    out += forecast_pickup_lines(honmei, ara)
    out.append("")
    if url:
        out.append(f"🔗展開予想・買い目はこちら\n{url}")
    else:
        out.append("(ページのURLがまだ設定されていません)")
    return "\n".join(out)


# ---------------- Discord Embed(カード形式) ----------------
# テキストの羅列ではなく、色帯つきのカードで届ける(2026-11-05 ユーザー決定)。
# Discordの上限: 1回の送信でEmbed最大10枚 / 合計6000文字 / フィールド値1024文字。
EMBED_COLORS = {"header": 0x3498DB, "pickup": 0xF1C40F,
                "honmei": 0x2ECC71, "ara": 0xE67E22}
EMBED_TOTAL_LIMIT = 5500   # 合計上限6000に余裕を持たせた値


def _fit_field(v, limit=1024):
    return v if len(v) <= limit else v[:limit - 1] + "…"


def smart_race_embed(x, kind):
    """1レース分のEmbed(カード)。中身はスマート版テキストと同じ部品から作る"""
    score = x["honmei"] if kind == "honmei" else x["ara"]
    label = "🎯本命" if kind == "honmei" else "🔥狙い目"
    fields = []
    if x.get("lines"):
        fields.append({"name": "並び", "inline": False, "value": _fit_field(" ｜ ".join(
            "-".join(circ(c) for c in ln) for ln in x["lines"]))})
    flow = smart_flow_lines(x, kind)
    if flow:
        flow = [flow[0].replace("🔮展開：", "", 1)] + flow[1:]
        fields.append({"name": "🔮展開", "inline": False,
                       "value": _fit_field("\n".join(flow))})
    axis = x[f"{kind}_axis"]
    partners = x[f"{kind}_partners"]
    riders = sorted(x["by_car"].items(), key=lambda kv: -(kv[1].get("rating") or 0))[:5]
    rlines = []
    for car, r in riders:
        m = "◎" if car == axis else ("○" if partners and car == partners[0] else
                                      ("▲" if car in partners else "・"))
        s = f"{m}{circ(car)} {r['name']}（{r['脚']}） 評価{r['rating']:.0f}"
        form = r.get("form") or {}
        if form.get("n"):
            s += f"｜3着内{form['top3'] * 100:.0f}%"
        rlines.append(s)
    if rlines:
        fields.append({"name": "選手", "inline": False,
                       "value": _fit_field("\n".join(rlines))})
    bets = []
    if partners:
        bets.append(f"**2車単：{circ(axis)}→{''.join(circ(c) for c in partners)}**")
    tri = x.get(f"{kind}_tri")
    if tri:
        bets.append(f"**3連単：{circ(tri['first'])}→"
                    f"{''.join(circ(c) for c in tri['second'])}→"
                    f"{''.join(circ(c) for c in tri['third'])}（{tri['points']}点）**")
    if bets:
        fields.append({"name": "🎫買い目", "inline": False,
                       "value": _fit_field("\n".join(bets))})
    verdict = x.get(f"{kind}_verdict")
    if verdict:
        fields.append({"name": "💬一言", "inline": False, "value": _fit_field(verdict)})
    return {
        "title": f"🏁 {VENUE_JP.get(x['venue'], x['venue'])}{x['race']}R "
                 f"⏰{fmt_time(x.get('deadline'))} {label} {stars(score)}",
        "color": EMBED_COLORS[kind],
        "fields": fields,
    }


def build_forecast_embeds(honmei, ara):
    """予想配信のEmbed一式: ヘッダー＋ピックアップ＋レースごとのカード"""
    jst = datetime.now(timezone(timedelta(hours=9)))
    picks = sort_picks(honmei, ara)
    embeds = [{
        "title": f"🏇 本日のおすすめ（{jst.month}/{jst.day} {jst.hour}:{jst.minute:02d}時点）",
        "color": EMBED_COLORS["header"],
        "description": "\n".join(pick_list(picks, True)[1:]),
    }]
    pickup = forecast_pickup_lines(honmei, ara)
    if len(pickup) > 2:
        embeds.append({
            "title": "⭐ 本日のピックアップ選手",
            "color": EMBED_COLORS["pickup"],
            "description": "\n".join(pickup[2:]),
        })
    for kind, x in picks:
        embeds.append(smart_race_embed(x, kind))
    return embeds


def _embed_chars(e):
    n = len(e.get("title", "")) + len(e.get("description", ""))
    for f in e.get("fields", []):
        n += len(f.get("name", "")) + len(f.get("value", ""))
    return n


def chunk_embeds(embeds, max_count=10, max_chars=EMBED_TOTAL_LIMIT):
    """Embedを、1回の送信に収まる(最大10枚・合計文字数以内)グループに分ける"""
    groups, cur, total = [], [], 0
    for e in embeds:
        c = _embed_chars(e)
        if cur and (len(cur) >= max_count or total + c > max_chars):
            groups.append(cur)
            cur, total = [], 0
        cur.append(e)
        total += c
    if cur:
        groups.append(cur)
    return groups


def line_position_labels(lines):
    """並び予想から、車番ごとの位置(1=先頭 2=番手 3=3番手以降 0=単騎/ライン無し)を返す"""
    labels = {}
    for ln in lines or []:
        for i, car in enumerate(ln):
            labels[car] = 0 if len(ln) == 1 else min(i + 1, 3)
    return labels


def race_learn_row(race):
    """学習用に、レースの全選手の特徴量と、並び予想での位置(先頭/番手/単騎など)を1行にまとめる"""
    riders = [r for r in race["riders"] if r.get("score") is not None and r.get("車") is not None]
    if len(riders) < 5:
        return None
    girls = is_girls_race(race, riders)
    by_car = {r["車"]: r for r in riders}
    feats = {r["車"]: rider_feats(r) for r in riders}
    lines = [] if girls else (race.get("lines") or [])
    if lines:
        pf, _ = position_feats(lines, by_car, VENUE_PREF.get(race["venue"]))
        for car, f in pf.items():
            feats.setdefault(car, {}).update(f)
    pos = line_position_labels(lines)
    d = (race.get("date") or "").replace("-", "")
    if len(d) != 8:
        d = datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%d")
    return {"k": f"{race['venue']}-{race.get('cup')}-{race.get('day')}-{race['race']}",
            "d": d,
            "g": int(girls), "v": race["venue"],
            "pos": pos,
            "r": [[c, r["score"], [round(feats[c].get(k, 0.0), 3) for k in LEARN_KEYS]]
                  for c, r in by_car.items()]}


# ================= LINE配信 =================
# ================= 通知の送り先(LINE / Discord) =================
DISCORD_LIMIT = 1800   # Discordは1通2000文字まで。余裕を持たせる(絵文字は2文字分で数える)


def _send_line_only(texts):
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
    user_id = os.environ.get("LINE_USER_ID", "")
    # LINEの1通は約5000文字まで、1回に5通まで
    payload = {"messages": [{"type": "text", "text": fit_text(t)} for t in texts[:5]]}
    if user_id:
        url = "https://api.line.me/v2/bot/message/push"
        payload["to"] = user_id
    else:
        url = "https://api.line.me/v2/bot/message/broadcast"  # 友だち全員に送る
    r = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload, timeout=20,
    )
    print("LINE:", r.status_code, r.text[:200])
    r.raise_for_status()


def split_for_discord(text, limit=DISCORD_LIMIT):
    """長い文面を、レース単位(区切り線の手前)でまとめながら、1通の上限以内に分ける"""
    blocks = re.split(r"\n(?=━{5,})", text)
    chunks, cur = [], ""

    def flush():
        nonlocal cur
        if cur.strip():
            chunks.append(cur.strip("\n"))
        cur = ""

    for block in blocks:
        if line_len(block) > limit:                     # 1レースだけで上限を超える場合は、行ごとに詰める
            flush()
            for line in block.split("\n"):
                while line_len(line) > limit:           # 1行が長すぎる場合は、文字数で切る
                    piece, line = line[:500], line[500:]
                    if line_len(cur + piece) > limit:
                        flush()
                    cur += piece + "\n"
                if line_len(cur + line) > limit:
                    flush()
                cur += line + "\n"
            continue
        cand = (cur + "\n" + block) if cur else block
        if line_len(cand) > limit:
            flush()
            cur = block
        else:
            cur = cand
    flush()
    return chunks or [""]


def _send_discord(texts):
    url = os.environ["DISCORD_WEBHOOK_URL"]
    chunks = []
    for t in texts:
        chunks += split_for_discord(t)
    for c in chunks:
        if not c.strip():
            continue
        for _ in range(3):                               # 送りすぎの制限(429)のときは、少し待ってやり直す
            r = requests.post(
                url,
                json={"content": c, "username": "マッシュチャリンコ", "allowed_mentions": {"parse": []}},
                timeout=20,
            )
            if r.status_code == 429:
                try:
                    wait = float(r.json().get("retry_after", 1))
                except Exception:
                    wait = 1.0
                time.sleep(min(wait, 10))
                continue
            break
        print("Discord:", r.status_code)
        r.raise_for_status()
        time.sleep(0.4)


def _send_discord_embeds(embeds):
    """Embed(カード)をWebhookに送る。429のときは少し待ってやり直す"""
    url = os.environ["DISCORD_WEBHOOK_URL"]
    groups = chunk_embeds(embeds)
    for gi, group in enumerate(groups):
        payload = {"embeds": group, "username": "マッシュチャリンコ",
                   "allowed_mentions": {"parse": []}}
        if gi == len(groups) - 1:
            payload["content"] = "※参考情報です。的中や回収を保証するものではありません。"
        for _ in range(3):                               # 送りすぎの制限(429)のときは、少し待ってやり直す
            r = requests.post(url, json=payload, timeout=20)
            if r.status_code == 429:
                try:
                    wait = float(r.json().get("retry_after", 1))
                except Exception:
                    wait = 1.0
                time.sleep(min(wait, 10))
                continue
            break
        print("Discord(embed):", r.status_code)
        r.raise_for_status()
        time.sleep(0.4)


def notify_channels():
    """実際に送れる送り先の一覧を返す。NOTIFYで指定があればその指定に従い、
    未指定ならトークン/URLが設定済みのものすべて"""
    configured = {
        "line": bool(os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")),
        "discord": bool(os.environ.get("DISCORD_WEBHOOK_URL")),
    }
    wanted = [c.strip().lower() for c in os.environ.get("NOTIFY", "").split(",") if c.strip()]
    channels = []
    for c in (wanted or [k for k in configured if configured[k]]):
        if c not in configured:
            print(f"NOTIFYに未対応の送り先 '{c}' があります(line / discord が使えます)")
        elif not configured[c]:
            print(f"{c} は指定されていますが、トークン/URLが未設定のため、スキップします")
        else:
            channels.append(c)
    return channels


def send_forecast(honmei, ara, full_text, url):
    """予想配信の送信。Discord宛てはEmbed(カード形式)、LINE宛ては従来のテキスト
    (フル版が上限を超える日は短縮版+URL)。ページには常にフル版テキストを使う。
    片方が失敗してももう片方は送り、すべて失敗したときだけエラーにする"""
    channels = notify_channels()
    if not channels:
        print("送り先が設定されていないため、送信せず表示のみ")
        print(full_text)
        return
    errors = []
    for c in channels:
        try:
            if c == "discord":
                _send_discord_embeds(build_forecast_embeds(honmei, ara))
            else:
                text = full_text if line_len(full_text) <= LINE_LIMIT \
                    else build_short_message(honmei, ara, url)
                _send_line_only([text])
        except Exception as e:
            print(f"{c} への送信に失敗しました: {e}")
            errors.append(e)
    if len(errors) == len(channels):                     # すべて失敗したときだけ、呼び出し側にエラーを返す
        raise errors[0]


def send_report(new_rows, text):
    """結果報告の送信。Discord宛てはEmbed(カード形式)、LINE宛ては従来のテキスト。
    片方が失敗してももう片方は送り、すべて失敗したときだけエラーにする"""
    channels = notify_channels()
    if not channels:
        print("送り先が設定されていないため、送信せず表示のみ")
        print(text)
        return
    errors = []
    for c in channels:
        try:
            if c == "discord":
                from keirin_track import build_report_embeds
                _send_discord_embeds(build_report_embeds(new_rows))
            else:
                _send_line_only([text])
        except Exception as e:
            print(f"{c} への送信に失敗しました: {e}")
            errors.append(e)
    if len(errors) == len(channels):                     # すべて失敗したときだけ、呼び出し側にエラーを返す
        raise errors[0]


def send_line(texts):
    """配信の入口(名前は昔のまま)。設定されている送り先すべてに送る。
    送り先は環境変数 NOTIFY で絞れる(例: line / discord / line,discord)。
    未設定なら、LINEのトークン・DiscordのWebhook URLのうち、設定済みのものすべてに送る。
    片方が失敗してももう片方は送り、すべて失敗したときだけエラーにする"""
    if isinstance(texts, str):
        texts = [texts]
    channels = notify_channels()
    if not channels:
        print("送り先が設定されていないため、送信せず表示のみ")
        print("\n-----\n".join(texts))
        return

    errors = []
    for c in channels:
        try:
            {"line": _send_line_only, "discord": _send_discord}[c](texts)
        except Exception as e:
            print(f"{c} への送信に失敗しました: {e}")
            errors.append(e)
    if len(errors) == len(channels):                     # すべて失敗したときだけ、呼び出し側にエラーを返す
        raise errors[0]
