# 競輪 出走表取得 + 本命/荒れ分析 + LINE配信
# 必要: requests, beautifulsoup4 (Colabには入っています)
import os
import re
import time
from datetime import datetime, timezone, timedelta
import requests
from bs4 import BeautifulSoup
from collections import Counter

BASE = "https://www.winticket.jp"
HEADERS = {"User-Agent": "Mozilla/5.0"}
SLEEP = 1.5  # レース間の待ち時間(秒)


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
    return "".join(s.split())


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
    races = get_today_races()
    if limit:
        races = races[:limit]
    results = []
    for i, rc in enumerate(races):
        data = parse_race(rc["url"])
        if data:
            data.update({"venue": rc["venue"], "race": rc["race"], "day": rc["day"]})
            results.append(data)
        print(f"{i+1}/{len(races)} {rc['venue']} {rc['race']}R "
              f"{'OK' if data and data['riders'] else 'NG'}")
        time.sleep(SLEEP)
    return results

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


def deadline_min(title):
    """タイトルから締切時刻(0時からの分)を取る。取れなければNone"""
    m = re.search(r"締切\s*(\d{1,2}):(\d{2})", title or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def now_min_jst():
    n = datetime.now(timezone(timedelta(hours=9)))
    return n.hour * 60 + n.minute


def analyze(race):
    """1レースを分析して、本命度・荒れ度・買い目を返す"""
    riders = [r for r in race["riders"]
              if r.get("score") is not None and r.get("車") is not None]
    if len(riders) < 5:
        return None
    riders.sort(key=lambda r: -r["score"])
    sc = [r["score"] for r in riders]
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

    # ---- 買い目(本命向き): 得点1位を軸、同ラインと得点上位を相手 ----
    partners = [c for c in top_line if c != top]
    for r in riders[1:]:
        if r["車"] not in partners and r["車"] != top:
            partners.append(r["車"])
    partners = partners[:3]
    honmei_bet = f"{top} － {','.join(map(str, partners))} (2車単 軸流し)"

    # ---- 買い目(荒れ向き): 逃げ選手の番手を狙う ----
    fronts = [r for r in riders if r.get("脚") == "逃" and r["車"] != top]
    if fronts and fronts[0]["車"] in line_of:
        f = fronts[0]["車"]
        ln = lines[line_of[f]]
        i = ln.index(f)
        target = ln[i + 1] if i + 1 < len(ln) else f
        ara_bet = f"{target} － {f},{top} (2車単 番手狙い)"
    else:
        ara_bet = f"{riders[1]['車']} － {top},{riders[2]['車']} (2車単)"

    return {
        "venue": VENUE_JP.get(race["venue"], race["venue"]),
        "race": race["race"],
        "deadline": deadline_min(race.get("title")),
        "honmei": honmei, "ara": ara,
        "axis": f"{top} {by_car[top]['name']}({sc[0]})",
        "honmei_bet": honmei_bet, "ara_bet": ara_bet,
        "lines": lines,
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


def build_message(honmei, ara):
    out = ["🎯【本命で決まりそう】"]
    for x in honmei:
        out.append(f"{x['venue']}{x['race']}R (締切{fmt_time(x['deadline'])})\n"
                   f" 軸 {x['axis']}\n 買い目 {x['honmei_bet']}")
    out.append("")
    out.append("🌪【荒れそう】")
    for x in ara:
        out.append(f"{x['venue']}{x['race']}R (締切{fmt_time(x['deadline'])})\n"
                   f" 買い目 {x['ara_bet']}")
    out.append("")
    out.append("※参考情報です。的中や回収を保証するものではありません。")
    return "\n".join(out)


# ================= LINE配信 =================
def send_line(text):
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
    user_id = os.environ.get("LINE_USER_ID", "")
    if not token:
        print("LINE_CHANNEL_ACCESS_TOKEN が未設定のため、送信せず表示のみ")
        print(text)
        return
    text = text[:4900]  # LINEの1通あたり上限対策
    payload = {"messages": [{"type": "text", "text": text}]}
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
    honmei, ara = pick(results, only_upcoming=True)
    if not honmei and not ara:
        print("対象レースがないため送信しません")
        return
    send_line(build_message(honmei, ara))


if __name__ == "__main__":
    main()
