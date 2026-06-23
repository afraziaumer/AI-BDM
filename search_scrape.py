import os
import json
import time
import requests
from dotenv import load_dotenv

load_dotenv("api.env")

SERPER_API_KEY = os.getenv("serper")
ZENROWS_API_KEY = os.getenv("zenrows")

SERPER_URL = "https://google.serper.dev/search"
ZENROWS_URL = "https://api.zenrows.com/v1/"


def serper_search(query, start_page=1, end_page=2):
    """Search Google via Serper across pages start_page..end_page."""
    urls = []
    headers = {"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"}

    for page in range(start_page, end_page + 1):
        payload = {"q": query, "page": page, "num": 10}
        try:
            resp = requests.post(SERPER_URL, headers=headers, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            print(f"[serper] page {page} failed: {e}")
            continue

        organic = data.get("organic", [])
        if not organic:
            print(f"[serper] page {page}: no more results, stopping")
            break

        for item in organic:
            link = item.get("link")
            if link and link not in urls:
                urls.append(link)

        print(f"[serper] page {page}: {len(organic)} results")
        time.sleep(1)

    return urls


def zenrows_scrape(url, js_render=False, premium_proxy=False):
    """Scrape a single URL through ZenRows."""
    params = {"url": url, "apikey": ZENROWS_API_KEY}
    if js_render:
        params["js_render"] = "true"
    if premium_proxy:
        params["premium_proxy"] = "true"

    try:
        resp = requests.get(ZENROWS_URL, params=params, timeout=60)
        resp.raise_for_status()
        return {"url": url, "status": resp.status_code, "html": resp.text}
    except requests.RequestException as e:
        return {"url": url, "status": None, "error": str(e)}


def run(query, output="results.json", js_render=False):
    print(f"Searching: {query!r}")
    urls = serper_search(query, start_page=1, end_page=2)
    print(f"\nTotal unique URLs: {len(urls)}\n")

    results = []
    for i, url in enumerate(urls, 1):
        print(f"[{i}/{len(urls)}] scraping {url}")
        results.append(zenrows_scrape(url, js_render=js_render))
        time.sleep(1)

    with open(output, "w", encoding="utf-8") as f:
        json.dump(
            {"query": query, "url_count": len(urls), "results": results},
            f,
            ensure_ascii=False,
            indent=2,
        )

    print(f"\nSaved {len(results)} records to {output}")


if __name__ == "__main__":
    import sys

    if not SERPER_API_KEY or not ZENROWS_API_KEY:
        raise SystemExit("Missing API keys. Check api.env (keys: serper, zenrows).")

    query = " ".join(sys.argv[1:]).strip()
    if not query:
        query = input("Enter search query: ").strip()

    run(query, output="results.json", js_render=False)