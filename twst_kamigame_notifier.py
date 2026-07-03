#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
扭曲仙境 (Twisted Wonderland) JP 服活動行程 -> Discord 推送與 ICS 產生器
資料來源: kamigame.jp (神ゲー攻略) - 最新情報と速報まとめ
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

JP_DATETIME_RE = re.compile(
    r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日[（\(][^）\)]{1,3}[）\)]\s*(\d{1,2})[:：](\d{2})"
)

# ---------------------------------------------------------------------------
# 抓取 & 解析
# ---------------------------------------------------------------------------

def fetch_html(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text

def parse_summary_links(soup: BeautifulSoup) -> dict:
    links_data = {}
    main_content = soup.find("article") or soup.find("div", class_="kamigame-layout-main") or soup
    
    TARGET_H2 = ["メインストーリー最新話配信情報", "最新イベント情報", "最新ガチャ"]
    in_target_section = False
    
    for tag in main_content.find_all(['h2', 'table']):
        if tag.name == 'h2':
            text = tag.get_text(strip=True)
            in_target_section = any(target in text for target in TARGET_H2)
        elif tag.name == 'table' and in_target_section:
            for a in tag.find_all("a"):
                href = a.get("href", "")
                if "/page/" in href and href.endswith(".html"):
                    url = href if href.startswith("http") else BASE_URL + href
                    title = a.get_text(strip=True)
                    
                    bad_keywords = [
                        "家具", "引くべき", "評価", "ステータス", "グッズ", 
                        "編成", "ミッション", "ボイス", "プロフィール",
                        "攻略チャート", "効率的な進め方"
                    ]
                    if re.match(r"^Chapter\d+$", title, re.IGNORECASE):
                        continue
                    if any(bad in title for bad in bad_keywords):
                        continue

                    if url not in links_data or not links_data[url]:
                        links_data[url] = title

    return links_data

def fetch_article_detail(url: str) -> dict:
    detail = {
        "title": "未知標題",
        "description": "",
        "start": None,
        "end": None
    }
    try:
        html = fetch_html(url)
        soup = BeautifulSoup(html, "html.parser")
        
        h1 = soup.find("h1")
        if h1:
            for span in h1.find_all("span"):
                span.decompose()
            detail["title"] = h1.get_text(strip=True)
        else:
            title_tag = soup.find("title")
            if title_tag:
                detail["title"] = title_tag.get_text(strip=True).replace(" - 神ゲー攻略", "")
                
        content = soup.find("article") or soup.find("div", class_=re.compile("article|entry|content"))
        if content:
            for p in content.find_all(["p", "li"]):
                text = p.get_text(" ", strip=True)
                if len(text) >= 20:
                    detail["description"] = text[:400] + ("…" if len(text) > 400 else "")
                    break
                    
        if content:
            for table in content.find_all("table"):
                for tr in table.find_all("tr"):
                    text = tr.get_text(" ", strip=True)
                    matches = JP_DATETIME_RE.findall(text)
                    if matches:
                        now = datetime.now(timezone.utc)
                        def to_iso(y, m, d, h, mi):
                            year = int(y) if y else now.year
                            try:
                                # 日本時間 UTC+9
                                dt = datetime(year, int(m), int(d), int(h), int(mi), tzinfo=timezone(timedelta(hours=9)))
                                return dt.isoformat()
                            except ValueError:
                                return None

                        detail["start"] = to_iso(*matches[0])
                        if len(matches) >= 2:
                            detail["end"] = to_iso(*matches[1])
                        break 
                if detail["start"]:
                    break

    except Exception as e:
        print(f"  [警告] 無法抓取詳情 {url}: {e}", file=sys.stderr)
        
    return detail

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
    for url, info in state.items():
        if info.get("start"):
            e = Event()
            e.name = info.get("title", "未知活動")
            e.begin = info["start"]
            if info.get("end"):
                e.end = info["end"]
            else:
                # 若無結束時間，預設設定為 1 小時長度
                e.end = (datetime.fromisoformat(info["start"]) + timedelta(hours=1)).isoformat()
            
            desc = info.get("description", "")
            e.description = f"{desc}\n\n詳細連結: {url}"
            e.url = url
            e.uid = url.replace("https://kamigame.jp/", "").replace("/", "_")
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
            "url": ev["url"],
            "color": 0x8AA8C6,
            "footer": {"text": "扭曲仙境 JP 服情報 (kamigame.jp)"},
        }
        
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
    print(f"[{now.isoformat()}] 開始抓取 {SUMMARY_URL} ...")

    html = fetch_html(SUMMARY_URL)
    soup = BeautifulSoup(html, "html.parser")

    links_data = parse_summary_links(soup)
    print(f"總結頁面共找到 {len(links_data)} 個文章連結。")
    
    state = load_state()
    new_urls = [url for url in links_data.keys() if url not in state]
            
    if new_urls:
        print(f"偵測到 {len(new_urls)} 個新情報，開始抓取詳情...")
        events_to_push = []
        for i, url in enumerate(new_urls):
            print(f"  ({i+1}/{len(new_urls)}) 抓取詳情: {url}")
            detail = fetch_article_detail(url)
            detail["url"] = url
            
            if detail["title"] == "未知標題" and links_data[url]:
                detail["title"] = links_data[url]
                
            bad_keywords = [
                "家具", "引くべき", "評価", "ステータス", "グッズ", 
                "編成", "ミッション", "ボイス", "プロフィール",
                "攻略チャート", "効率的な進め方"
            ]
            if re.match(r"^Chapter\d+$", detail["title"], re.IGNORECASE) or any(bad in detail["title"] for bad in bad_keywords):
                print(f"  [略過] 偵測到非活動內容: {detail['title']}")
                state[url] = {"discovered_at": now.isoformat(), "title": detail["title"]}
                time.sleep(REQUEST_INTERVAL_SEC)
                continue
                
            events_to_push.append(detail)
            
            # 將詳細資訊寫入 state 以供 ICS 產生使用
            state[url] = {
                "discovered_at": now.isoformat(),
                "title": detail["title"],
                "description": detail.get("description", ""),
                "start": detail.get("start"),
                "end": detail.get("end")
            }
            time.sleep(REQUEST_INTERVAL_SEC)
            
        print("開始推送 Discord ...")
        push_to_discord(events_to_push)
    else:
        print("沒有新情報，不需推送。")

    save_state(state)
    generate_ics(state)
    print("完成。")

if __name__ == "__main__":
    main()
