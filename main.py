import asyncio
from datetime import datetime
import re
import gspread
from google.oauth2.service_account import Credentials
from playwright.async_api import async_playwright

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

async def crawl_buyee(keyword: str) -> list[dict]:
    # 1) 초기 검색 페이지 URL
    initial_url = f"https://buyee.jp/mercari/search?keyword={keyword}"

    async with async_playwright() as pw:
        # Stealth headless
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
            ],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/115.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
        )
        await context.add_init_script(
            "() => { Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); }"
        )
        page = await context.new_page()

        # 2) 초기 페이지 로드 & idle 대기
        await page.goto(initial_url, wait_until="networkidle", timeout=60000)

        # 3) iframe src 추출 시도
        iframe_src = None
        try:
            iframe_el = await page.query_selector('iframe[name="search_result_iframe"]')
            iframe_src = await iframe_el.get_attribute("src")
        except:
            pass

        # 4) fallback: 직접 조립한 iframe URL
        if not iframe_src:
            iframe_src = (
                f"https://asf.buyee.jp/mercari"
                f"?keyword={keyword}"
                "&conversionType=Mercari_DirectSearch"
                "&currencyCode=KRW"
                "&myee=0"
                "&languageCode=en"
                "&lang=en"
            )

        # 5) iframe URL 로드
        await page.goto(iframe_src, wait_until="networkidle", timeout=60000)

        # 6) 상품 리스트 스크래핑
        await page.wait_for_selector('a.simple_container__llX1q', timeout=60000)
        links = await page.query_selector_all('a.simple_container__llX1q')

        items = []
        for link in links:
            # SOLD‑out 제외
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
    max_map = {}
    for c, mp in zip(codes, max_raw):
        try:
            max_map[c.strip()] = int(mp.replace(",", "").strip())
        except:
            max_map[c.strip()] = None

    existing_urls = set(list_ws.col_values(5)[1:])
    new_rows = []

    for kw in codes:
        limit = max_map.get(kw)
        print(f"\n=== Crawling Buyee: {kw} (max={'∞' if limit is None else limit}엔) ===")
        results = await crawl_buyee(kw)

        if not results:
            if "" not in existing_urls:
                new_rows.append([
                    kw, "결과 없음", "", "", "", datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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

        print(f"✅ {kw}: accumulated {len(new_rows)} items")

    if new_rows:
        list_ws.insert_rows(new_rows, row=2, value_input_option="USER_ENTERED")
        list_ws.sort((6, "des"))

if __name__ == "__main__":
    asyncio.run(main())
