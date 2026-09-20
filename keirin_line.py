# 競輪 出走表取得 + 本命/荒れ分析 + LINE配信
# 必要: requests, beautifulsoup4 (Colabには入っています)
import json
import os
import re
import time
import unicodedata
from datetime import datetime, timezone, timedelta
import requests
from bs4 import BeautifulSoup
from collections import Counter

BASE = "https://www.winticket.jp"
HEADERS = {"User-Agent": "Mozilla/5.0"}
SLEEP = 1.5  # レース間の待ち時間(秒)
ENRICH = os.environ.get("ENRICH", "1") != "0"        # 選手ページの成績も使う(0で無効)
ENRICH_SLEEP = 1.0                                   # 選手ページ間の待ち時間(秒)
ENRICH_BUDGET = int(os.environ.get("ENRICH_BUDGET", "420"))  # 選手ページ取得の制限時間(秒)
LAYOUT = os.environ.get("LAYOUT", "ranking")         # ranking=ランキング形式 / venue=会場ごとにまとめる
SPLIT = os.environ.get("SPLIT", "0") == "1"          # 1=会場ごとに別メッセージ(LINEの通数が増える)
VENUES = [v.strip() for v in os.environ.get("VENUES", "").split(",") if v.strip()]  # 例: toyama,高知(空=全会場)


def fetch(url, retries=2):
    """HTMLを取得。失敗したら少し待って再試行"""
    for i in range(retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            if r.status_code == 200:
                return r.text
            print(f"  status {r.status_code}: {url}")
        except Exception as e:
            print(f"  error {e}: {url}")
        time.sleep(2)
    return None


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


def fetch_all_today(limit=None):
    """今日の全レースを取得して返す。limitでテスト用に件数を絞れる"""
    races = [r for r in get_today_races() if venue_ok(r["venue"])]
    if limit:
        races = races[:limit]
    results = []
    for i, rc in enumerate(races):
        data = parse_race(rc["url"])
        if data:
            data.update({"venue": rc["venue"], "race": rc["race"], "day": rc["day"], "cup": rc["cup"]})
            results.append(data)
        print(f"{i+1}/{len(races)} {rc['venue']} {rc['race']}R "
              f"{'OK' if data and data['riders'] else 'NG'}")
        time.sleep(SLEEP)
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


def fetch_player_form(pid):
    html = fetch(f"{BASE}/keirin/cyclist/{pid}", retries=1)
    if not html:
        return None
    try:
        d = extract_player_data(html)
        return compute_form(d) if d else None
    except Exception as e:
        print(f"  player parse error {pid}: {e}")
        return None


_FORM_CACHE = {}


def enrich_riders(races):
    """候補レースの全選手について、直近成績を取得して r['form'] に入れる"""
    start = time.time()
    total = sum(1 for rc in races for r in rc["riders"] if r.get("pid"))
    done = 0
    for rc in races:
        for r in rc["riders"]:
            pid = r.get("pid")
            if not pid:
                continue
            if pid not in _FORM_CACHE:
                if time.time() - start > ENRICH_BUDGET:
                    print("選手ページの取得が制限時間に達したため、残りは基本データで続行")
                    return
                _FORM_CACHE[pid] = fetch_player_form(pid)
                time.sleep(ENRICH_SLEEP)
                done += 1
                if done % 10 == 0:
                    print(f"選手ページ {done}/{total}")
            r["form"] = _FORM_CACHE[pid]


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


def venue_ok(key):
    """VENUESが空なら全会場。指定があれば、ローマ字(toyama)でも日本語(富山)でも可"""
    if not VENUES:
        return True
    return key in VENUES or VENUE_JP.get(key, key) in VENUES


def deadline_min(title):
    """タイトルから締切時刻(0時からの分)を取る。取れなければNone"""
    m = re.search(r"締切\s*(\d{1,2}):(\d{2})", title or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def now_min_jst():
    n = datetime.now(timezone(timedelta(hours=9)))
    return n.hour * 60 + n.minute


def rider_rating(r):
    """評価点 = 競走得点 + 補正(3連対率・決まり手・直近成績)。重みは仮"""
    adj = 0.0
    t3 = to_float(r.get("3連対率"))
    if t3 is not None:
        adj += max(-2.0, min(2.0, (t3 - 40) * 0.05))

    def k(key):
        return to_int(r.get(key)) or 0

    jiriki = min(2.0, (k("逃") + k("捲")) * 0.3)    # 自力(逃げ・捲り)で勝てているか
    mark = min(1.5, (k("差") + k("マ")) * 0.15)     # 差し・マークで勝てているか
    kyaku = r.get("脚")
    if kyaku == "逃":
        adj += jiriki
    elif kyaku == "追":
        adj += mark
    else:
        adj += max(jiriki, mark)
    f = r.get("form")
    if f and f["n"] >= 5:
        adj += max(-1.5, min(1.5, (5.0 - f["avg"]) * 0.4))      # 直近の平均着順
        if f["n"] >= 8:
            adj += max(-0.7, min(0.7, f["trend"] * 0.2))          # 上り調子か下り調子か
        adj -= min(1.0, 0.5 * f["acc"])                           # 落車・失格など
    r["rating"] = r["score"] + adj
    return r["rating"]


def name_of(by_car, car):
    r = by_car.get(car)
    return r["name"] if r else ""


def analyze(race):
    """1レースを分析して、本命度・荒れ度・買い目・評価コメントを返す"""
    riders = [r for r in race["riders"]
              if r.get("score") is not None and r.get("車") is not None]
    if len(riders) < 5:
        return None
    for r in riders:
        rider_rating(r)
    riders.sort(key=lambda r: -r["rating"])
    sc = [r["rating"] for r in riders]      # 以降の計算は評価点で行う
    by_car = {r["車"]: r for r in riders}

    lines = race.get("lines") or []
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

    # ---- 本命度(重みは仮。的中結果を見て調整する) ----
    honmei = (gap12 * 2.0 + gap14 * 0.5
              + (3 if same_line else 0)
              + (2 if len(top_line) >= 3 else 0)
              - max(0, n_front - 1) * 1.5)

    # ---- 荒れ度 ----
    ara = (max(0, 10 - spread)
           + max(0, n_lines - 3) * 2.0
           + n_single * 1.5
           + max(0, n_front - 1) * 1.5
           + max(0, 3 - gap12))

    # ---- 理由タグ ----
    honmei_tags = []
    if gap12 >= 3:
        honmei_tags.append(f"評価差{gap12:.1f}")
    if same_line:
        honmei_tags.append("評価1・2位が同ライン")
    elif len(top_line) >= 3:
        honmei_tags.append(f"{len(top_line)}車ライン")
    if n_front <= 1:
        honmei_tags.append("先行少なめ")
    ara_tags = []
    if n_front >= 3:
        ara_tags.append(f"先行{n_front}人")
    if n_lines >= 4:
        ara_tags.append(f"{n_lines}分戦")
    if n_single >= 1:
        ara_tags.append(f"単騎{n_single}")
    if spread < 4:
        ara_tags.append("評価団子")

    # ---- 買い目(本命向き): 得点1位を軸、同ラインと得点上位を相手 ----
    partners = [c for c in top_line if c != top]
    for r in riders[1:]:
        if r["車"] not in partners and r["車"] != top:
            partners.append(r["車"])
    partners = partners[:3]
    honmei_bet = f"{top}→{','.join(map(str, partners))} (2車単 軸流し)"

    # ---- 買い目(荒れ向き): 逃げ選手の番手を狙う ----
    front_axis = None
    fronts = [r for r in riders if r.get("脚") == "逃" and r["車"] != top]
    if fronts and fronts[0]["車"] in line_of:
        front = fronts[0]["車"]
        ln = lines[line_of[front]]
        i = ln.index(front)
        front_axis = front
        if i + 1 < len(ln):
            ara_axis = ln[i + 1]
            ara_kind = "番手狙い"
        else:
            ara_axis = front          # 番手がいない(単騎)なら先行選手そのものを狙う
            ara_kind = "先行狙い"
        ara_partners = [c for c in (front, top) if c != ara_axis]
    else:
        ara_axis = riders[1]["車"]
        ara_partners = [top, riders[2]["車"]]
        ara_kind = "2位軸"
    ara_bet = f"{ara_axis}→{','.join(map(str, ara_partners))} (2車単 {ara_kind})"

    # ---- 展開予想・注意点(本命向き) ----
    honmei_outlook = []
    if len(top_line) >= 2:
        hk = (by_car.get(top_line[0]) or {}).get("脚", "")
        seg = "-".join(map(str, top_line))
        if hk == "逃":
            honmei_outlook.append(
                f"主力{seg}: 先頭{circ(top_line[0])}が逃げ、番手{circ(top_line[1])}が有利")
        else:
            honmei_outlook.append(
                f"主力{seg}: 先頭{circ(top_line[0])}は{hk or '不明'}型、位置取り次第")
    else:
        honmei_outlook.append(f"評価1位{circ(top)}は単騎、展開頼み")
    honmei_outlook.append(
        f"評価 1位{circ(top)}{name_of(by_car, top)} {sc[0]:.1f}"
        f" / 2位{circ(second)}{name_of(by_car, second)} 差{gap12:.1f}")
    honmei_caution = []
    if n_front >= 3:
        honmei_caution.append(f"先行{n_front}人でペース乱れに注意")
    if not same_line:
        honmei_caution.append("評価2位が別ライン、頭を取られる恐れ")
    if len(top_line) == 1:
        honmei_caution.append("軸が単騎で展開に左右される")

    # ---- 展開予想・注意点(荒れ向き) ----
    ara_outlook = []
    fr = [r for r in riders if r.get("脚") == "逃"][:3]
    if fr:
        ara_outlook.append("先行候補 " + "・".join(
            f"{circ(r['車'])}{r['name']}" for r in fr))
    ara_outlook.append(f"上位5人の評価差{spread:.1f} / {n_lines}ライン(単騎{n_single})")
    if ara_kind == "番手狙い" and front_axis:
        ara_outlook.append(
            f"{circ(ara_axis)}{name_of(by_car, ara_axis)}が{circ(front_axis)}の番手から抜け出す形")
    elif ara_kind == "先行狙い" and front_axis:
        ara_outlook.append(
            f"{circ(front_axis)}{name_of(by_car, front_axis)}が単騎で先行して粘る形")
    ara_caution = []
    if n_front >= 3:
        ara_caution.append("先行が多く、ペース次第で結果が割れる")
    if n_single >= 1:
        ara_caution.append("単騎が展開をかき回す")

    return {
        "venue": VENUE_JP.get(race["venue"], race["venue"]),
        "venue_key": race["venue"],
        "cup": race.get("cup"), "day": race.get("day"),
        "race": race["race"],
        "deadline": deadline_min(race.get("title")),
        "honmei": honmei, "ara": ara,
        "by_car": by_car, "lines": lines,
        "honmei_axis": top, "honmei_partners": partners,
        "honmei_bet": honmei_bet, "honmei_tags": honmei_tags,
        "honmei_outlook": honmei_outlook, "honmei_caution": honmei_caution,
        "ara_axis": ara_axis, "ara_partners": ara_partners,
        "ara_bet": ara_bet, "ara_tags": ara_tags,
        "ara_outlook": ara_outlook, "ara_caution": ara_caution,
    }


def pick(results, only_upcoming=True, n=5):
    now = now_min_jst()
    rows = []
    for rc in results:
        a = analyze(rc)
        if not a:
            continue
        if only_upcoming and a["deadline"] is not None:
            d = a["deadline"]
            if now >= 18 * 60 and d < 6 * 60:
                d += 24 * 60  # 夜の配信では、深夜(0〜6時)のレースは翌日扱い
            if d <= now:
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


def stat_text(r):
    """勝率・3連対率・決まり手(逃/捲/差/マ)"""
    parts = []
    if r.get("勝率"):
        parts.append(f"勝率{r['勝率']}%")
    if r.get("3連対率"):
        parts.append(f"3連対{r['3連対率']}%")
    kim = "".join(f"{k}{r[k]}" for k in ("逃", "捲", "差", "マ")
                  if r.get(k) not in (None, ""))
    t = " ".join(parts)
    if kim:
        t += (" ｜ " if t else "") + "決まり手 " + kim
    return t


def form_text(r):
    f = r.get("form")
    if not f or f["n"] < 3:
        return ""
    t = f"直近{f['n']}走 平均{f['avg']:.1f}着 3着内{f['top3'] * 100:.0f}%"
    if f["trend"] >= 1.0:
        t += " ↗上昇"
    elif f["trend"] <= -1.0:
        t += " ↘下降"
    if f["acc"]:
        t += f" 事故{f['acc']}"
    if f["kim"]:
        t += " ｜ 連対時 " + "".join(f"{k}{v}" for k, v in f["kim"].items())
    return t


def rider_text(by_car, car, role, level, is_axis):
    r = by_car.get(car)
    if not r:
        return f"{role} {circ(car)}"
    kyaku = r.get("脚", "")
    t = f"{role} {circ(car)} {r['name']} 得点{r['score']:.1f}"
    if kyaku:
        t += f"({kyaku})"
    t += f" 評価{r['rating']:.1f}"
    cm = (r.get("コメント") or "").strip()
    if cm and (is_axis or level >= 1):
        t += f"\n　💬{cm}"
    st = stat_text(r)
    if st and ((is_axis and level >= 2) or level >= 3):
        t += f"\n　📊{st}"
    ft = form_text(r)
    if ft and ((is_axis and level >= 2) or level >= 3):
        t += f"\n　📈{ft}"
    return t


def race_block(x, kind, idx, level=3):
    if kind == "honmei":
        axis, partners = x["honmei_axis"], x["honmei_partners"]
        tags, bet = x["honmei_tags"], x["honmei_bet"]
        outlook, caution = x["honmei_outlook"], x["honmei_caution"]
        label, score = "本命度", x["honmei"]
    else:
        axis, partners = x["ara_axis"], x["ara_partners"]
        tags, bet = x["ara_tags"], x["ara_bet"]
        outlook, caution = x["ara_outlook"], x["ara_caution"]
        label, score = "荒れ度", x["ara"]
    by_car = x["by_car"]
    if idx is None:      # 会場別表示: 会場名は見出しに出ているので省く
        icon = "🎯" if kind == "honmei" else "🌪"
        title = f"{icon}{x['race']}R ⏰{fmt_time(x['deadline'])}締切"
    else:
        title = f"【{idx}】{x['venue']}{x['race']}R ⏰{fmt_time(x['deadline'])}締切"
    out = ["━━━━━━━━━━", title, f"{label} {stars(score)}"]
    if tags:
        out.append("📌" + "・".join(tags))
    if level >= 2:
        for t in outlook:
            out.append("▶" + t)
    out.append(rider_text(by_car, axis, "◎", level, True))
    for c in partners:
        out.append(rider_text(by_car, c, "○", level, False))
    out.append(f"🎫 {bet}")
    if x["lines"]:
        out.append("並び " + " ｜ ".join("-".join(map(str, ln)) for ln in x["lines"]))
    if level >= 2 and caution:
        out.append("⚠" + " / ".join(caution))
    return "\n".join(out)


def build_message(honmei, ara, level=3):
    jst = datetime.now(timezone(timedelta(hours=9)))
    out = [f"📅 {jst.month}/{jst.day} {jst.hour}:{jst.minute:02d} 時点", ""]
    out.append(f"🎯【本命で決まりそう】{len(honmei)}レース")
    for i, x in enumerate(honmei, 1):
        out.append(race_block(x, "honmei", i, level))
    out.append("")
    out.append(f"🌪【荒れそう】{len(ara)}レース")
    for i, x in enumerate(ara, 1):
        out.append(race_block(x, "ara", i, level))
    out.append("")
    out.append("※評価=得点+直近成績・3連対率・決まり手の補正。★は目安です。参考情報で、的中や回収を保証するものではありません。")
    msg = "\n".join(out)
    if len(msg) > 4900 and level > 0:
        return build_message(honmei, ara, level - 1)  # 長すぎる場合は詳細を減らす
    return msg


FOOTER = "※評価=得点+直近成績・3連対率・決まり手の補正。★は目安です。参考情報で、的中や回収を保証するものではありません。"


def build_venue_messages(honmei, ara):
    """会場ごとにまとめたメッセージ。SPLIT=1なら会場ごとに別メッセージ(最大5通)"""
    jst = datetime.now(timezone(timedelta(hours=9)))
    head = f"📅 {jst.month}/{jst.day} {jst.hour}:{jst.minute:02d} 時点"
    groups = {}
    for kind, xs in (("honmei", honmei), ("ara", ara)):
        for x in xs:
            groups.setdefault(x["venue"], []).append((kind, x))

    def dl(x):
        return x["deadline"] if x["deadline"] is not None else 9999

    venues = sorted(groups, key=lambda v: min(dl(x) for _, x in groups[v]))

    def venue_text(v, lv):
        items = sorted(groups[v], key=lambda kx: (dl(kx[1]), kx[1]["race"]))
        nh = sum(1 for k, _ in items if k == "honmei")
        out = [f"📍【{v}】🎯本命{nh} 🌪荒れ{len(items) - nh}"]
        for kind, x in items:
            out.append(race_block(x, kind, None, lv))
        return "\n".join(out)

    def fit(fn):
        lv = 3
        t = fn(lv)
        while len(t) > 4800 and lv > 0:
            lv -= 1
            t = fn(lv)
        return t

    if SPLIT:
        texts = [fit(lambda lv, v=v: venue_text(v, lv)) for v in venues]
        texts[0] = head + "\n\n" + texts[0]
        texts[-1] = texts[-1] + "\n\n" + FOOTER
        if len(texts) > 5:                       # LINEは1回に5通まで
            texts = texts[:4] + ["\n\n".join(texts[4:])]
        return texts
    return [fit(lambda lv: head + "\n\n" + "\n\n".join(venue_text(v, lv) for v in venues)
                + "\n\n" + FOOTER)]


def build_messages(honmei, ara):
    if LAYOUT == "venue":
        return build_venue_messages(honmei, ara)
    return [build_message(honmei, ara)]


# ================= 記録(あとで的中を集計するため) =================
def save_records(results, honmei, ara):
    """配信した予想と、その日の全レースの評価を data/ に保存する(失敗しても配信には影響しない)"""
    try:
        jst = datetime.now(timezone(timedelta(hours=9)))
        date, slot = jst.strftime("%Y%m%d"), jst.strftime("%H:%M")
        os.makedirs("data/races", exist_ok=True)
        p = f"data/races/{date}.json"
        if not os.path.exists(p):          # その日の全レース(基準の集計用)。最初の配信で1回だけ
            rows = []
            for rc in results:
                a = analyze(rc)
                if a:
                    rows.append({"venue_key": a["venue_key"], "cup": a["cup"], "day": a["day"],
                                 "race": a["race"], "deadline": a["deadline"],
                                 "honmei": round(a["honmei"], 2), "ara": round(a["ara"], 2)})
            with open(p, "w", encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=False)
        with open("data/picks.jsonl", "a", encoding="utf-8") as f:
            for kind, xs in (("honmei", honmei), ("ara", ara)):
                for x in xs:
                    f.write(json.dumps({
                        "date": date, "slot": slot, "kind": kind,
                        "venue_key": x["venue_key"], "venue": x["venue"],
                        "cup": x["cup"], "day": x["day"], "race": x["race"],
                        "deadline": x["deadline"],
                        "axis": x[kind + "_axis"], "partners": x[kind + "_partners"],
                        "bet": x[kind + "_bet"], "score": round(x[kind], 2),
                    }, ensure_ascii=False) + "\n")
        print("記録を保存しました")
    except Exception as e:
        print("記録の保存に失敗(配信には影響なし):", e)


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
    payload = {"messages": [{"type": "text", "text": t[:4900]} for t in texts[:5]]}
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


def main():
    results = fetch_all_today()
    print(f"取得レース数: {len(results)}")
    if not results:
        print("レースを取得できなかったため送信しません")
        return

    pool = results
    if ENRICH:
        # 1段目: 基本データで候補を絞る(本命8+荒れ8)
        cand_h, cand_a = pick(results, only_upcoming=True, n=8)
        keys = {(x["venue_key"], x["race"]) for x in cand_h + cand_a}
        cand = [rc for rc in results if (rc["venue"], rc["race"]) in keys]
        print(f"候補 {len(cand)}レースの選手ページを取得します")
        try:
            enrich_riders(cand)
            pool = cand
        except Exception as e:
            print("選手ページの取得でエラー。基本データだけで続行:", e)

    # 2段目: 直近成績も入れた評価で、最終の5+5を決める
    honmei, ara = pick(pool, only_upcoming=True)
    if not honmei and not ara:
        print("対象レースがないため送信しません")
        return
    send_line(build_messages(honmei, ara))
    save_records(results, honmei, ara)


if __name__ == "__main__":
    main()
