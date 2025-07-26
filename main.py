import asyncio
import os
import re
from collections import Counter
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials
from playwright.async_api import async_playwright

# — Proxy 설정 제거 (직접 연결 보장)
os.environ.pop('HTTP_PROXY', None)
os.environ.pop('HTTPS_PROXY', None)

# — Google Sheets 설정
SERVICE_ACCOUNT_FILE = 'credentials.json'
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
SPREADSHEET_ID = '1GSro604hDjybH5bhOQ4h_ZtcMCi_QdV-9ZYnMnH5kdo'
CODE_SHEET = 'code'
LIST_SHEET = 'list'


def get_sheets():
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    client = gspread.authorize(creds)
    ss = client.open_by_key(SPREADSHEET_ID)
    return ss.worksheet(CODE_SHEET), ss.worksheet(LIST_SHEET)


async def auto_scroll(page):
    prev_height = await page.evaluate("() => document.body.scrollHeight")
    while True:
        await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(1)
        new_height = await page.evaluate("() => document.body.scrollHeight")
        if new_height == prev_height:
            break
        prev_height = new_height


async def crawl_buyee(keyword: str) -> list[dict]:
    search_url = f"https://buyee.jp/mercari/search?keyword={keyword}"
    # 선택적 residential proxy
    proxy_server = os.getenv("RESIDENTIAL_PROXY")  # 예: "http://user:pass@proxy.example.com:8000"

    async with async_playwright() as pw:
        launch_opts = {
            "headless": True,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
            ],
        }
        if proxy_server:
            launch_opts["proxy"] = {"server": proxy_server}

        browser = await pw.chromium.launch(**launch_opts)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/115.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
        )
        # Headless 탐지 회피
        await context.add_init_script(
            "() => { Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); }"
        )
        page = await context.new_page()

        # 1) 검색 페이지 로드
        await page.goto(search_url, wait_until="networkidle", timeout=60000)

        # 2) iframe src 찾기 또는 fallback
        iframe_el = await page.query_selector('iframe[name="search_result_iframe"]')
        iframe_src = await iframe_el.get_attribute("src") if iframe_el else None
        if not iframe_src:
            iframe_src = (
                f"https://asf.buyee.jp/mercari?keyword={keyword}"
                "&conversionType=Mercari_DirectSearch"
                "&currencyCode=KRW&myee=0&languageCode=en&lang=en"
            )

        # 3) iframe 로드 후 스크롤
        await page.goto(iframe_src, wait_until="networkidle", timeout=60000)
        await auto_scroll(page)

        # 4) CI 디버그
        if os.getenv("CI"):
            content = await page.content()
            print("===== PAGE CONTENT DUMP =====")
            print(content[:1000])
            print("===== END OF DUMP =====")
            await page.screenshot(path="ci-dump.png", full_page=True)

        # 5) 동적 클래스 추출
        html = await page.content()
        classes = re.findall(
            r'<a[^>]+href="[^"]*?/item/[^"]*"[^>]*class="([^"]+)"',
            html
        )
        counter = Counter()
        for cls_str in classes:
            for cls in cls_str.split():
                counter[cls] += 1
        top_class = counter.most_common(1)[0][0] if counter else None
        selector = f'a.{top_class}' if top_class else 'a[href*="/item/"]'

        # 6) 항목 수집
        await page.wait_for_selector(selector, timeout=60000)
        links = await page.query_selector_all(selector)

        items = []
        for link in links:
            # sold-out 제외
            if await link.query_selector("span.sold_text__yvzaS"):
                continue

            title_el = await link.query_selector("span.simple_name__XMcbt")
            price_el = await link.query_selector("span.simple_price__h13DP")
            img_el   = await link.query_selector("img")
            href     = await link.get_attribute("href") or ""

            items.append({
                "code":  keyword,
                "title": (await title_el.inner_text()).strip() if title_el else "",
                "price": (await price_el.inner_text()).strip() if price_el else "",
                "image": await img_el.get_attribute("src") if img_el else "",
                "url":   href if href.startswith("http") else f"https://buyee.jp{href}",
                "date":  datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })

        await browser.close()
        return items


async def main():
    code_ws, list_ws = get_sheets()
    codes   = code_ws.col_values(1)[1:]
    max_raw = code_ws.col_values(2)[1:]

    # 최대 가격 파싱
    max_map = {}
    for code, raw in zip(codes, max_raw):
        clean = raw.replace(",", "").strip()
        try:
            max_map[code.strip()] = int(clean) if clean else None
        except ValueError:
            max_map[code.strip()] = None

    existing_urls = set(list_ws.col_values(5)[1:])
    new_rows = []

    for kw in codes:
        limit = max_map.get(kw)
        print(f"\n=== Crawling Buyee: {kw} (max={'∞' if limit is None else limit}엔) ===")
        results = await crawl_buyee(kw)

        if not results:
            if "" not in existing_urls:
                new_rows.append([
                    kw, "결과 없음", "", "", "",
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                ])
                existing_urls.add("")
        else:
            for it in results:
                price_num = int(re.sub(r"[^\d]", "", it["price"])) if it["price"] else 0
                if limit is not None and price_num > limit:
                    continue
                if it["url"] in existing_urls:
                    continue
                img_formula = f'=IMAGE("{it["image"]}",1)' if it["image"] else ""
                new_rows.append([
                    it["code"], it["title"], it["price"],
                    img_formula, it["url"], it["date"]
                ])
                existing_urls.add(it["url"])

        print(f"✅ {kw}: 누적 {len(new_rows)}개")

    if new_rows:
        list_ws.insert_rows(new_rows, row=2, value_input_option="USER_ENTERED")
        list_ws.sort((6, "des"))


if __name__ == "__main__":
    asyncio.run(main())
