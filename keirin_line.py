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

BASE = "https://www.winticket.jp"
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
    """HTMLを取得。失敗したら少し待って再試行"""
    for i in range(retries + 1):
        try:
            r = _session().get(url, timeout=20)
            if r.status_code == 200:
                return r.text
            print(f"  status {r.status_code}: {url}")
        except Exception as e:
            print(f"  error {e}: {url}")
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


def get_today_races():
    """出走表トップから、今日の全レースURLを集める"""
    html = fetch(f"{BASE}/keirin/racecard")
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    pat = re.compile(r"^/keirin/([a-z]+)/racecard/(\d+)/(\d+)/(\d+)$")
    seen, races = set(), []
    for a in soup.find_all("a", href=True):
        m = pat.match(a["href"])
        if m and a["href"] not in seen:
            seen.add(a["href"])
            venue, cup, day, rn = m.groups()
            races.append({
                "venue": venue,
                "cup": cup,
                "day": int(day),
                "race": int(rn),
                "url": BASE + a["href"],
            })
    races.sort(key=lambda x: (x["venue"], x["race"]))
    return races


def split_name(text):
    """'和田真久留 神奈川 S1 35歳 99期' を分解"""
    m = re.match(r"^(\S+)\s+(\S+)\s+(\S+)\s+(\d+)歳\s+(\d+)期", text)
    if not m:
        return {"name": text, "pref": "", "grade": "", "age": None, "term": None}
    return {
        "name": m.group(1),
        "pref": m.group(2),
        "grade": m.group(3),
        "age": int(m.group(4)),
        "term": int(m.group(5)),
    }


def parse_riders(soup):
    """詳細の表(2つ目のtable)から選手データを取る"""
    tables = soup.find_all("table")
    if len(tables) < 2:
        return []
    rows = tables[1].find_all("tr")
    header = [clean(c.get_text()) for c in rows[0].find_all(["th", "td"])]
    if "選手名" not in header:
        return []
    cols = header[header.index("選手名"):]

    riders = []
    for tr in rows[1:]:
        cells = tr.find_all(["td", "th"])
        # 選手ページへのリンクがあるセル=選手名セル(枠のrowspan対策)
        ni = next((i for i, c in enumerate(cells)
                   if c.find("a", href=lambda h: h and "/cyclist/" in h)), None)
        if ni is None:
            continue
        vals = [c.get_text(" ", strip=True) for c in cells[ni:]]
        d = dict(zip(cols, vals))
        d["車"] = to_int(cells[ni - 1].get_text(strip=True)) if ni > 0 else None
        d.update(split_name(d.get("選手名", "")))
        d["score"] = to_float(d.get("競走得点"))
        a_tag = cells[ni].find("a", href=lambda h: h and "/cyclist/" in h)
        pm = re.search(r"/cyclist/(\d+)", a_tag["href"]) if a_tag else None
        d["pid"] = pm.group(1) if pm else None
        riders.append(d)
    return riders


def parse_lines(soup):
    """並び予想 -> [[7,1],[2,8],[5,3],[6,4]]"""
    el = soup.find(class_=re.compile(r"^LinePrediction"))
    if not el:
        return None
    txt = el.get_text(" ", strip=True)
    lines = []
    for part in txt.split("区切り"):
        nums = [int(n) for n in re.findall(r"\d+", part)]
        if nums:
            lines.append(nums)
    return lines or None


def parse_race(url):
    html = fetch(url)
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    return {
        "url": url,
        "title": h1.get_text(" ", strip=True) if h1 else "",
        "riders": parse_riders(soup),
        "lines": parse_lines(soup),
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
                data.update({"venue": rc["venue"], "race": rc["race"],
                             "day": rc["day"], "cup": rc["cup"]})
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

def extract_player_data(html):
    """選手ページ内のJSON(window.__PRELOADED_STATE__)から、成績データを取り出す"""
    m = re.search(r"window\.__PRELOADED_STATE__\s*=\s*", html)
    if not m:
        return None
    obj, _ = json.JSONDecoder().raw_decode(html[m.end():])
    for q in (obj.get("tanStackQuery") or {}).get("queries", []):
        d = (q.get("state") or {}).get("data")
        if isinstance(d, dict) and "latestCupResults" in d:
            return d
    return None


def compute_form(d, n=10):
    """直近nレースの着順・事故・連対時の決まり手をまとめる
    raceId = レース番号(2桁) + 場コード(2桁) + 日付(8桁) と見て、日付順に並べる"""
    seen = {}
    for cup in d.get("latestCupResults") or []:
        for rr in cup.get("raceResults") or []:
            rid = str(rr.get("raceId") or "")
            if len(rid) < 12 or rid in seen:
                continue
            seen[rid] = {
                "date": rid[-8:],
                "no": to_int(rid[:2]) or 0,
                "order": to_int(rr.get("order")),
                "factor": (rr.get("factor") or "").strip(),
                "acc": bool(rr.get("hasAccident")),
            }
    races = sorted(seen.values(), key=lambda x: (x["date"], x["no"]), reverse=True)
    if not races:
        return None
    recent = races[:n]
    # 着順0(順位なし)は9着扱い、事故としても数える
    orders = [x["order"] if x["order"] and x["order"] >= 1 else 9 for x in recent]
    cnt = len(orders)
    form = {
        "n": cnt,
        "avg": sum(orders) / cnt,
        "win": sum(1 for o in orders if o == 1) / cnt,
        "top2": sum(1 for o in orders if o <= 2) / cnt,
        "top3": sum(1 for o in orders if o <= 3) / cnt,
        "acc": sum(1 for x in recent if x["acc"] or not x["order"]),
        "trend": 0.0,   # プラス=上り調子(直近5走が、その前より良い)
        "kim": {},
    }
    if cnt >= 8:
        head, tail = orders[:5], orders[5:]
        form["trend"] = sum(tail) / len(tail) - sum(head) / len(head)
    kim = {}
    for x in races[:20]:
        if x["order"] in (1, 2) and x["factor"] in ("逃", "捲", "差", "マ"):
            kim[x["factor"]] = kim.get(x["factor"], 0) + 1
    form["kim"] = kim
    return form


REL_LABELS = ("縁故選手", "友人", "練習仲間", "師匠", "弟子", "練習グループ")


def parse_relations(html):
    """選手ページの『縁故・知人・仲間』から、種類ごとの選手ID(登録番号)を取る。
    例: {"練習仲間": ["012436"], "師匠": ["012436"]}。見つからなければ空"""
    m = re.search(r"<(h[1-4])[^>]*>\s*縁故・知人・仲間", html)
    if not m:
        return {}
    end = html.find("<" + m.group(1), m.end())        # 同じ階層の次の見出しまで
    seg = html[m.start(): end if end > 0 else m.start() + 30000]
    rel, cur = {}, None
    for el in BeautifulSoup(seg, "html.parser").descendants:
        if isinstance(el, NavigableString):
            t = str(el).strip()
            if t in REL_LABELS:
                cur = t
        elif getattr(el, "name", None) == "a":
            mid = re.search(r"/cyclist/(\d+)", el.get("href") or "")
            if mid and cur:
                ids = rel.setdefault(cur, [])
                if mid.group(1) not in ids:
                    ids.append(mid.group(1))
    return rel


def fetch_player_form(pid):
    html = fetch(f"{BASE}/keirin/cyclist/{pid}", retries=1)
    if not html:
        return None
    try:
        _REL_CACHE[pid] = parse_relations(html)
    except Exception as e:
        print(f"  relation parse error {pid}: {e}")
        _REL_CACHE[pid] = {}
    try:
        d = extract_player_data(html)
        return compute_form(d) if d else None
    except Exception as e:
        print(f"  player parse error {pid}: {e}")
        return None


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
                 "pos_follow": 2.5, "pos_local": 1.0, "jump": 1.5}
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
            adj += max(-0.7, min(0.7, fm["trend"] * 0.2))          # 上り調子か下り調子か
        adj -= min(1.0, 0.5 * fm["acc"])                           # 落車・失格など
    r["rating"] = r["score"] + adj
    return r["rating"]


def name_of(by_car, car):
    r = by_car.get(car)
    return r["name"] if r else ""


def is_girls_race(race, riders):
    """ガールズケイリンか(ラインが無く、個人の力量と作戦で決まる)"""
    if "ガールズ" in (race.get("title") or ""):
        return True
    return any(str(r.get("grade") or "").startswith("L") for r in riders)


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
            if head_front:
                # 先行選手の番手は、差し・マークで決まりやすい。
                # 先頭の自力の実績と、本人の差し・マークの実績が多いほど大きい
                raw = min(1.5, 0.25 * head_power) + min(1.0, 0.15 * follow_wins(r))
                if pos == 3:
                    raw *= 0.5
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
    """展開メモを最大limit個。地元番手と飛びつきを優先し、残りは加点の大きい順"""
    local = sorted((n for n in notes if n[0] == "local"), key=lambda n: -n[1])[:1]
    jump = sorted((n for n in notes if n[0] == "jump"), key=lambda n: -n[1])[:1]
    follow = sorted((n for n in notes if n[0] == "follow"), key=lambda n: -n[1])
    follow = follow[:max(0, limit - len(local) - len(jump))]
    return [n[2] for n in follow + local + jump]


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
            parts.append(f"直近平均{fm['avg']:.1f}着")
        elif r.get("3連対率"):
            parts.append(f"3連対{r['3連対率']}%")
        style = r.get("脚", "")
        out.append((f"{circ(r['車'])} {r['name']}" + (f"({style})" if style else ""), " / ".join(parts)))
    return out


def line_strength(ln, by_car):
    """ラインの強さ = 選手の評価点の平均 + 先頭の自力の実績"""
    rs = [by_car[c] for c in ln if c in by_car]
    if not rs:
        return -1e9
    head = by_car.get(ln[0])
    return sum(r["rating"] for r in rs) / len(rs) + (0.3 * jiriki_wins(head) if head else 0)


def flow_lines(lines, by_car, girls, riders, n_front, notes):
    """展開予想を、短い文の並びにする"""
    out = []
    if girls:
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

    # ガールズはラインが無い。個人の力量(得点・決まり手・直近成績)だけで評価する
    lines = [] if girls else (race.get("lines") or [])
    pos_notes = []
    if lines:
        bonus, pos_notes = position_bonus(lines, by_car, VENUE_PREF.get(race["venue"]),
                                          WEIGHTS_GIRLS if girls else WEIGHTS)
        for car, bb in bonus.items():
            if car in by_car:
                by_car[car]["pos_bonus"] = bb
                by_car[car]["rating"] += bb
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
    if girls:
        honmei = gap12 * 2.0 + gap14 * 0.5 - max(0, n_front - 2) * 1.0
        ara = (max(0, 10 - spread) + max(0, n_front - 2) * 1.5 + max(0, 3 - gap12))
    else:
        honmei = (gap12 * 2.0 + gap14 * 0.5
                  + (3 if same_line else 0)
                  + (2 if len(top_line) >= 3 else 0)
                  - max(0, n_front - 1) * 1.5)
        ara = (max(0, 10 - spread)
               + max(0, n_lines - 3) * 2.0
               + n_single * 1.5
               + max(0, n_front - 1) * 1.5
               + max(0, 3 - gap12))

    # ---- 理由タグ ----
    honmei_tags, ara_tags = [], []
    if girls:
        honmei_tags.append("👩ガールズ(ライン無し・個人の力量)")
        ara_tags.append("👩ガールズ(ライン無し・個人の力量)")
    if gap12 >= 3:
        honmei_tags.append(f"評価差{gap12:.1f}")
    if not girls:
        if same_line:
            honmei_tags.append("評価1・2位が同ライン")
        elif len(top_line) >= 3:
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
    if girls:
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
    if cands:
        _, ara_axis, front_axis = max(cands)
        ara_partners = [c for c in (front_axis, top) if c != ara_axis]
        ara_kind = "番手狙い"
    else:
        fronts = [r for r in riders if r.get("脚") == "逃" and r["車"] != top]
        if not girls and fronts and fronts[0]["車"] in line_of:
            ara_axis = fronts[0]["車"]      # 番手のいない(単騎の)先行選手そのものを狙う
            front_axis = ara_axis
            ara_partners = [c for c in (top,) if c != ara_axis] or [second]
            ara_kind = "先行狙い"
        else:
            ara_axis = riders[1]["車"]
            ara_partners = [top, riders[2]["車"]]
            ara_kind = "2位軸"
    ara_bet = f"{ara_axis}→{','.join(map(str, ara_partners))} (2車単 {ara_kind})"

    # ---- 展開予想 ----
    notes_txt = pick_notes(pos_notes)
    base_flow = flow_lines(lines, by_car, girls, riders, n_front, notes_txt)
    honmei_flow = list(base_flow)
    ara_line = None
    if ara_kind == "番手狙い" and front_axis:
        ara_line = f"{circ(ara_axis)}{name_of(by_car, ara_axis)}が{circ(front_axis)}の番手から差し・マークで抜け出す形"
    elif ara_kind == "先行狙い" and front_axis:
        ara_line = f"{circ(front_axis)}{name_of(by_car, front_axis)}が単騎で先行して粘る形"
    # 荒れ向きは、狙いの説明を先頭の次に置く(文の数を絞っても消えないように)
    ara_flow = base_flow[:1] + ([ara_line] if ara_line else []) + base_flow[1:]
    if n_single >= 1 and not girls:
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

    return {
        "venue": VENUE_JP.get(race["venue"], race["venue"]),
        "venue_key": race["venue"],
        "cup": race.get("cup"), "day": race.get("day"),
        "race": race["race"],
        "deadline": deadline_min(race.get("title")),
        "girls": girls, "girls_cmp": girls_compare(riders) if girls else [],
        "honmei": honmei, "ara": ara,
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
    ara = sorted(rest, key=lambda x: -x["ara"])[:n]
    return honmei, ara


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
        tags, verdict, score, icon = x["ara_tags"], x["ara_verdict"], x["ara"], "🌪"
    place = f"{x['venue']}" if idx is not None else ""
    n_flow = {3: 4, 2: 3, 1: 2}.get(level, 1)
    reason = "・".join(t for t in tags[:3] if "ガールズ" not in t)

    # 先頭の空行2つで、前のレースとの間をはっきり空ける
    out = ["", "", SEP, f"{icon} {place}{x['race']}R　⏰{fmt_time(x['deadline'])}締切", SEP]
    out += ["", "📈展開予想"]
    for t in flow[:n_flow]:
        out += wrap_text("・" + t)
    if x.get("girls") and x.get("girls_cmp") and level >= 1:
        out += ["", "📊力量比較(ライン無し)"]
        for title, detail in x["girls_cmp"]:
            out.append(title)
            out += wrap_text("　" + detail, indent="　")
    out += ["", "👤軸選手"] + axis_lines(x["by_car"].get(axis), axis, level)
    out += ["", "🎫3連単予想", "　" + tri_text(tri)]
    out += ["", "🎫2車単予想", f"　{circ(axis)}→{cs(partners)}"]
    out += ["", f"⭐総合評価 {stars(score)}", f"　{verdict}"]
    if x.get("girls"):
        out += wrap_text("　個人の力量(得点・決まり手・直近成績)の総合", indent="　")
    if reason:
        out += wrap_text(f"　({reason})", indent="　")
    if x["lines"] and level >= 1:
        out += ["", "🧭並び " + " ｜ ".join("-".join(map(str, ln)) for ln in x["lines"])]
    return "\n".join(out)


def footer():
    lines = ["🎯=本命サイド　🌪=荒れ狙い", "評価=得点+成績などの補正"]
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
        icon = "🎯" if kind == "honmei" else "🌪"
        score = x["honmei"] if kind == "honmei" else x["ara"]
        place = x["venue"] if show_venue else ""
        out.append(f"{icon}{place}{x['race']}R　⏰{fmt_time(x['deadline'])}　{stars(score)}")
    return out


def sort_picks(honmei, ara):
    picks = [("honmei", x) for x in honmei] + [("ara", x) for x in ara]
    return sorted(picks, key=lambda t: (t[1]["deadline"] if t[1]["deadline"] is not None else 9999, t[1]["race"]))


def build_message(honmei, ara, level=3, enforce_limit=True):
    """展開予想・買い目まで含めたフル版。LINEには使わず、ページ(GitHub Pages)用に使う"""
    jst = datetime.now(timezone(timedelta(hours=9)))
    picks = sort_picks(honmei, ara)
    out = [f"📅 {jst.month}/{jst.day} {jst.hour}:{jst.minute:02d} 時点", ""]
    out += pick_list(picks, True)
    for kind, x in picks:
        out.append(race_block(x, kind, 1, level))
    out += ["", "", footer()]
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
    out.append("")
    if url:
        out.append(f"🔗展開予想・買い目はこちら\n{url}")
    else:
        out.append("(ページのURLがまだ設定されていません)")
    return "\n".join(out)


def race_learn_row(race):
    """学習用に、レースの全選手の特徴量(基本データだけ)を1行にまとめる"""
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
    return {"k": f"{race['venue']}-{race.get('cup')}-{race.get('day')}-{race['race']}",
            "g": int(girls),
            "r": [[c, r["score"], [round(feats[c].get(k, 0.0), 3) for k in LEARN_KEYS]]
                  for c, r in by_car.items()]}


# ================= LINE配信 =================
def send_line(texts):
    if isinstance(texts, str):
        texts = [texts]
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
    user_id = os.environ.get("LINE_USER_ID", "")
    if not token:
        print("LINE_CHANNEL_ACCESS_TOKEN が未設定のため、送信せず表示のみ")
        print("\n-----\n".join(texts))
        return
    # LINEの1通は約5000文字まで、1回に5通まで
    payload = {"messages": [{"type": "text", "text": fit_text(t)} for t in texts[:5]]}
    if user_id:
        url = "https://api.line.me/v2/bot/message/push"
        payload["to"] = user_id
    else:
        url = "https://api.line.me/v2/bot/message/broadcast"  # 友だち全員に送る
    r = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        json=payload, timeout=20,
    )
    print("LINE:", r.status_code, r.text[:200])
    r.raise_for_status()
