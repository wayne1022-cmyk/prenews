"""
第一階段：抓取工商時報「今天」的盤前 Hub，以及 Hub 內各篇文章全文。

所有網頁抓取都集中在這一段，第二階段只需呼叫 AI，不必再碰網站。
輸出：data/premarket_result.json
  status: success / partial_success / failed / no_data
  無論成功或失敗都會輸出，讓第三階段寄出對應的通知。
"""
from __future__ import annotations

import asyncio
import random
import re
from urllib.parse import urldefrag

from playwright.async_api import Page, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeout

from common import (
    DEBUG_DIR, FAILED, NO_DATA, PARTIAL, STAGE1_FILE, SUCCESS,
    env_int, now_tw, setup_logging, today_tw, write_json,
)

log = setup_logging("stage1")

# ============================================================
# 設定
# ============================================================

BASE_URL = "https://www.ctee.com.tw"
LIVE_NEWS_URL = f"{BASE_URL}/livenews/stock"
NEWS_PREFIX = f"{BASE_URL}/news/"

START_MARKER = "利多因子速覽"
END_MARKER = "將工商時報加入Google偏好來源"

PREMARKET_RE = re.compile(r"盤前\s*[|｜]")      # 全形、半形直線都接受
NEWS_DATE_RE = re.compile(r"/news/(\d{8})")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

MAX_LOAD_MORE = env_int("MAX_LOAD_MORE", 5)
HUB_WAIT_ATTEMPTS = env_int("HUB_WAIT_ATTEMPTS", 3)   # 今天的盤前還沒發布時，總共檢查幾次
HUB_WAIT_MINUTES = env_int("HUB_WAIT_MINUTES", 10)    # 每次檢查間隔
MIN_CONTENT_CHARS = env_int("MIN_CONTENT_CHARS", 200)
PAGE_TIMEOUT_MS = 45_000

# 驗證頁的特徵。刻意不用 "captcha"、"challenge-platform" 這類字，
# 因為正常頁面也可能載入相關腳本，會造成誤判。
BLOCK_TITLE_SIGNS = ("just a moment", "attention required", "access denied", "請稍候")
BLOCK_HTML_SIGNS = ("_cf_chl_opt", "cf-challenge")


class BlockedError(RuntimeError):
    """網站回傳驗證頁或拒絕存取。"""


# ============================================================
# 共用工具
# ============================================================

async def polite_pause(base: float = 2.5, jitter: float = 2.0) -> None:
    """請求之間保留間隔，避免短時間連續打同一個網站。"""
    await asyncio.sleep(base + random.uniform(0, jitter))


async def save_debug(page: Page, label: str) -> None:
    """存下截圖與 HTML；Actions 會上傳成 artifact，方便事後確認網站回了什麼。"""
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{now_tw().strftime('%H%M%S')}_{re.sub(r'[^\w-]', '_', label)[:60]}"
    try:
        await page.screenshot(path=str(DEBUG_DIR / f"{name}.png"), full_page=True)
        (DEBUG_DIR / f"{name}.html").write_text(await page.content(), encoding="utf-8")
        log.info("已儲存除錯檔案：%s", name)
    except Exception as e:
        log.warning("儲存除錯檔案失敗：%s", e)


async def open_page(page: Page, url: str, label: str) -> None:
    """開啟網頁並檢查是否被擋。被擋時明確丟出 BlockedError，而不是讓解析默默失敗。"""
    response = await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    try:
        await page.wait_for_load_state("load", timeout=15_000)
    except PlaywrightTimeout:
        pass  # 新聞網站的廣告腳本常拖慢 load，主要內容通常已就緒

    status = response.status if response else None
    title = ((await page.title()) or "").lower()
    html_head = (await page.content())[:20_000]

    blocked = (
        status in (403, 429, 503)
        or any(s in title for s in BLOCK_TITLE_SIGNS)
        or any(s in html_head for s in BLOCK_HTML_SIGNS)
    )
    if blocked:
        await save_debug(page, f"blocked_{label}")
        raise BlockedError(f"疑似被網站阻擋（HTTP {status}，頁面標題：{title[:50]}）")

    if status is not None and status >= 400:
        await save_debug(page, f"http{status}_{label}")
        raise RuntimeError(f"HTTP {status}：{url}")


# ============================================================
# 步驟 1：找今天的盤前 Hub
# ============================================================

COLLECT_LINKS_JS = r"""
() => Array.from(document.querySelectorAll("a[href]")).map(a => ({
    text: [a.getAttribute("title"), a.innerText, a.textContent].filter(Boolean).join(" "),
    title: (a.getAttribute("title") || a.innerText || a.textContent || "").trim(),
    url: a.href,
}))
"""


async def collect_premarket_hubs(page: Page) -> list[dict]:
    hubs: dict[str, dict] = {}
    for item in await page.evaluate(COLLECT_LINKS_JS):
        if not PREMARKET_RE.search(item["text"]):
            continue
        url = urldefrag(item["url"])[0]
        if not url.startswith(NEWS_PREFIX) or url in hubs:
            continue
        match = NEWS_DATE_RE.search(url)
        hubs[url] = {
            "session": "盤前",
            "title": re.sub(r"\s+", " ", item["title"]).strip(),
            "url": url,
            "target_date": match.group(1) if match else None,
        }
    return list(hubs.values())


async def click_load_more(page: Page) -> bool:
    button = page.locator("button, a, [role='button']", has_text="載入更多").first
    try:
        if await button.count() == 0:
            return False
        await button.scroll_into_view_if_needed(timeout=5_000)
        await button.click(timeout=5_000)
    except PlaywrightTimeout:
        return False
    await polite_pause(2.0, 1.5)
    return True


async def find_today_hub(page: Page, today: str) -> tuple[dict | None, list[dict]]:
    """找到今天的盤前就立刻停止，只在必要時才點「載入更多」。"""
    await open_page(page, LIVE_NEWS_URL, "livenews")
    hubs: list[dict] = []

    for clicks in range(MAX_LOAD_MORE + 1):
        hubs = await collect_premarket_hubs(page)
        today_hubs = [h for h in hubs if h["target_date"] == today]
        if today_hubs:
            log.info("找到今天的盤前 Hub（點擊載入更多 %d 次）：%s", clicks, today_hubs[0]["title"])
            return today_hubs[0], hubs

        # 已經看到較早的盤前，代表今天的還沒發布，不必再往下載入
        if hubs or clicks == MAX_LOAD_MORE:
            break
        if not await click_load_more(page):
            break

    return None, hubs


# ============================================================
# 步驟 2：從 Hub 取出「利多因子速覽」～「加入Google偏好來源」之間的文章連結
# （保留你原本的 DOM Range 作法，只精簡寫法）
# ============================================================

RANGE_LINKS_JS = r"""
([startMarker, endMarker]) => {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    const nodes = [];
    let full = "";
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
        if (!n.nodeValue.trim()) continue;
        nodes.push({ node: n, start: full.length });
        full += n.nodeValue;
    }

    const s = full.indexOf(startMarker);
    if (s === -1) return { ok: false, reason: `找不到開始標記「${startMarker}」` };
    const e = full.indexOf(endMarker, s + startMarker.length);
    if (e === -1) return { ok: false, reason: `找不到結束標記「${endMarker}」` };

    const locate = (idx) => {
        for (let i = nodes.length - 1; i >= 0; i--) {
            if (idx >= nodes[i].start) return { node: nodes[i].node, offset: idx - nodes[i].start };
        }
        return null;
    };

    const range = document.createRange();
    const a = locate(s), b = locate(e);
    range.setStart(a.node, a.offset);
    range.setEnd(b.node, b.offset);

    const links = Array.from(document.body.querySelectorAll("a[href]"))
        .filter(el => range.intersectsNode(el))
        .map(el => ({ text: (el.innerText || el.textContent || "").trim(), href: el.href }));

    return { ok: true, textLength: range.toString().length, links };
}
"""


async def get_article_links(page: Page, hub: dict) -> list[dict]:
    await open_page(page, hub["url"], "hub")
    result = await page.evaluate(RANGE_LINKS_JS, [START_MARKER, END_MARKER])

    if not result.get("ok"):
        await save_debug(page, "hub_range_failed")
        raise RuntimeError(f"盤前 Hub 區間解析失敗：{result.get('reason')}")

    articles: list[dict] = []
    seen = {hub["url"]}
    for link in result["links"]:
        url = urldefrag(link["href"])[0]
        title = re.sub(r"\s+", " ", link["text"]).strip()
        if not url.startswith(NEWS_PREFIX) or url in seen or not title:
            continue
        seen.add(url)
        articles.append({"session": "盤前", "article_title": title, "url": url})

    log.info("區間文字 %d 字，原始連結 %d 個，有效文章 %d 篇",
             result["textLength"], len(result["links"]), len(articles))
    return articles


# ============================================================
# 步驟 3：抓取各篇文章全文
# 優先讀取 JSON-LD（新聞網站給搜尋引擎的結構化資料，乾淨且不受版面影響），
# 沒有才退回 DOM，並排除側欄、延伸閱讀等雜訊。
# ============================================================

EXTRACT_ARTICLE_JS = r"""
() => {
    const clean = t => (t || "").toString().replace(/\s+/g, " ").trim();
    const out = { title: "", author: "", publish_time: "", content: "", source: "" };

    // 1. JSON-LD
    for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
        let data;
        try { data = JSON.parse(script.textContent); } catch (e) { continue; }
        const items = Array.isArray(data) ? data : (data["@graph"] || [data]);
        for (const it of items) {
            const types = [].concat(it["@type"] || []);
            if (!types.some(t => /Article/.test(t))) continue;
            out.title = clean(it.headline);
            const au = it.author;
            out.author = clean(Array.isArray(au)
                ? au.map(x => (x && x.name) || x).join("、")
                : (au && (au.name || au)) || "");
            out.publish_time = clean(it.datePublished);
            if (it.articleBody && clean(it.articleBody).length > 100) {
                out.content = clean(it.articleBody);
                out.source = "json-ld";
            }
            break;
        }
        if (out.content) break;
    }

    // 2. DOM：只取本文容器內的段落
    if (!out.content) {
        const selectors = ["article", "[class*='article-content']", "[class*='articleContent']", "main"];
        const noise = "script,style,aside,nav,figure,iframe,form,[class*='related'],[class*='recommend'],[class*='share']";
        for (const sel of selectors) {
            const el = document.querySelector(sel);
            if (!el) continue;
            const clone = el.cloneNode(true);
            clone.querySelectorAll(noise).forEach(n => n.remove());
            const text = Array.from(clone.querySelectorAll("p"))
                .map(p => clean(p.textContent))
                .filter(t => t.length >= 15)
                .join("\n");
            if (text.length > out.content.length) {
                out.content = text;
                out.source = "dom:" + sel;
            }
            if (out.content.length >= 200) break;
        }
    }

    if (!out.title) {
        const h1 = document.querySelector("h1");
        out.title = h1 ? clean(h1.innerText) : "";
    }
    if (!out.publish_time) {
        const t = document.querySelector("time");
        const m = document.querySelector('meta[property="article:published_time"]');
        out.publish_time = (t && (t.getAttribute("datetime") || clean(t.innerText))) || (m && m.content) || "";
    }
    return out;
}
"""


async def scrape_article(page: Page, article: dict) -> dict:
    """被擋時往上丟 BlockedError（由呼叫端決定停止）；其他錯誤記錄在結果中。"""
    try:
        await open_page(page, article["url"], "article")
        data = await page.evaluate(EXTRACT_ARTICLE_JS)
    except BlockedError:
        raise
    except Exception as e:
        return {**article, "scrape_status": "failed", "scrape_error": f"{type(e).__name__}: {e}"}

    content = (data.get("content") or "").strip()
    result = {
        **article,
        "scraped_title": data.get("title", ""),
        "author": data.get("author", ""),
        "publish_time": data.get("publish_time", ""),
        "content": content,
        "content_source": data.get("source", ""),
    }

    if len(content) < MIN_CONTENT_CHARS:
        await save_debug(page, "short_content")
        return {**result, "scrape_status": "failed",
                "scrape_error": f"內文僅 {len(content)} 字，可能是付費牆或版面變動"}

    log.info("  ✓ %d 字（來源：%s）", len(content), result["content_source"])
    return {**result, "scrape_status": "success"}


async def scrape_all_articles(page: Page, articles: list[dict]) -> list[dict]:
    results: list[dict] = []
    blocked_reason: str | None = None

    for i, article in enumerate(articles, 1):
        if blocked_reason:
            # 一旦被擋就不再繼續請求，避免讓情況更糟
            results.append({**article, "scrape_status": "failed", "scrape_error": blocked_reason})
            continue

        log.info("抓取文章 %d/%d：%s", i, len(articles), article["article_title"])
        try:
            results.append(await scrape_article(page, article))
        except BlockedError as e:
            blocked_reason = f"網站阻擋，已停止抓取：{e}"
            log.error(blocked_reason)
            results.append({**article, "scrape_status": "failed", "scrape_error": str(e)})

        if i < len(articles):
            await polite_pause()

    # 非阻擋造成的失敗（逾時、暫時錯誤），稍候重試一次
    retry_idx = [] if blocked_reason else [
        i for i, r in enumerate(results) if r["scrape_status"] == "failed"
    ]
    if retry_idx:
        log.info("等待 30 秒後重試 %d 篇失敗文章", len(retry_idx))
        await asyncio.sleep(30)
        for idx in retry_idx:
            try:
                results[idx] = {**(await scrape_article(page, articles[idx])), "scrape_retried": True}
            except BlockedError as e:
                log.error("重試時被阻擋，停止重試：%s", e)
                break
            await polite_pause()

    return results


# ============================================================
# 主流程
# ============================================================

async def run() -> dict:
    today = today_tw()
    base = {"stage": 1, "target_date": today, "hub": None, "articles": []}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=USER_AGENT,
            locale="zh-TW",
            timezone_id="Asia/Taipei",
            viewport={"width": 1440, "height": 900},
        )
        page = await context.new_page()

        try:
            hub, latest = None, "無"
            for attempt in range(1, HUB_WAIT_ATTEMPTS + 1):
                hub, seen_hubs = await find_today_hub(page, today)
                if hub:
                    break
                latest = max((h["target_date"] or "" for h in seen_hubs), default="") or "無"
                log.warning("第 %d 次檢查：尚未找到今天（%s）的盤前，頁面上最新為 %s",
                            attempt, today, latest)
                if attempt < HUB_WAIT_ATTEMPTS:
                    log.info("%d 分鐘後重新檢查", HUB_WAIT_MINUTES)
                    await asyncio.sleep(HUB_WAIT_MINUTES * 60)

            if not hub:
                return {**base, "status": NO_DATA,
                        "reason": f"今天（{today}）未找到盤前 Hub，頁面上最新為 {latest}。"
                                  "可能是休市日，或今天的盤前尚未發布。"}

            links = await get_article_links(page, hub)
            if not links:
                return {**base, "hub": hub, "status": FAILED, "reason": "盤前 Hub 內沒有找到任何文章連結"}

            await polite_pause()
            articles = await scrape_all_articles(page, links)
        finally:
            await browser.close()

    ok = sum(1 for a in articles if a["scrape_status"] == "success")
    status = SUCCESS if ok == len(articles) else PARTIAL if ok else FAILED
    return {
        **base,
        "hub": hub,
        "status": status,
        "reason": None if status == SUCCESS else f"{len(articles) - ok} / {len(articles)} 篇文章抓取失敗",
        "article_count": len(articles),
        "scrape_success_count": ok,
        "articles": articles,
    }


def main() -> None:
    try:
        result = asyncio.run(run())
    except BlockedError as e:
        log.error("被網站阻擋：%s", e)
        result = {"stage": 1, "target_date": today_tw(), "hub": None, "articles": [],
                  "status": FAILED, "reason": f"網站阻擋：{e}"}
    except Exception as e:
        log.exception("第一階段發生未預期錯誤")
        result = {"stage": 1, "target_date": today_tw(), "hub": None, "articles": [],
                  "status": FAILED, "reason": f"{type(e).__name__}: {e}"}

    write_json(STAGE1_FILE, result)
    log.info("第一階段結束：status=%s｜%s", result["status"], result.get("reason") or "全部成功")


if __name__ == "__main__":
    main()
