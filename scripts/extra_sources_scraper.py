#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Дополнительные скраперы источников анекдотов:
1. lib.ru/ANEKDOTY/ — текстовый архив Мошкова (257 файлов)
2. anekdot.ru — архив по месяцам 1996-2000 (на момент 1985-1990 там нет, но
   в 1996-2000 много советских/постсоветских анекдотов: Штирлиц, Вовочка, Чапаев)
3. Anna's Archive — журналы «Крокодил» 1985-1990 (поиск по запросу «Крокодил журнал»
   + фильтр по году)

Сохраняет результаты:
- /home/z/my-project/download/libru_anekdoty.json + .md
- /home/z/my-project/download/anekdotru_1996-2000.json + .md
- /home/z/my-project/download/krokodil_1985-1990.json + .md
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import quote, urljoin

import httpx
from bs4 import BeautifulSoup

BASE_LIBRU = "http://lib.ru"
BASE_ANEKDOT = "https://www.anekdot.ru"
BASE_AA = "https://ru.annas-archive.gl"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

OUT_DIR = Path("/home/z/my-project/download")
OUT_DIR.mkdir(parents=True, exist_ok=True)

CONCURRENCY = 15
REQUEST_TIMEOUT = 30.0


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


async def fetch(client: httpx.AsyncClient, url: str, attempt: int = 0) -> str | None:
    try:
        r = await client.get(url)
        if r.status_code == 200:
            return r.text
        if r.status_code in (429, 503) and attempt < 3:
            await asyncio.sleep(2 ** attempt)
            return await fetch(client, url, attempt + 1)
    except (httpx.HTTPError, asyncio.TimeoutError) as e:
        if attempt < 3:
            await asyncio.sleep(2 ** attempt)
            return await fetch(client, url, attempt + 1)
        log(f"  ! {url[:80]} -> {e!r}")
    return None


# ---------------------------------------------------------------------------
# 1. lib.ru/ANEKDOTY
# ---------------------------------------------------------------------------

@dataclass
class LibRuBook:
    url: str
    title: str = ""
    raw_text: str = ""
    char_count: int = 0
    anekdot_count: int = 0   # оценка: число "—" / абзацев
    sample: str = ""


async def scrape_libru() -> list[LibRuBook]:
    """Собирает все .txt файлы из lib.ru/ANEKDOTY/."""
    log("\n=== [1/3] lib.ru/ANEKDOTY ===")
    async with httpx.AsyncClient(http2=True, follow_redirects=True,
                                  timeout=REQUEST_TIMEOUT, headers=HEADERS) as client:
        html = await fetch(client, f"{BASE_LIBRU}/ANEKDOTY/")
        if not html:
            log("  ! не удалось загрузить индекс lib.ru/ANEKDOTY/")
            return []
        soup = BeautifulSoup(html, "lxml")
        links = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not href.endswith(".txt"):
                continue
            # также включаем соседние папки (../UMBEKO, ../SEGAL и т.п.) — пропускаем
            if href.startswith("../"):
                continue
            url = urljoin(f"{BASE_LIBRU}/ANEKDOTY/", href)
            title = a.get_text(" ", strip=True) or href
            links.append((url, title))
        log(f"  Найдено .txt файлов: {len(links)}")

        sem = asyncio.Semaphore(CONCURRENCY)
        results: list[LibRuBook] = []
        progress = {"done": 0, "total": len(links)}
        lock = asyncio.Lock()

        async def fetch_one(url: str, title: str) -> LibRuBook | None:
            async with sem:
                text = await fetch(client, url)
                async with lock:
                    progress["done"] += 1
                    if progress["done"] % 20 == 0 or progress["done"] == progress["total"]:
                        log(f"  lib.ru {progress['done']}/{progress['total']}")
                if not text:
                    return None
                # для txt-файлов важно перекодировать: lib.ru хранит в cp1251/koi8r,
                # но httpx должен автоматически по Content-Type
                # Убираем HTML-обёртку если есть
                if "<html" in text.lower():
                    soup2 = BeautifulSoup(text, "lxml")
                    raw = soup2.get_text("\n", strip=True)
                else:
                    raw = text
                # очищаем типовые lib.ru-маркеры
                raw = re.sub(r"^-{50,}$", "", raw, flags=re.MULTILINE)
                raw = re.sub(r"\n{3,}", "\n\n", raw).strip()
                # считаем "анекдоты" — фрагменты, разделённые пустой строкой или "***"
                paragraphs = [p.strip() for p in re.split(r"\n\s*\n|\*\s*\*\s*\*", raw) if len(p.strip()) > 30]
                # sample — первый короткий абзац
                sample = ""
                for p in paragraphs[:5]:
                    if 50 < len(p) < 500:
                        sample = p[:300]
                        break
                return LibRuBook(
                    url=url, title=title, raw_text=raw,
                    char_count=len(raw),
                    anekdot_count=len(paragraphs),
                    sample=sample,
                )

        tasks = [fetch_one(u, t) for u, t in links]
        for coro in asyncio.as_completed(tasks):
            r = await coro
            if r:
                results.append(r)
    log(f"  OK lib.ru: скачано {len(results)} файлов, "
        f"всего {sum(b.char_count for b in results):,} символов")
    return results


# ---------------------------------------------------------------------------
# 2. anekdot.ru — архив 1996-2000
# ---------------------------------------------------------------------------

@dataclass
class AnekdotRuEntry:
    date: str = ""           # YYYY-MM-DD
    year: str = ""
    month: str = ""
    source_url: str = ""
    text: str = ""


async def scrape_anekdotru() -> list[AnekdotRuEntry]:
    """Собирает анекдоты из ежемесячных выпусков 1996-2000 (формат URL /an/anYYMM/j500a.html)."""
    log("\n=== [2/3] anekdot.ru 1996-2000 ===")
    async with httpx.AsyncClient(http2=True, follow_redirects=True,
                                  timeout=REQUEST_TIMEOUT, headers=HEADERS) as client:
        # URL для каждого месяца 1996-2000: an9601..an0012
        urls: list[tuple[str, str, str]] = []  # (url, year, month)
        for yy in range(96, 101):  # 96..100 (1996..2000)
            year = f"19{yy}" if yy < 100 else "2000"
            for mm in range(1, 13):
                urls.append((
                    f"{BASE_ANEKDOT}/an/an{yy:02d}{mm:02d}/j500a.html",
                    year, f"{mm:02d}"
                ))
        log(f"  Запланировано месяцев: {len(urls)}")

        sem = asyncio.Semaphore(8)  # gentler — сайт с анти-ботом
        all_entries: list[AnekdotRuEntry] = []
        progress = {"done": 0, "total": len(urls)}
        lock = asyncio.Lock()

        async def fetch_month(url: str, year: str, month: str) -> list[AnekdotRuEntry]:
            async with sem:
                html = await fetch(client, url)
                await asyncio.sleep(0.3)  # вежливо к anekdot.ru
                async with lock:
                    progress["done"] += 1
                    if progress["done"] % 6 == 0 or progress["done"] == progress["total"]:
                        log(f"  anekdot.ru {progress['done']}/{progress['total']}")
                if not html:
                    return []
                # проверяем, что это не заглушка
                if "В этом периоде не было" in html:
                    return []
                soup = BeautifulSoup(html, "lxml")
                entries: list[AnekdotRuEntry] = []
                # Анекдоты в <div id="N">...</div>
                for d in soup.find_all("div", id=True):
                    div_id = d.get("id", "")
                    if not div_id.isdigit():
                        continue
                    text = d.get_text(" ", strip=True)
                    # Убираем шапку "DD.MM.YYYY, Свежие анекдоты - основной выпуск"
                    m = re.match(
                        r"(\d{2}\.\d{2}\.\d{4}),\s*[^-]+-\s*[^$]+?\s*(.+)",
                        text, re.DOTALL,
                    )
                    if m:
                        date_str = m.group(1)  # DD.MM.YYYY
                        body = m.group(2).strip()
                        try:
                            dd, mm_, yyyy = date_str.split(".")
                            iso_date = f"{yyyy}-{mm_}-{dd}"
                        except Exception:
                            iso_date = date_str
                    else:
                        body = text
                        iso_date = ""
                    if body and len(body) > 30:
                        entries.append(AnekdotRuEntry(
                            date=iso_date, year=year, month=month,
                            source_url=url, text=body,
                        ))
                return entries

        tasks = [fetch_month(u, y, m) for u, y, m in urls]
        for coro in asyncio.as_completed(tasks):
            entries = await coro
            all_entries.extend(entries)
    log(f"  OK anekdot.ru: собрано {len(all_entries)} анекдотов за 1996-2000")
    return all_entries


# ---------------------------------------------------------------------------
# 3. Anna's Archive — «Крокодил» 1985-1990
# ---------------------------------------------------------------------------

@dataclass
class AAKrokodil:
    md5: str
    title: str = ""
    year: str = ""
    format: str = ""
    size: str = ""
    detail_url: str = ""
    raw_snippet: str = ""


async def scrape_aa_krokodil() -> list[AAKrokodil]:
    """Ищет журналы «Крокодил» 1985-1990 на Anna's Archive."""
    log("\n=== [3/3] Anna's Archive: «Крокодил» 1985-1990 ===")
    queries = ["Крокодил журнал", "Крокодил 1985", "Крокодил 1986",
                "Крокодил 1987", "Крокодил 1988", "Крокодил 1989", "Крокодил 1990"]
    async with httpx.AsyncClient(http2=True, follow_redirects=True,
                                  timeout=REQUEST_TIMEOUT, headers=HEADERS) as client:
        all_md5: dict[str, AAKrokodil] = {}
        for q in queries:
            for page in range(1, 11):
                url = f"{BASE_AA}/search?q={quote(q)}&page={page}"
                html = await fetch(client, url)
                if not html:
                    continue
                soup = BeautifulSoup(html, "lxml")
                rows = soup.find_all("div", class_="flex")
                page_count = 0
                for div in rows:
                    cls = div.get("class") or []
                    if not ("pt-3" in cls and "pb-3" in cls and "border-b" in cls):
                        continue
                    a = div.find("a", href=re.compile(r"/md5/[a-f0-9]{32}"))
                    if not a:
                        continue
                    md5 = re.search(r"/md5/([a-f0-9]{32})", a["href"]).group(1)
                    if md5 in all_md5:
                        continue
                    row_text = div.get_text(" | ", strip=True)
                    row_text = re.split(r"\|\s*Save\s*\|", row_text)[0]
                    row_text = re.sub(r"\s+", " ", row_text).strip()[:500]
                    # фильтр: «Крокодил» + год 1985-1990
                    if "крокодил" not in row_text.lower():
                        continue
                    year = ""
                    for m in re.finditer(r"\b(19[5-9]\d|20[0-2]\d)\b", row_text):
                        y = int(m.group(1))
                        if 1985 <= y <= 1990:
                            year = str(y)
                            break
                    if not year:
                        continue
                    # формат/размер — сегмент после «·»
                    fmt = ""
                    size = ""
                    if "·" in row_text:
                        segs = [s.strip() for s in row_text.split("·") if s.strip()]
                        if len(segs) > 1:
                            fmt = segs[1][:20]
                        for s in segs[2:]:
                            if re.match(r"^[\d.,]+\s*[KMGkmg]?[Bb]$", s):
                                size = s
                                break
                    title_match = re.search(r"Крокодил[^|]*\d{4}", row_text, re.IGNORECASE)
                    title = title_match.group(0).strip()[:120] if title_match else "Крокодил"
                    all_md5[md5] = AAKrokodil(
                        md5=md5, title=title, year=year,
                        format=fmt, size=size,
                        detail_url=f"{BASE_AA}/md5/{md5}",
                        raw_snippet=row_text,
                    )
                    page_count += 1
                log(f"  {q} стр.{page}: +{page_count} (всего {len(all_md5)})")
                if page_count == 0:
                    break  # дальше нет смысла
    log(f"  OK Anna's Archive Крокодил 1985-1990: {len(all_md5)} выпусков")
    return list(all_md5.values())


# ---------------------------------------------------------------------------
# Запись в Markdown
# ---------------------------------------------------------------------------

def write_libru_md(books: list[LibRuBook]) -> None:
    out = OUT_DIR / "libru_anekdoty.md"
    books.sort(key=lambda b: -b.char_count)
    lines = ["# lib.ru/ANEKDOTY — текстовый архив анекдотов\n",
             f"_Источник_: [lib.ru/ANEKDOTY](http://lib.ru/ANEKDOTY/)  ",
             f"_Файлов_: **{len(books)}** | "
             f"_Всего символов_: {sum(b.char_count for b in books):,}\n",
             "## Все файлы\n",
             "| # | Название | Символов | ~Анекдотов | URL |",
             "|---|----------|----------|------------|-----|"]
    for i, b in enumerate(books, 1):
        title = b.title.replace("|", "/")[:80]
        lines.append(f"| {i} | {title} | {b.char_count:,} | ~{b.anekdot_count} | [txt]({b.url}) |")
    lines.append("\n## Образцы текста\n")
    for b in books[:5]:
        lines.append(f"### {b.title}\n")
        lines.append(f"- URL: {b.url}")
        lines.append(f"- Размер: {b.char_count:,} символов, ~{b.anekdot_count} абзацев")
        if b.sample:
            lines.append(f"- Образец:\n```\n{b.sample}\n```")
        lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")
    log(f"  -> {out}")


def write_anekdotru_md(entries: list[AnekdotRuEntry]) -> None:
    out = OUT_DIR / "anekdotru_1996-2000.md"
    # по годам
    by_year: dict[str, list[AnekdotRuEntry]] = {}
    for e in entries:
        by_year.setdefault(e.year, []).append(e)
    lines = ["# anekdot.ru — архив анекдотов 1996-2000\n",
             f"_Источник_: [anekdot.ru](https://www.anekdot.ru/)  ",
             f"_Анекдотов_: **{len(entries)}**\n",
             "## Распределение по годам\n",
             "| Год | Анекдотов |",
             "|-----|-----------|"]
    for y in sorted(by_year.keys()):
        lines.append(f"| {y} | {len(by_year[y])} |")
    lines.append("\n## Образцы анекдотов (по 5 на год)\n")
    for y in sorted(by_year.keys()):
        lines.append(f"### {y}\n")
        for e in by_year[y][:5]:
            text = e.text.replace("`", "'")[:500]
            lines.append(f"- **[{e.date}]** {text}")
            lines.append(f"  - источник: <{e.source_url}>")
        lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")
    log(f"  -> {out}")


def write_krokodil_md(items: list[AAKrokodil]) -> None:
    out = OUT_DIR / "krokodil_1985-1990.md"
    items.sort(key=lambda x: (x.year, x.title))
    lines = ["# Журнал «Крокодил» 1985-1990 — Anna's Archive\n",
             f"_Источник_: [Anna's Archive](https://ru.annas-archive.gl)  ",
             f"_Найдено выпусков_: **{len(items)}**\n",
             "## Все выпуски\n",
             "| # | Год | Название | Формат | Размер | MD5 |",
             "|---|-----|----------|--------|--------|-----|"]
    for i, k in enumerate(items, 1):
        title = k.title.replace("|", "/")[:80]
        lines.append(f"| {i} | {k.year} | {title} | {k.format or '—'} | {k.size or '—'} | "
                     f"[`{k.md5[:8]}`]({k.detail_url}) |")
    lines.append("\n## Образцы снапшотов поиска\n")
    for k in items[:10]:
        lines.append(f"### {k.title}\n")
        lines.append(f"- **Год**: {k.year}")
        lines.append(f"- **MD5**: `{k.md5}`")
        lines.append(f"- **URL**: <{k.detail_url}>")
        lines.append(f"- **Снапшот**: _{k.raw_snippet[:300]}_")
        lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")
    log(f"  -> {out}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

async def main() -> int:
    log("=" * 70)
    log("Дополнительные источники анекдотов: lib.ru + anekdot.ru + Крокодил")
    log("=" * 70)

    # 1. lib.ru
    libru_books = await scrape_libru()
    if libru_books:
        (OUT_DIR / "libru_anekdoty.json").write_text(
            json.dumps([asdict(b) for b in libru_books], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        write_libru_md(libru_books)

    # 2. anekdot.ru 1996-2000
    anekdotru_entries = await scrape_anekdotru()
    if anekdotru_entries:
        (OUT_DIR / "anekdotru_1996-2000.json").write_text(
            json.dumps([asdict(e) for e in anekdotru_entries], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        write_anekdotru_md(anekdotru_entries)

    # 3. Крокодил 1985-1990 с Anna's Archive
    krokodil_items = await scrape_aa_krokodil()
    if krokodil_items:
        (OUT_DIR / "krokodil_1985-1990.json").write_text(
            json.dumps([asdict(k) for k in krokodil_items], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        write_krokodil_md(krokodil_items)

    log("\n=== ИТОГ ===")
    log(f"lib.ru: {len(libru_books)} файлов, "
        f"{sum(b.char_count for b in libru_books):,} символов")
    log(f"anekdot.ru 1996-2000: {len(anekdotru_entries)} анекдотов")
    log(f"Крокодил 1985-1990: {len(krokodil_items)} выпусков")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
