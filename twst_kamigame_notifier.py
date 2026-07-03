#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
扭曲仙境 (Twisted Wonderland) JP 服活動行程 -> Discord 推送
資料來源: kamigame.jp (神ゲー攻略)

https://kamigame.jp/ツイステ/イベント/index.html

流程：
1. 抓取「開催中のイベントスケジュール」(目前開放中) 表格
2. 額外抓取「過去のイベント開催情報」中『今年』區塊的表格，
   當作備援 (有些活動可能剛結束但還沒被 diff 記錄過)
3. 針對每個活動，再打開活動自己的頁面，抓取一段簡介文字當作「詳情」
4. 跟本地 state.json 比對，只有「新活動」或「日期有變動」才會推送
5. 用 Discord Webhook 送出 embed 訊息

使用前準備：
    pip install requests beautifulsoup4

環境變數：
    TWST_DISCORD_WEBHOOK_URL   Discord Webhook 網址 (必填)

執行：
    python twst_kamigame_notifier.py
"""

import os
import re
import sys
import json
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

BASE_URL = "https://kamigame.jp"
EVENTS_URL = "https://kamigame.jp/ツイステ/イベント/index.html"
STATE_FILE = Path(__file__).parent / "state.json"
DISCORD_WEBHOOK_URL = os.environ.get("TWST_DISCORD_WEBHOOK_URL", "").strip()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en;q=0.8,zh-TW;q=0.6",
}

REQUEST_INTERVAL_SEC = 1.0

# 「05月27日（水）16:00」這種日期字串的正則
JP_DATETIME_RE = re.compile(
    r"(\d{1,2})月(\d{1,2})日（[^）]*）\s*(\d{1,2})[:：](\d{2})"
)
# 表格區塊標題中「2026年の開催イベント」的年份
YEAR_HEADING_RE = re.compile(r"(\d{4})年")


# ---------------------------------------------------------------------------
# 抓取 & 解析
# ---------------------------------------------------------------------------

def fetch_html(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


def infer_year_for_current_table(month: int, day: int, now: datetime) -> int:
    """
    『開催中のイベントスケジュール』表格沒有年份，用『離現在最近』的原則推斷年份：
    如果用今年算出來的日期距離現在超過 200 天以前，就當作是明年 (跨年場景)。
    """
    candidate = datetime(now.year, month, day, tzinfo=timezone.utc)
    if (now - candidate).days > 200:
        candidate = datetime(now.year + 1, month, day, tzinfo=timezone.utc)
    return candidate.year


def parse_event_row(row, year_hint: int | None, now: datetime) -> dict | None:
    """解析單一表格列，year_hint=None 代表要用 infer_year_for_current_table 猜年份"""
    cells = row.find_all("td")
    if len(cells) < 2:
        return None

    name_cell, period_cell = cells[0], cells[1]

    link = name_cell.find("a")
    if not link or not link.get("href"):
        return None

    name = link.get_text(strip=True)
    if not name:
        # 有些名稱在 <img alt="..."> 裡
        img = name_cell.find("img")
        name = img.get("alt", "").strip() if img else ""
    if not name:
        return None

    url = link["href"]
    if url.startswith("/"):
        url = BASE_URL + url

    period_text = period_cell.get_text(" ", strip=True)
    matches = JP_DATETIME_RE.findall(period_text)
    if not matches:
        return None

    def to_iso(m, d, h, mi, year):
        try:
            return datetime(year, int(m), int(d), int(h), int(mi)).strftime(
                "%Y-%m-%d %H:%M"
            )
        except ValueError:
            return None

    start_year = year_hint or infer_year_for_current_table(
        int(matches[0][0]), int(matches[0][1]), now
    )
    start = to_iso(*matches[0], year=start_year)

    end = None
    if len(matches) >= 2:
        end_year = year_hint or infer_year_for_current_table(
            int(matches[1][0]), int(matches[1][1]), now
        )
        end = to_iso(*matches[1], year=end_year)

    return {"name": name, "url": url, "start": start, "end": end}


def parse_current_events(soup: BeautifulSoup, now: datetime) -> list[dict]:
    heading = soup.find(id="開催中のイベントスケジュール")
    if not heading:
        print("[警告] 找不到「開催中のイベントスケジュール」區塊。", file=sys.stderr)
        return []
    table = heading.find_next("table")
    if not table:
        return []

    events = []
    for row in table.find_all("tr"):
        ev = parse_event_row(row, year_hint=None, now=now)
        if ev:
            events.append(ev)
    return events


def parse_current_year_history(soup: BeautifulSoup, now: datetime) -> list[dict]:
    """抓『過去のイベント開催情報』中『今年』的表格，當作備援/補齊資料"""
    events = []
    for heading in soup.find_all(["h2", "h3"]):
        text = heading.get_text(strip=True)
        m = YEAR_HEADING_RE.search(text)
        if not m:
            continue
        year = int(m.group(1))
        if year != now.year:
            continue
        table = heading.find_next("table")
        if not table:
            continue
        for row in table.find_all("tr"):
            ev = parse_event_row(row, year_hint=year, now=now)
            if ev:
                events.append(ev)
        break  # 只需要今年這一個區塊
    return events


def fetch_event_detail(url: str) -> str:
    """打開活動自己的頁面，取開頭一段文字當作『詳情』"""
    try:
        html = fetch_html(url)
        soup = BeautifulSoup(html, "html.parser")

        # kamigame.jp 內文通常在 <article> 或帶有 entry/article class 的區塊裡
        content = soup.find("article") or soup.find(
            "div", class_=re.compile("article|entry|content")
        )
        if not content:
            return ""

        # 找內文中第一段像樣的文字 (跳過導覽列、廣告等短字串)
        for p in content.find_all(["p", "li"]):
            text = p.get_text(" ", strip=True)
            if len(text) >= 20:
                return text[:400] + ("…" if len(text) > 400 else "")
        return ""
    except Exception as e:
        print(f"  [警告] 無法抓取詳情 {url}: {e}", file=sys.stderr)
        return ""


# ---------------------------------------------------------------------------
# 狀態管理
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    raw = STATE_FILE.read_bytes()
    if not raw.strip():
        return {}
    # 依序嘗試常見編碼，容忍 Windows 編輯器/PowerShell 存出來的 UTF-16 或帶 BOM 的 UTF-8
    for encoding in ("utf-8", "utf-8-sig", "utf-16"):
        try:
            return json.loads(raw.decode(encoding))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    print(
        f"[警告] {STATE_FILE} 無法用常見編碼解析，當作空白重新開始。",
        file=sys.stderr,
    )
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def diff_events(events: list[dict], state: dict) -> list[dict]:
    changed = []
    for ev in events:
        key = ev["url"]
        sig = f"{ev['start']}|{ev['end']}"
        prev = state.get(key)
        if prev is None or prev.get("sig") != sig:
            changed.append(ev)
        state[key] = {"sig": sig, "name": ev["name"]}
    return changed


# ---------------------------------------------------------------------------
# Discord 推送
# ---------------------------------------------------------------------------

def push_to_discord(events: list[dict]) -> None:
    if not DISCORD_WEBHOOK_URL:
        print("[錯誤] 未設定 TWST_DISCORD_WEBHOOK_URL 環境變數，略過推送。", file=sys.stderr)
        return

    for ev in events:
        detail = fetch_event_detail(ev["url"])

        embed = {
            "title": f"🎉 {ev['name']}",
            "url": ev["url"],
            "color": 0x8AA8C6,
            "fields": [
                {"name": "開始", "value": ev["start"] or "未知", "inline": True},
                {"name": "結束", "value": ev["end"] or "未知（進行中/無明確結束）", "inline": True},
            ],
            "footer": {"text": "扭曲仙境 JP 服活動通知 (kamigame.jp)"},
        }
        if detail:
            embed["description"] = detail

        resp = requests.post(
            DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=15
        )
        if resp.status_code >= 300:
            print(
                f"[錯誤] 推送失敗 ({resp.status_code}): {ev['name']} -> {resp.text}",
                file=sys.stderr,
            )
        else:
            print(f"[已推送] {ev['name']} ({ev['start']} ~ {ev['end']})")

        time.sleep(REQUEST_INTERVAL_SEC)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main():
    now = datetime.now(timezone.utc)
    print(f"[{now.isoformat()}] 開始抓取 {EVENTS_URL} ...")

    html = fetch_html(EVENTS_URL)
    soup = BeautifulSoup(html, "html.parser")

    current_events = parse_current_events(soup, now)
    history_events = parse_current_year_history(soup, now)

    # 合併去重 (以 url 為 key)
    merged = {}
    for ev in history_events + current_events:  # current 放後面，若重複以 current 為準
        merged[ev["url"]] = ev
    events = list(merged.values())

    print(f"共解析到 {len(events)} 個活動項目 "
          f"(開催中: {len(current_events)}, 今年歷史: {len(history_events)})。")

    if not events:
        print("[警告] 沒有解析到任何活動，頁面結構可能已變動，請人工檢查。")
        return

    state = load_state()
    changed = diff_events(events, state)

    if changed:
        print(f"偵測到 {len(changed)} 個新增/變動的活動，開始推送 Discord ...")
        push_to_discord(changed)
    else:
        print("沒有新的活動或日期變動，不需推送。")

    save_state(state)
    print("完成。")


if __name__ == "__main__":
    main()
