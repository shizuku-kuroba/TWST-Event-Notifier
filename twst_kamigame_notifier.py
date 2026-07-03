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

def parse_date_text(text: str):
    start_dt, end_dt = None, None
    now = datetime.now(timezone.utc)
    # Start Date
    m_start = re.search(r'(\d+)[月/](\d+)[日]?', text.split('〜')[0])
    if m_start:
        month, day = int(m_start.group(1)), int(m_start.group(2))
        t_m = re.search(r'(\d{1,2}):(\d{2})', text.split('〜')[0])
        hr, mn = (int(t_m.group(1)), int(t_m.group(2))) if t_m else (16, 0)
        
        # 簡單猜測年份
        year = now.year
        if now.month == 12 and month == 1:
            year += 1
        elif now.month == 1 and month == 12:
            year -= 1
            
        try:
            start_dt = datetime(year, month, day, hr, mn, tzinfo=timezone(timedelta(hours=9)))
        except ValueError:
            start_dt = None
    
    # End Date
    if '〜' in text:
        end_part = text.split('〜')[1]
        m_end = re.search(r'(\d+)[月/](\d+)[日]?', end_part)
        if m_end:
            month, day = int(m_end.group(1)), int(m_end.group(2))
            t_m = re.search(r'(\d{1,2}):(\d{2})', end_part)
            hr, mn = (int(t_m.group(1)), int(t_m.group(2))) if t_m else (14, 59)
            
            year = now.year
            if start_dt and month < start_dt.month:
                year = start_dt.year + 1
            elif start_dt:
                year = start_dt.year
                
            try:
                end_dt = datetime(year, month, day, hr, mn, tzinfo=timezone(timedelta(hours=9)))
            except ValueError:
                end_dt = None
            
    return start_dt.isoformat() if start_dt else None, end_dt.isoformat() if end_dt else None

def parse_events_index(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    events = {}
    
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True) for th in table.find_all("th")]
        if not headers:
            first_row = table.find("tr")
            if first_row:
                headers = [td.get_text(strip=True) for td in first_row.find_all("td")]
                
        if len(headers) >= 2 and ("イベント名" in headers[0] or "キャンペーン" in headers[0] or "マスターシェフ" in headers[0] or "開催期間" in headers[1]):
            for tr in table.find_all("tr"):
                tds = tr.find_all(["td", "th"])
                if len(tds) >= 2:
                    title = tds[0].get_text(" ", strip=True)
                    date_text = tds[1].get_text(" ", strip=True)
                    
                    if title in ["イベント名", "開催期間/主な内容"] or "今までに開催された" in title or not date_text:
                        continue
                        
                    a_tag = tr.find("a")
                    href = a_tag.get("href") if a_tag else None
                    url = (href if href.startswith("http") else BASE_URL + href) if href else None
                    
                    start, end = parse_date_text(date_text)
                    if not start:
                        continue
                        
                    events[title] = {
                        "title": title,
                        "url": url,
                        "start": start,
                        "end": end,
                        "description": "",
                        "type": "event"
                    }
    return events

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
                
            e.uid = info.get("title").replace(" ", "_")
            cal.events.add(e)
            
    with open(ICS_FILE, 'w', encoding='utf-8') as f:
        f.writelines(cal.serialize_iter())
    print(f"[ICS] 已產生包含 {len(cal.events)} 個行程的 twst_events.ics")

# ---------------------------------------------------------------------------
# Discord 推送
# ---------------------------------------------------------------------------

def push_to_discord(events: list[dict]) -> None:
    if not DISCORD_WEBHOOK_URL:
        print("[錯誤] 未設定 TWST_DISCORD_WEBHOOK_URL 環境變數，略過推送。", file=sys.stderr)
        return

    for ev in events:
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

        resp = requests.post(
            DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=15
        )
        if resp.status_code >= 300:
            print(f"[錯誤] 推送失敗 ({resp.status_code}): {ev['title']} -> {resp.text}", file=sys.stderr)
        else:
            print(f"[已推送] {ev['title']}")

        time.sleep(REQUEST_INTERVAL_SEC)

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
    
    state = load_state()
    events_to_push = []
    
    # 針對沒有存在 state 中的新事件處理
    for title, ev in all_events.items():
        if title in state:
            continue
            
        # 避免處理太久遠的歷史資料（如果 state 剛好被清空）
        if ev.get("start"):
            try:
                start_dt = datetime.fromisoformat(ev["start"])
                # 如果是超過 60 天前的舊活動，就只加入 state 不推播
                if now - start_dt > timedelta(days=60):
                    state[title] = {"discovered_at": now.isoformat(), **ev}
                    continue
            except ValueError:
                pass
                
        print(f"  偵測到新情報: {title}")
        
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
        
        state[title] = {
            "discovered_at": now.isoformat(),
            **ev
        }

    if events_to_push:
        print(f"開始推送 {len(events_to_push)} 筆新情報到 Discord ...")
        push_to_discord(events_to_push)
    else:
        print("沒有新情報，不需推送。")

    save_state(state)
    generate_ics(state)
    print("完成。")

if __name__ == "__main__":
    main()
