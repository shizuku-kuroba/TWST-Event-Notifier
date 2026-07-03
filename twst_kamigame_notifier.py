#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
扭曲仙境 (Twisted Wonderland) JP 服活動行程 -> Discord 推送
資料來源: kamigame.jp (神ゲー攻略) - 最新情報と速報まとめ

https://kamigame.jp/ツイステ/page/375175514312182214.html

流程：
1. 抓取「最新情報と速報まとめ」頁面的所有表格連結。
2. 過濾出屬於文章頁面 (/page/數字.html) 的連結，並跟本地 state.json 比對。
3. 針對「新出現」的連結，打開該頁面抓取：
   - 文章標題 (<h1>)
   - 簡介文字 (第一段夠長的 <p>)
   - 舉辦時間 (從表格中提取正則匹配的日期時間)
4. 用 Discord Webhook 送出 embed 訊息。
5. 將已推送過的連結存入 state.json。
"""

import os
import re
import sys
import json
import time
from pathlib import Path
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

BASE_URL = "https://kamigame.jp"
# 改為使用者指定的新來源：最新情報と速報まとめ
SUMMARY_URL = "https://kamigame.jp/%E3%83%84%E3%82%A4%E3%82%B9%E3%83%86/page/375175514312182214.html"
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

# 匹配「06月30日(火)16:00」或「2026年06月30日（火）16:00」
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
    """
    從總結頁面中找出所有表格裡的有效文章連結。
    回傳 dict: { url: fallback_title }
    """
    links_data = {}
    
    # 限制只在主要內容區塊尋找，避免抓到側邊欄或 header 的共用連結
    main_content = soup.find("article") or soup.find("div", class_="kamigame-layout-main") or soup
    
    for table in main_content.find_all("table"):
        for a in table.find_all("a"):
            href = a.get("href", "")
            # 只抓取特定的文章頁面
            if "/page/" in href and href.endswith(".html"):
                url = href if href.startswith("http") else BASE_URL + href
                title = a.get_text(strip=True)
                
                # 過濾掉非主活動的衍生資訊或卡池
                bad_keywords = [
                    "家具", "ガチャ", "引くべき", "評価", "ステータス", 
                    "グッズ", "章", "Chapter", "召喚", "PU", "編成", 
                    "ミッション", "ボイス", "プロフィール"
                ]
                if any(bad in title for bad in bad_keywords):
                    continue

                # 避免覆蓋已有標題
                if url not in links_data or not links_data[url]:
                    links_data[url] = title

    return links_data

def fetch_article_detail(url: str) -> dict:
    """
    打開個別活動/情報頁面，抓取標題、簡介、時間
    """
    detail = {
        "title": "未知標題",
        "description": "",
        "start": None,
        "end": None
    }
    
    try:
        html = fetch_html(url)
        soup = BeautifulSoup(html, "html.parser")
        
        # 1. 抓標題 (優先找 h1)
        h1 = soup.find("h1")
        if h1:
            # 移除 span (例如 【ツイステ】 前綴通常包在 span 裡)
            for span in h1.find_all("span"):
                span.decompose()
            detail["title"] = h1.get_text(strip=True)
        else:
            title_tag = soup.find("title")
            if title_tag:
                detail["title"] = title_tag.get_text(strip=True).replace(" - 神ゲー攻略", "")
                
        # 2. 抓簡介文字
        content = soup.find("article") or soup.find("div", class_=re.compile("article|entry|content"))
        if content:
            for p in content.find_all(["p", "li"]):
                text = p.get_text(" ", strip=True)
                if len(text) >= 20:
                    detail["description"] = text[:400] + ("…" if len(text) > 400 else "")
                    break
                    
        # 3. 抓日期時間
        # 尋找包含「開催」或「期間」的表格列，或者直接正則匹配
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
                                return datetime(year, int(m), int(d), int(h), int(mi)).strftime("%Y-%m-%d %H:%M")
                            except ValueError:
                                return None

                        detail["start"] = to_iso(*matches[0])
                        if len(matches) >= 2:
                            detail["end"] = to_iso(*matches[1])
                        break # 找到第一組時間就跳出
                if detail["start"]:
                    break

    except Exception as e:
        print(f"  [警告] 無法抓取詳情 {url}: {e}", file=sys.stderr)
        
    return detail

# ---------------------------------------------------------------------------
# 狀態管理
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
        
        if ev.get("start") or ev.get("end"):
            embed["fields"] = [
                {"name": "開始", "value": ev.get("start") or "未知", "inline": True},
                {"name": "結束", "value": ev.get("end") or "未知", "inline": True},
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
    now = datetime.now(timezone.utc)
    print(f"[{now.isoformat()}] 開始抓取 {SUMMARY_URL} ...")

    html = fetch_html(SUMMARY_URL)
    soup = BeautifulSoup(html, "html.parser")

    links_data = parse_summary_links(soup)
    print(f"總結頁面共找到 {len(links_data)} 個文章連結。")
    
    if not links_data:
        print("[警告] 沒有解析到任何連結，頁面結構可能已變動。")
        return

    state = load_state()
    
    new_urls = []
    for url in links_data.keys():
        if url not in state:
            new_urls.append(url)
            
    if new_urls:
        print(f"偵測到 {len(new_urls)} 個新情報，開始抓取詳情...")
        
        events_to_push = []
        for i, url in enumerate(new_urls):
            print(f"  ({i+1}/{len(new_urls)}) 抓取詳情: {url}")
            detail = fetch_article_detail(url)
            detail["url"] = url
            # 如果抓不到標題，用外部連結文字補上
            if detail["title"] == "未知標題" and links_data[url]:
                detail["title"] = links_data[url]
                
            events_to_push.append(detail)
            state[url] = {"discovered_at": now.isoformat(), "title": detail["title"]}
            time.sleep(REQUEST_INTERVAL_SEC)
            
        print("開始推送 Discord ...")
        push_to_discord(events_to_push)
    else:
        print("沒有新情報，不需推送。")

    save_state(state)
    print("完成。")


if __name__ == "__main__":
    main()
