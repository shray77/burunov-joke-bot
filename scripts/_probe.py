import httpx, sys
url = "https://ru.annas-archive.gl/search?q=%D1%81%D0%B1%D0%BE%D1%80%D0%BD%D0%B8%D0%BA+%D0%B0%D0%BD%D0%B5%D0%BA%D0%B4%D0%BE%D1%82%D0%BE%D0%B2"
headers = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}
with httpx.Client(http2=True, follow_redirects=True, timeout=30.0, headers=headers) as client:
    r = client.get(url)
    print("STATUS:", r.status_code)
    print("LEN:", len(r.text))
    print("HEADERS:", dict(r.headers)[:200] if isinstance(r.headers, str) else {k:v for k,v in list(r.headers.items())[:8]})
    # Save for inspection
    open("/home/z/my-project/scripts/_search.html","w").write(r.text)
    # quick grep
    import re
    for needle in ["/md5/", "сборник", "анекдот", "1986", "page=", "Cloudflare", "cf-"]:
        cnt = r.text.count(needle)
        print(f"  '{needle}': {cnt} matches")
