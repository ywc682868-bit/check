#!/usr/bin/env python3
"""Pokémon GO 活動提醒 → Telegram

每天執行三次（早上完整版、下午與晚上只報新增或變更）。
只用 Python 標準函式庫，不需要安裝任何套件。

環境變數：
  TELEGRAM_BOT_TOKEN  Telegram 機器人權杖（必填，除非 DRY_RUN=1）
  TELEGRAM_CHAT_ID    你的聊天室 ID（必填，除非 DRY_RUN=1）
  MODE                morning / delta / auto（預設 auto：台灣時間 11 點前跑完整版）
  DRY_RUN             設為 1 時只印出訊息，不發送
  NOW                 測試用，假裝現在是這個台灣時間，例如 2026-10-04T22:00
"""
import difflib
import hashlib
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "state.json"
LOCATIONS_FILE = HERE / "locations.json"
TPE = ZoneInfo("Asia/Taipei")
UA = "Mozilla/5.0 (compatible; pogo-telegram-notifier)"

EVENTS_URL = "https://raw.githubusercontent.com/bigfoott/ScrapedDuck/data/events.min.json"
NEWS_URL = "https://pokemongo.com/zh-Hant/news"
FEEDS = {
    "Pokemon Hubs": "https://pokemonhubs.com/feed/",
    "ポケらく": "https://pokemongo-raku.com/feed",
}

# 當地時間活動要換算的時區：(顯示名稱, IANA 時區)
ZONES = [
    ("吉里巴斯（全球最早）", "Pacific/Kiritimati"),
    ("紐西蘭", "Pacific/Auckland"),
    ("日本", "Asia/Tokyo"),
    ("台灣", "Asia/Taipei"),
    ("英國", "Europe/London"),
    ("巴西（巴西利亞）", "America/Sao_Paulo"),
    ("美國東岸", "America/New_York"),
    ("美國西岸", "America/Los_Angeles"),
    ("夏威夷", "Pacific/Honolulu"),
    ("美屬薩摩亞（全球最晚）", "Pacific/Pago_Pago"),
]
LAST_ZONE = ZoneInfo("Pacific/Pago_Pago")
FIRST_ZONE = ZoneInfo("Pacific/Kiritimati")

TYPE_NAMES = {
    "community-day": "社群日", "event": "活動", "go-battle-league": "GO對戰聯盟",
    "go-pass": "GO Pass", "max-battles": "極巨對戰", "max-mondays": "極巨星期一",
    "pokemon-go-tour": "GO Tour", "pokemon-spotlight-hour": "聚焦時刻",
    "raid-battles": "團體戰", "raid-day": "團體戰日", "raid-hour": "團體戰時刻",
    "research": "調查", "season": "季節", "wild-area": "曠野地帶",
    "pokemon-go-fest": "GO Fest", "city-safari": "City Safari",
}
SKIP_TYPES = {"go-battle-league", "season", "twitch-drops"}

errors = []


# ---------- 基本工具 ----------

def now_tpe():
    fake = os.environ.get("NOW")
    if fake:
        return datetime.fromisoformat(fake).replace(tzinfo=TPE)
    return datetime.now(TPE)


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def try_fetch(label, url):
    try:
        return fetch(url)
    except Exception as e:  # 任何來源失敗都不能讓整支程式停掉
        errors.append(f"{label}：{type(e).__name__}")
        return None


def esc(s):
    return html.escape(str(s), quote=False)


def fmt(dt):
    """台灣時間，含星期。"""
    return dt.astimezone(TPE).strftime("%m/%d") + "（" + "一二三四五六日"[dt.astimezone(TPE).weekday()] + "）" + dt.astimezone(TPE).strftime("%H:%M")


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


# ---------- 活動時間 ----------

def parse_time(s):
    """回傳 (naive 或 aware datetime, 是否為各地當地時間)。"""
    if not s:
        return None, False
    if s.endswith("Z"):
        return datetime.fromisoformat(s[:-1].split(".")[0] + "+00:00"), False
    return datetime.fromisoformat(s.split(".")[0]), True


def at(naive, tz):
    return naive.replace(tzinfo=tz)


class Event:
    def __init__(self, raw):
        self.id = raw.get("eventID", "")
        self.name = raw.get("name", "")
        self.type = raw.get("eventType", "event")
        self.link = raw.get("link", "")
        self.start, self.start_local = parse_time(raw.get("start"))
        self.end, self.end_local = parse_time(raw.get("end"))
        self.raw_start, self.raw_end = raw.get("start"), raw.get("end")

    def start_in(self, tz=TPE):
        if self.start is None:
            return None
        return at(self.start, tz) if self.start_local else self.start

    def end_in(self, tz=TPE):
        if self.end is None:
            return None
        return at(self.end, tz) if self.end_local else self.end

    @property
    def label(self):
        return TYPE_NAMES.get(self.type, self.type)

    def title(self):
        t = f"[{esc(self.label)}] {esc(self.name)}"
        return f'<a href="{html.escape(self.link)}">{t}</a>' if self.link else t


def load_events():
    text = try_fetch("活動時間表", EVENTS_URL)
    if not text:
        return []
    try:
        return [Event(r) for r in json.loads(text) if r.get("eventType") not in SKIP_TYPES]
    except Exception as e:
        errors.append(f"活動時間表解析：{type(e).__name__}")
        return []


def zone_table(naive):
    """當地時間 → 各時區對應的台灣時間。"""
    rows = []
    for name, tz in ZONES:
        rows.append(f"　{esc(name)}：{fmt(at(naive, ZoneInfo(tz)))}")
    return "\n".join(rows)


def world_window_lines(ev, now):
    """當地時間活動：寫出台灣、巴西、全球最晚的結束時刻。"""
    if not (ev.end and ev.end_local):
        return [f"　結束：{fmt(ev.end_in())}（全球同時）"] if ev.end else []
    lines = [
        f"　台灣結束：{fmt(ev.end_in(TPE))}",
        f"　巴西結束：{fmt(ev.end_in(ZoneInfo('America/Sao_Paulo')))}",
        f"　全球最晚（美屬薩摩亞）：{fmt(ev.end_in(LAST_ZONE))}",
    ]
    return lines


# ---------- 官方新聞與攻略站 ----------

def strip_tags(s):
    s = re.sub(r"(?is)<(script|style|noscript|svg|header|footer|nav).*?</\1>", " ", s)
    s = re.sub(r"(?s)<[^>]+>", "\n", s)
    lines = [html.unescape(x).strip() for x in s.split("\n")]
    return [x for x in lines if x]


def official_news():
    """回傳 [(標題, 網址)]。"""
    text = try_fetch("官方新聞列表", NEWS_URL)
    if not text:
        return []
    out, seen = [], set()
    for m in re.finditer(r'(?is)<a\b[^>]*href="([^"]*?/news/[A-Za-z0-9\-_]+)"[^>]*>(.*?)</a>', text):
        url = urllib.parse.urljoin(NEWS_URL, m.group(1))
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        if slug in seen:
            continue
        title = " ".join(strip_tags(m.group(2)))[:120] or slug
        seen.add(slug)
        out.append((title, url))
    if not out:
        errors.append("官方新聞列表：找不到文章連結（網頁結構可能改了）")
    return out[:40]


def article_lines(url):
    text = try_fetch("官方公告內文", url)
    if not text:
        return None
    m = re.search(r"(?is)<(article|main)\b.*?</\1>", text)
    lines = strip_tags(m.group(0) if m else text)
    # 只留有實質內容的行，避免頁面雜訊造成誤報
    return [x for x in lines if len(x) >= 8][:400]


def feed_items(label, url):
    text = try_fetch(label, url)
    if not text:
        return []
    try:
        root = ET.fromstring(text.encode("utf-8"))
    except Exception:
        errors.append(f"{label}：RSS 解析失敗")
        return []
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        if title and link:
            out.append((title, link))
    return out[:30]


# ---------- 地點限定活動 ----------

def active_locations(now):
    groups = load_json(LOCATIONS_FILE, [])
    out = []
    for g in groups:
        tz = ZoneInfo(g.get("tz", "Asia/Taipei"))
        start = datetime.fromisoformat(g["start"]).replace(tzinfo=tz)
        end = datetime.fromisoformat(g["end"]).replace(tzinfo=tz)
        if start - timedelta(days=7) <= now <= end:
            out.append((g, start, end))
    return out


def locations_section(now, only_ending_days=None):
    lines = []
    for g, start, end in active_locations(now):
        days_left = (end - now).total_seconds() / 86400
        if only_ending_days is not None and days_left > only_ending_days:
            continue
        head = f"<b>{esc(g['name'])}</b>"
        if now < start:
            head += f"（{fmt(start)} 開始）"
        lines.append(head)
        lines.append(f"　到 {end.astimezone(TPE).strftime('%Y/%m/%d %H:%M')}（台灣時間）｜{esc(g.get('reward', ''))}")
        for name, lat, lon in g.get("places", []):
            lines.append(f"　・{esc(name)}　<code>{lat},{lon}</code>")
    return lines


# ---------- 組訊息 ----------

def build(mode, now, state):
    events = load_events()
    first_run = not state.get("initialized")
    seen_events = state.setdefault("events", {})
    seen_articles = state.setdefault("articles", {})
    seen_feed = state.setdefault("feed", [])

    # --- 新增或變更的活動 ---
    new_events, changed_events = [], []
    for ev in events:
        sig = f"{ev.raw_start}|{ev.raw_end}"
        old = seen_events.get(ev.id)
        if old is None:
            if not first_run:
                new_events.append(ev)
        elif old != sig:
            changed_events.append((ev, old))
        seen_events[ev.id] = sig

    # --- 官方公告：新公告與內文更新 ---
    new_articles, updated_articles = [], []
    for title, url in official_news():
        lines = article_lines(url)
        if lines is None:
            continue
        digest = hashlib.sha256("\n".join(lines).encode()).hexdigest()
        old = seen_articles.get(url)
        if old is None:
            if not first_run:
                new_articles.append((title, url))
        elif old.get("hash") != digest:
            added = [l[2:] for l in difflib.ndiff(old.get("lines", []), lines) if l.startswith("+ ")]
            if added:
                updated_articles.append((title, url, added[:4]))
        seen_articles[url] = {"hash": digest, "lines": lines, "title": title}

    # --- 攻略站新文章 ---
    new_posts = []
    for label, url in FEEDS.items():
        for title, link in feed_items(label, url):
            if link not in seen_feed:
                if not first_run:
                    new_posts.append((label, title, link))
                seen_feed.append(link)
    state["feed"] = seen_feed[-400:]
    state["initialized"] = True

    # --- 時間相關 ---
    def ends_within(hours):
        out = []
        for ev in events:
            e = ev.end_in(TPE)
            if e and now <= e <= now + timedelta(hours=hours):
                out.append(ev)
        return sorted(out, key=lambda x: x.end_in(TPE))

    # 台灣已結束，但其他時區還沒結束
    still_alive = []
    for ev in events:
        if ev.end and ev.end_local and ev.end_in(TPE) < now < ev.end_in(LAST_ZONE):
            still_alive.append(ev)
    still_alive.sort(key=lambda x: x.end_in(LAST_ZONE))

    msg = []
    today = now.date()

    if mode == "morning":
        msg.append(f"<b>Pokémon GO 今日整理</b>　{now.strftime('%m/%d')}（{'一二三四五六日'[now.weekday()]}）")

        todays = []
        for ev in events:
            s, e = ev.start_in(TPE), ev.end_in(TPE)
            if s and s.date() == today:
                todays.append((s, f"{s.strftime('%H:%M')} 開始　{ev.title()}"))
            if e and e.date() == today:
                todays.append((e, f"{e.strftime('%H:%M')} 結束　{ev.title()}"))
        msg.append("\n<b>今天（台灣時間）</b>")
        msg += [t for _, t in sorted(todays, key=lambda x: x[0])] or ["　今天沒有開始或結束的活動"]

        soon = ends_within(48)
        if soon:
            msg.append("\n<b>48 小時內結束</b>")
            for ev in soon:
                msg.append(ev.title())
                msg += world_window_lines(ev, now)

        if still_alive:
            msg.append("\n<b>台灣已結束，其他時區還在進行</b>")
            for ev in still_alive:
                msg.append(ev.title())
                msg.append(f"　巴西結束：{fmt(ev.end_in(ZoneInfo('America/Sao_Paulo')))}")
                msg.append(f"　全球最晚：{fmt(ev.end_in(LAST_ZONE))}")

        running = [ev for ev in events if ev.start_in(TPE) and ev.end_in(TPE) and ev.start_in(TPE) <= now <= ev.end_in(TPE)]
        if running:
            msg.append("\n<b>進行中</b>")
            for ev in sorted(running, key=lambda x: x.end_in(TPE)):
                msg.append(f"{ev.title()}　到 {fmt(ev.end_in(TPE))}")

        upcoming = [ev for ev in events if ev.start_in(TPE) and now < ev.start_in(TPE) <= now + timedelta(days=7)]
        if upcoming:
            msg.append("\n<b>未來 7 天</b>")
            for ev in sorted(upcoming, key=lambda x: x.start_in(TPE)):
                early = ""
                if ev.start_local:
                    early = f"（全球最早 {fmt(ev.start_in(FIRST_ZONE))}）"
                msg.append(f"{fmt(ev.start_in(TPE))}　{ev.title()}{early}")

        loc = locations_section(now)
        if loc:
            msg.append("\n<b>地點限定活動與座標</b>")
            msg += loc
    else:
        msg.append(f"<b>Pokémon GO 新增與變更</b>　{now.strftime('%m/%d %H:%M')}")

    # --- 新增與變更（兩種模式都報） ---
    delta = []
    for ev in new_events:
        delta.append(f"🆕 {ev.title()}")
        if ev.start:
            delta.append(f"　開始：{fmt(ev.start_in(TPE))}" + ("（各地當地時間）" if ev.start_local else "（全球同時）"))
        delta += world_window_lines(ev, now)
    for ev, old in changed_events:
        delta.append(f"✏️ 時間變更　{ev.title()}")
        delta.append(f"　原本：{esc(old.replace('|', ' → '))}")
        delta.append(f"　現在：{esc(ev.raw_start)} → {esc(ev.raw_end)}")
    for title, url in new_articles:
        delta.append(f'📰 官方新公告　<a href="{html.escape(url)}">{esc(title)}</a>')
    for title, url, added in updated_articles:
        delta.append(f'✏️ 官方公告更新　<a href="{html.escape(url)}">{esc(title)}</a>')
        delta += [f"　＋{esc(a[:160])}" for a in added]
    for label, title, link in new_posts:
        delta.append(f'📝 {esc(label)}　<a href="{html.escape(link)}">{esc(title)}</a>')

    if delta:
        msg.append("\n<b>新增與變更</b>")
        msg += delta

    if mode == "delta":
        soon = ends_within(12)
        if soon or still_alive:
            msg.append("\n<b>快結束了</b>")
            for ev in soon:
                msg.append(f"{ev.title()}　台灣 {fmt(ev.end_in(TPE))}")
                if ev.end_local:
                    msg.append(f"　全球最晚：{fmt(ev.end_in(LAST_ZONE))}")
            for ev in still_alive:
                msg.append(f"{ev.title()}　台灣已結束")
                msg.append(f"　巴西：{fmt(ev.end_in(ZoneInfo('America/Sao_Paulo')))}｜全球最晚：{fmt(ev.end_in(LAST_ZONE))}")
        loc = locations_section(now, only_ending_days=2)
        if loc:
            msg.append("\n<b>兩天內結束的地點限定活動</b>")
            msg += loc
        if not delta and not soon and not still_alive and not loc:
            msg.append("這個時段沒有新增或變更的消息。")

    if first_run:
        msg.append("\n（第一次執行：已記下目前所有活動與公告，之後才會開始比對新增與變更。）")
    if errors:
        msg.append("\n<i>這次抓不到的來源：" + esc("、".join(dict.fromkeys(errors))) + "</i>")
    return "\n".join(msg)


# ---------- Telegram ----------

def chunks(text, limit=3800):
    buf = ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > limit and buf:
            yield buf
            buf = ""
        buf += line + "\n"
    if buf.strip():
        yield buf


def send(text):
    if os.environ.get("DRY_RUN") == "1":
        print(text)
        return
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat = os.environ["TELEGRAM_CHAT_ID"]
    for part in chunks(text):
        data = json.dumps({
            "chat_id": chat, "text": part, "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data,
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=30).read()
        except Exception as e:
            # 不把權杖印進錯誤訊息
            detail = getattr(e, "read", lambda: b"")()[:300]
            sys.exit(f"Telegram 發送失敗：{type(e).__name__} {detail!r}")


def main():
    now = now_tpe()
    mode = os.environ.get("MODE", "auto")
    if mode not in ("morning", "delta"):
        mode = "morning" if now.hour < 11 else "delta"
    state = load_json(STATE_FILE, {})
    text = build(mode, now, state)
    send(text)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
