#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
扭曲仙境 (Twisted Wonderland) JP 服活動行程 -> Discord 推送與 ICS 產生器
資料來源: kamigame.jp (神ゲー攻略)
"""

import os
import re
import sys
import json
import time
import hashlib
from pathlib import Path
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup
from ics import Calendar, Event

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

BASE_URL = "https://kamigame.jp"
EVENTS_INDEX_URL = "https://kamigame.jp/%E3%83%84%E3%82%A4%E3%82%B9%E3%83%86/%E3%82%A4%E3%83%99%E3%83%B3%E3%83%88/index.html"
SUMMARY_URL = "https://kamigame.jp/%E3%83%84%E3%82%A4%E3%82%B9%E3%83%86/page/375175514312182214.html"

STATE_FILE = Path(__file__).parent / "state.json"
ICS_FILE = Path(__file__).parent / "twst_events.ics"
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

# ---------------------------------------------------------------------------
# 抓取 & 解析
# ---------------------------------------------------------------------------

def fetch_html(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text

def parse_date_text(text, default_year=None):
    """
    從字串中提取時間，支援以下格式：
    【開催期間】06月30日（火）16:00 〜07月24日（金）14:59
    """
    start_dt, end_dt = None, None
    now = datetime.now(timezone(timedelta(hours=9)))
    
    # 拆分開始與結束時間
    parts = text.split('〜')
    
    def extract_dt(part, is_end=False):
        match = re.search(r'(?:(\d{4})年)?(\d+)月(\d+)日[^\d]*?(\d{1,2})[:：](\d{2})', part)
        if not match:
            # 嘗試沒有時分的格式
            match = re.search(r'(?:(\d{4})年)?(\d+)月(\d+)日', part)
            if not match: return None
            year_text, month, day = match.groups()
            month, day = int(month), int(day)
            hr, mn = (14, 59) if is_end else (16, 0)
        else:
            year_text, month, day, hr, mn = match.groups()
            month, day, hr, mn = map(int, (month, day, hr, mn))

        year = int(year_text) if year_text else (default_year if default_year else now.year)
        if not year_text and not default_year:
            if month > now.month + 2: year -= 1
            elif month < now.month - 2: year += 1
            
        try:
            return datetime(year, month, day, hr, mn, tzinfo=timezone(timedelta(hours=9)))
        except ValueError:
            return None

    start_dt = extract_dt(parts[0], False)
    if len(parts) > 1:
        end_dt = extract_dt(parts[1], True)
        if start_dt and end_dt and end_dt.month < start_dt.month:
            end_dt = end_dt.replace(year=end_dt.year + 1)
        
    return start_dt.isoformat() if start_dt else None, end_dt.isoformat() if end_dt else None

def parse_events_index(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    events = {}

    yearly_blocks = soup.select('div[data-group="過去のイベント開催情報"][data-trigger]')
    for block in yearly_blocks:
        year_match = re.search(r"(\d{4})年", block.get("data-trigger", ""))
        if not year_match:
            continue

        table_year = int(year_match.group(1))
        table = block.find("table")
        if table:
            extract_event_table(table, events, table_year)

    # 開催中のイベントスケジュールなど、明確な年がない現在向けの表も拾う。
    for table in soup.find_all("table"):
        if table.find_parent('div', attrs={"data-group": "過去のイベント開催情報"}):
            continue

        heading = table.find_previous(["h2", "h3", "h4"])
        heading_text = heading.get_text(strip=True) if heading else ""
        if heading_text and "開催中" not in heading_text:
            continue

        extract_event_table(table, events, None)

    return events

def event_key(ev: dict) -> str:
    if ev.get("start"):
        return f"{ev['title']}|{ev['start']}"
    if ev.get("url"):
        return f"{ev['title']}|{ev['url']}"
    return ev["title"]

def state_has_event(state: dict, key: str, ev: dict) -> bool:
    if key in state:
        return True

    for existing in state.values():
        same_title_and_start = (
            existing.get("title") == ev.get("title")
            and existing.get("start") == ev.get("start")
        )
        if same_title_and_start:
            return True

        same_url = ev.get("url") and existing.get("url") == ev.get("url")
        if same_url:
            if not ev.get("start"):
                return True
            if existing.get("start") == ev.get("start"):
                return True

    return False

def merge_event(existing: dict | None, incoming: dict) -> dict:
    if not existing:
        return incoming

    merged = {**existing, **incoming}
    for field in ("description", "url", "end", "start"):
        if not merged.get(field) and existing.get(field):
            merged[field] = existing[field]
    return merged

def normalize_state(state: dict) -> dict:
    normalized = {}
    for info in state.values():
        if not info.get("title"):
            continue

        key = event_key(info)
        normalized[key] = merge_event(normalized.get(key), info)

    return dict(
        sorted(
            normalized.items(),
            key=lambda item: (
                item[1].get("start") or "9999",
                item[1].get("title") or "",
                item[0],
            ),
        )
    )

def event_uid(key: str) -> str:
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return f"twst-{digest}@twst-kamigame-notifier"

def extract_event_table(table, events: dict, table_year: int | None) -> None:
    first_row = table.find("tr")
    headers = []
    if first_row:
        th_cells = first_row.find_all("th")
        if th_cells:
            headers = [th.get_text(strip=True) for th in th_cells]
        else:
            headers = [td.get_text(strip=True) for td in first_row.find_all("td")]

    if len(headers) >= 2 and ("イベント名" in headers[0] or "キャンペーン" in headers[0] or "マスターシェフ" in headers[0] or "開催期間" in headers[1]):
        for tr in table.find_all("tr"):
            tds = tr.find_all(["td", "th"])
            if len(tds) >= 2:
                title = tds[0].get_text(" ", strip=True)
                if not title:
                    continue
                if title in ["イベント名", "キャンペーン名", "マスターシェフ"]:
                    continue

                date_text = tds[1].get_text(" ", strip=True)
                start, end = parse_date_text(date_text, default_year=table_year)
                if not start:
                    continue

                a_tag = tr.find("a")
                href = a_tag.get("href") if a_tag else None
                url = (href if href.startswith("http") else BASE_URL + href) if href else None

                ev = {
                    "title": title,
                    "url": url,
                    "start": start,
                    "end": end,
                    "description": "",
                    "type": "event"
                }
                events[event_key(ev)] = ev

def parse_gacha_summary(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    gachas = {}
    main_content = soup.find("article") or soup.find("div", class_="kamigame-layout-main") or soup
    
    in_gacha = False
    for tag in main_content.find_all(['h2', 'table']):
        if tag.name == 'h2':
            text = tag.get_text(strip=True)
            in_gacha = ("最新ガチャ" in text)
        elif tag.name == 'table' and in_gacha:
            for a in tag.find_all("a"):
                href = a.get("href", "")
                if "/page/" in href and href.endswith(".html"):
                    url = href if href.startswith("http") else BASE_URL + href
                    title = a.get_text(strip=True)
                    if not title:
                        continue
                    
                    bad = ["評価", "ステータス", "引くべき", "家具"]
                    if any(b in title for b in bad):
                        continue
                        
                    gachas[title] = {
                        "title": title,
                        "url": url,
                        "start": None,
                        "end": None,
                        "description": "",
                        "type": "gacha"
                    }
    return gachas

def fetch_description(url: str) -> str:
    try:
        html = fetch_html(url)
        soup = BeautifulSoup(html, "html.parser")
        content = soup.find("article") or soup.find("div", class_=re.compile("article|entry|content"))
        if content:
            for p in content.find_all(["p", "li"]):
                text = p.get_text(" ", strip=True)
                if len(text) >= 20:
                    return text[:400] + ("…" if len(text) > 400 else "")
    except Exception as e:
        print(f"  [警告] 無法抓取簡述 {url}: {e}", file=sys.stderr)
    return ""

def extract_dates_from_detail(url: str):
    # 專門為 Gacha 抓取日期
    try:
        html = fetch_html(url)
        soup = BeautifulSoup(html, "html.parser")
        content = soup.find("article") or soup.find("div", class_=re.compile("article|entry|content"))
        if content:
            for table in content.find_all("table"):
                for tr in table.find_all("tr"):
                    text = tr.get_text(" ", strip=True)
                    matches = re.findall(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日[（\(][^）\)]{1,3}[）\)]\s*(\d{1,2})[:：](\d{2})", text)
                    if matches:
                        now = datetime.now(timezone.utc)
                        def to_iso(y, m, d, h, mi):
                            year = int(y) if y else now.year
                            try:
                                dt = datetime(year, int(m), int(d), int(h), int(mi), tzinfo=timezone(timedelta(hours=9)))
                                return dt.isoformat()
                            except ValueError:
                                return None
                        
                        start = to_iso(*matches[0])
                        end = to_iso(*matches[1]) if len(matches) >= 2 else None
                        return start, end
    except Exception:
        pass
    return None, None

# ---------------------------------------------------------------------------
# 狀態與行事曆管理
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    raw = STATE_FILE.read_bytes()
    if not raw.strip():
        return {}
    for encoding in ("utf-8", "utf-8-sig", "utf-16"):
        try:
            return json.loads(raw.decode(encoding))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    return {}

def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )

def generate_ics(state: dict):
    cal = Calendar()
    for key, info in state.items():
        if info.get("start"):
            e = Event()
            e.name = info.get("title", "未知活動")
            e.begin = info["start"]
            if info.get("end"):
                e.end = info["end"]
            else:
                e.end = (datetime.fromisoformat(info["start"]) + timedelta(hours=1)).isoformat()
            
            desc = info.get("description", "")
            url = info.get("url")
            if url:
                e.description = f"{desc}\n\n詳細連結: {url}"
                e.url = url
            else:
                e.description = f"{desc}\n\n(此預告目前尚無詳細連結)"
                
            e.uid = event_uid(key)
            cal.events.add(e)
            
    with open(ICS_FILE, 'w', encoding='utf-8') as f:
        f.writelines(cal.serialize_iter())
    print(f"[ICS] 已產生包含 {len(cal.events)} 個行程的 twst_events.ics")

# ---------------------------------------------------------------------------
# Discord 推送
# ---------------------------------------------------------------------------

def push_to_discord(events: list[dict]) -> set[str]:
    if not DISCORD_WEBHOOK_URL:
        print("[錯誤] 未設定 TWST_DISCORD_WEBHOOK_URL 環境變數，略過推送。", file=sys.stderr)
        return set()

    pushed_titles = set()
    for ev in events:
        if not ev.get("title"):
            continue

        embed = {
            "title": f"🎉 {ev['title']}",
            "color": 0x8AA8C6,
            "footer": {"text": "扭曲仙境 JP 服情報 (kamigame.jp)"},
        }
        if ev.get("url"):
            embed["url"] = ev["url"]
        
        start_str = datetime.fromisoformat(ev["start"]).strftime("%Y-%m-%d %H:%M") if ev.get("start") else "未知"
        end_str = datetime.fromisoformat(ev["end"]).strftime("%Y-%m-%d %H:%M") if ev.get("end") else "未知"
        
        if ev.get("start") or ev.get("end"):
            embed["fields"] = [
                {"name": "開始", "value": start_str, "inline": True},
                {"name": "結束", "value": end_str, "inline": True},
            ]
            
        if ev.get("description"):
            embed["description"] = ev["description"]

        try:
            resp = requests.post(
                DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=15
            )
        except requests.RequestException as e:
            print(f"[錯誤] 推送失敗: {ev['title']} -> {e}", file=sys.stderr)
            continue

        if resp.status_code >= 300:
            print(f"[錯誤] 推送失敗 ({resp.status_code}): {ev['title']} -> {resp.text}", file=sys.stderr)
        else:
            pushed_titles.add(event_key(ev))
            print(f"[已推送] {ev['title']}")

        time.sleep(REQUEST_INTERVAL_SEC)

    return pushed_titles

# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main():
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    now = datetime.now(timezone.utc)
    print(f"[{now.isoformat()}] 開始抓取資料 ...")

    # 1. 抓取 Events Index (主線與活動)
    html_idx = fetch_html(EVENTS_INDEX_URL)
    events = parse_events_index(html_idx)
    
    # 2. 抓取 Summary Page (抽卡)
    html_sum = fetch_html(SUMMARY_URL)
    gachas = parse_gacha_summary(html_sum)
    
    # 合併兩者
    all_events = {**events, **gachas}
    print(f"共擷取到 {len(all_events)} 筆活動與抽卡資料 (歷史總計)。")
    
    state = normalize_state(load_state())
    events_to_push = []
    
    # 針對沒有存在 state 中的新事件處理
    for key, ev in all_events.items():
        if not ev.get("title"):
            continue

        if state_has_event(state, key, ev):
            continue
            
        # 已結束的活動只補進 state/ICS，不補發 Discord。
        if ev.get("start"):
            try:
                start_dt = datetime.fromisoformat(ev["start"])
                end_dt = datetime.fromisoformat(ev["end"]) if ev.get("end") else start_dt
                if end_dt < now:
                    state[key] = {"discovered_at": now.isoformat(), **ev}
                    continue
            except ValueError:
                pass
                
        print(f"  偵測到新情報: {ev['title']}")
        
        # 如果是 Gacha，補齊日期
        if ev["type"] == "gacha" and ev["url"]:
            start, end = extract_dates_from_detail(ev["url"])
            ev["start"] = start
            ev["end"] = end
            time.sleep(REQUEST_INTERVAL_SEC)
            
        # 如果有 URL，補齊簡述
        if ev.get("url"):
            ev["description"] = fetch_description(ev["url"])
            time.sleep(REQUEST_INTERVAL_SEC)
            
        events_to_push.append(ev)

    if events_to_push:
        print(f"開始推送 {len(events_to_push)} 筆新情報到 Discord ...")
        pushed_titles = push_to_discord(events_to_push)
        for ev in events_to_push:
            key = event_key(ev)
            if key not in pushed_titles:
                continue

            state[key] = {
                "discovered_at": now.isoformat(),
                **ev
            }
    else:
        print("沒有新情報，不需推送。")

    state = normalize_state(state)
    save_state(state)
    generate_ics(state)
    print("完成。")

if __name__ == "__main__":
    main()
