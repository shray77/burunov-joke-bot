#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Сбор МАКСИМАЛЬНОГО советского корпуса анекдотов.

Стратегия: только советский период (1917-1991).
Источники:
1. Anna's Archive — журнал «Крокодил» ВСЕ годы 1950-1991
2. Anna's Archive — Измозик «Политический анекдот» (все издания 1993+)
3. Anna's Archive — Штурман & Тиктин (1985+)
4. Anna's Archive — советская классика (Зощенко, Хармс, Ильф-Петров)
5. lib.ru/ANEKDOTY — фильтр советской тематики (Штирлиц, Брежнев, Чапаев,
   Вовочка старого типа, митьки, КГБ, партия)
6. anekdot.ru 1996-1999 — фильтр советской тематики
   (там ещё живы анекдоты советского происхождения)

Сохраняет:
- download/soviet_corpus_aa.json  — книги/выпуски из Anna's Archive
- download/soviet_corpus_libru.json — тексты из lib.ru (только советские)
- download/soviet_corpus_anekdotru.json — анекдоты с anekdot.ru (только советские)
- download/soviet_corpus_full.md  — сводный отчёт
"""

from __future__ import annotations

import asyncio
import gc
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup

BASE_AA = "https://ru.annas-archive.gl"
BASE_LIBRU = "http://lib.ru"
BASE_ANEKDOT = "https://www.anekdot.ru"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

OUT_DIR = Path("/home/z/my-project/download")
OUT_DIR.mkdir(parents=True, exist_ok=True)

MD5_RE = re.compile(r"/md5/([a-f0-9]{32})")

# ─── Советский период: 1917-1991 ──────────────────────────────────────────
SOVIET_YEAR_RANGE = (1917, 1991)

# ─── Маркеры советской тематики (в названии/тексте) ──────────────────────
SOVIET_KEYWORDS = (
    # Персонажи советских анекдотов
    "штирлиц", "мюллер", "борман", "шлагбаум",
    "чапаев", "петька", "анна",
    "вовочка",  # двойной фильтр ниже — нужна сов. атрибутика
    "рабинович", "моня", "сара",
    "брежнев", "горбачёв", "горбачев", "андропов", "черненко",
    "ленин", "сталин", "троцкий", "свердлов", "дзержинский",
    "хрущёв", "хрущев", "маленков",
    "каганович", "берия", "жуков",
    "гагарин", "терешкова",
    # Советская реалия
    "колхоз", "союз", "цк", "политбюро", "парторг", "комсомол", "пионер",
    "вечный", "дефицит", "очередь за", "совзнак", "рубл", "копеек",
    "мавзолей", "красная площадь", "смольный", "кремль",
    "гэбэшник", "кагэбэшник", "кгб", "чк", "нквд", "гулаг", "лагерь",
    "путёвка", "профсоюз", "зарплата", "получка",
    "блат", "по блату", "достать", "достал",
    "пятилетк", "социалистическое", "капиталист",
    "интурист", "турист", "загран",
    "самиздат", "тамиздат", "радио свобода",
    # Эпохи
    "перестройк", "гласность", "ускорение",
    "застой", "оттепель",
    "нэп", "военный коммунизм",
    # Герои и символы
    "павлик морозов", "зорге", "николай островский",
    # Специфично советские темы
    "сухой закон", "антиалкогольн",
    "чернобыль", "спутак", "спутник",
)

# ─── Постоветские маркеры (выкидываем) ──────────────────────────────────
POST_SOVIET_KEYWORDS = (
    "новый русский", "новые русские",
    "путин", "медведев", "ельцин", "горбачёв в 96",
    "мобилизация", "мобилизован", "сво",
    "ковид", "коронавирус", "пандемия",
    "интернет", "сайт", "сайт.ру", ".com", ".ru", "youtube", "вконтакте",
    "смартфон", "айфон", "iphone", "телефон samsung",
    "криптовалют", "биткоин",
    "зеленский", "зеленский",
    "донбасс", "днр", "лнр",
    "нано-", "гаджеты",
    "youtube", "telegram", "instagram",
    "маск", "навальный",
    "ютуб", "телеграм", "тинькофф",
)


def is_soviet_content(text: str) -> bool:
    """Возвращает True если текст содержит сов. маркеры и НЕ содержит постсоветских."""
    if not text:
        return False
    blob = text.lower()
    if any(k in blob for k in POST_SOVIET_KEYWORDS):
        return False
    return any(k in blob for k in SOVIET_KEYWORDS)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Anna's Archive — поиск по запросам, сбор MD5
# ---------------------------------------------------------------------------

@dataclass
class AABook:
    md5: str
    title: str = ""
    author: str = ""
    year: str = ""
    language: str = ""
    format: str = ""
    size: str = ""
    detail_url: str = ""
    raw_snippet: str = ""
    query: str = ""


async def fetch(client: httpx.AsyncClient, url: str, attempt: int = 0) -> str | None:
    try:
        r = await client.get(url)
        if r.status_code == 200 and len(r.text) > 5000:
            return r.text
        if r.status_code in (429, 503) and attempt < 3:
            await asyncio.sleep(2 ** attempt)
            return await fetch(client, url, attempt + 1)
    except (httpx.HTTPError, asyncio.TimeoutError):
        if attempt < 3:
            await asyncio.sleep(2 ** attempt)
            return await fetch(client, url, attempt + 1)
    return None


async def scrape_aa_search(queries: list[str], max_pages: int = 5) -> list[AABook]:
    """Параллельно ищет все запросы, возвращает список книг."""
    async with httpx.AsyncClient(http2=True, follow_redirects=True,
                                  timeout=30.0, headers=HEADERS) as client:
        all_books: dict[str, AABook] = {}
        sem = asyncio.Semaphore(6)

        async def do_query(q: str) -> None:
            async with sem:
                url = f"{BASE_AA}/search?q={quote(q)}"
                log(f"  AA query: {q!r}")
                for page in range(1, max_pages + 1):
                    html = await fetch(client, f"{url}&page={page}")
                    if not html:
                        break
                    soup = BeautifulSoup(html, "lxml")
                    page_books = 0
                    for div in soup.find_all("div", class_="flex"):
                        cls = div.get("class") or []
                        if not ("pt-3" in cls and "pb-3" in cls and "border-b" in cls):
                            continue
                        a = div.find("a", href=MD5_RE)
                        if not a:
                            continue
                        md5 = MD5_RE.search(a["href"]).group(1)
                        if md5 in all_books:
                            continue
                        row_text = div.get_text(" | ", strip=True)
                        row_text = re.split(r"\|\s*Save\s*\|", row_text)[0]
                        row_text = re.sub(r"\s+", " ", row_text).strip()[:500]
                        # Извлекаем год
                        year = ""
                        for m in re.finditer(r"\b(19\d\d|20[0-2]\d)\b", row_text):
                            y = int(m.group(1))
                            if 1917 <= y <= 1995:  # для советских и ранних постсоветских сборников
                                year = str(y)
                                break
                        # Название и автор — сегменты после пути файла
                        title = ""
                        author = ""
                        meta_str = ""
                        if "|" in row_text:
                            parts = [p.strip() for p in row_text.split("|") if p.strip()]
                            for i, p in enumerate(parts):
                                if "·" in p and re.search(r"\[[a-z]{2}\]", p):
                                    meta_str = p
                                    if i >= 2:
                                        title = parts[i-2][:200]
                                        author = parts[i-1][:120]
                                    elif i == 1:
                                        title = parts[i-1][:200]
                                    break
                        # Формат/размер
                        fmt, size = "", ""
                        if meta_str:
                            segs = [s.strip() for s in meta_str.split("·") if s.strip()]
                            if len(segs) > 1:
                                fmt = segs[1][:20]
                            for s in segs[2:]:
                                if re.match(r"^[\d.,]+\s*[KMGkmg]?[Bb]$", s):
                                    size = s
                                    break
                        all_books[md5] = AABook(
                            md5=md5,
                            title=title, author=author, year=year,
                            format=fmt, size=size,
                            detail_url=f"{BASE_AA}/md5/{md5}",
                            raw_snippet=row_text, query=q,
                        )
                        page_books += 1
                    if page_books == 0:
                        break
                    log(f"    page {page}: +{page_books} (всего {len(all_books)})")

        await asyncio.gather(*(do_query(q) for q in queries))
    return list(all_books.values())


# ---------------------------------------------------------------------------
# lib.ru/ANEKDOTY — фильтр под советскую тематику
# ---------------------------------------------------------------------------

@dataclass
class LibRuSoviet:
    url: str
    title: str = ""
    text: str = ""
    soviet_markers: list[str] = field(default_factory=list)


def split_libru_text(raw: str) -> list[str]:
    """Разбивает длинный текст на отдельные фрагменты (потенциальные анекдоты)."""
    chunks = re.split(r"\n\s*\*\s*\*\s*\*\s*\n|\n-{50,}\n", raw)
    pieces: list[str] = []
    for chunk in chunks:
        for p in re.split(r"\n\s*\n", chunk):
            p = p.strip()
            if 30 < len(p) < 2500:
                pieces.append(p)
    return pieces


def clean_libru(raw: str) -> str:
    """Чистит служебные метки lib.ru."""
    menus = {"Fine HTML", "Printed version", "txt(Word,КПК)", "Lib.ru html",
             "Fb2.zip", "Epub", "Содержание", "Original", "Encoded", "Rus", "Eng"}
    lines = []
    for line in raw.split("\n"):
        s = line.strip()
        if s in menus or s.startswith(("Lib.ru", "Fine HTML", "URL:")):
            continue
        if "---------------------------------------------" in s:
            continue
        lines.append(line)
    return "\n".join(lines)


async def scrape_libru_soviet() -> list[LibRuSoviet]:
    """Собирает .txt с lib.ru/ANEKDOTY, фильтрует под советскую тематику."""
    log("\n=== lib.ru/ANEKDOTY (советский фильтр) ===")
    async with httpx.AsyncClient(http2=True, follow_redirects=True,
                                  timeout=30.0, headers=HEADERS) as client:
        html = await fetch(client, f"{BASE_LIBRU}/ANEKDOTY/")
        if not html:
            return []
        soup = BeautifulSoup(html, "lxml")
        links = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not href.endswith(".txt") or href.startswith("../"):
                continue
            url = f"{BASE_LIBRU}/ANEKDOTY/{href}"
            title = a.get_text(" ", strip=True) or href
            links.append((url, title))
        log(f"  Найдено .txt файлов: {len(links)}")

        sem = asyncio.Semaphore(2)
        results: list[LibRuSoviet] = []
        progress = {"done": 0}
        lock = asyncio.Lock()

        async def fetch_one(url: str, title: str) -> None:
            async with sem:
                text = await fetch(client, url)
                async with lock:
                    progress["done"] += 1
                    if progress["done"] % 10 == 0:
                        log(f"  lib.ru {progress['done']}/{len(links)}")
                if not text:
                    return
                if "<html" in text.lower():
                    soup2 = BeautifulSoup(text, "lxml")
                    raw = soup2.get_text("\n", strip=True)
                    del soup2
                else:
                    raw = text
                raw = clean_libru(raw)
                # разбиваем на анекдоты и фильтруем каждый отдельно
                pieces_added = 0
                for piece in split_libru_text(raw):
                    piece_clean = re.sub(r"\s+", " ", piece).strip()
                    if not (30 < len(piece_clean) < 2000):
                        continue
                    if is_soviet_content(piece_clean):
                        markers = [k for k in SOVIET_KEYWORDS if k in piece_clean.lower()][:5]
                        results.append(LibRuSoviet(
                            url=url, title=title,
                            text=piece_clean, soviet_markers=markers,
                        ))
                        pieces_added += 1
                # освобождаем память
                del raw, text
                if progress["done"] % 30 == 0:
                    gc.collect()

        await asyncio.gather(*(fetch_one(u, t) for u, t in links))
    log(f"  OK lib.ru советских анекдотов: {len(results)}")
    return results


# ---------------------------------------------------------------------------
# anekdot.ru 1996-1999 — фильтр советской тематики
# ---------------------------------------------------------------------------

@dataclass
class AnekdotRuSoviet:
    date: str
    year: str
    text: str
    soviet_markers: list[str] = field(default_factory=list)


async def scrape_anekdotru_soviet() -> list[AnekdotRuSoviet]:
    """Собирает анекдоты с anekdot.ru 1996-1999, фильтрует под советскую тематику."""
    log("\n=== anekdot.ru 1996-1999 (советский фильтр) ===")
    async with httpx.AsyncClient(http2=True, follow_redirects=True,
                                  timeout=30.0, headers=HEADERS) as client:
        urls = []
        for yy in range(96, 100):  # 1996-1999 (2000 уже постсоветский)
            for mm in range(1, 13):
                urls.append((f"{BASE_ANEKDOT}/an/an{yy:02d}{mm:02d}/j500a.html",
                             f"19{yy}", f"{mm:02d}"))
        log(f"  Запланировано месяцев: {len(urls)}")

        sem = asyncio.Semaphore(4)
        all_entries: list[AnekdotRuSoviet] = []
        progress = {"done": 0}
        lock = asyncio.Lock()

        async def fetch_month(url: str, year: str, month: str) -> None:
            async with sem:
                html = await fetch(client, url)
                await asyncio.sleep(0.3)
                async with lock:
                    progress["done"] += 1
                    if progress["done"] % 12 == 0:
                        log(f"  anekdot.ru {progress['done']}/{len(urls)}")
                if not html or "В этом периоде не было" in html:
                    return
                soup = BeautifulSoup(html, "lxml")
                for d in soup.find_all("div", id=True):
                    if not d.get("id", "").isdigit():
                        continue
                    text = d.get_text(" ", strip=True)
                    m = re.match(
                        r"(\d{2}\.\d{2}\.\d{4}),\s*[^-]+-\s*[^$]+?\s*(.+)",
                        text, re.DOTALL,
                    )
                    if m:
                        date_str = m.group(1)
                        body = m.group(2).strip()
                        try:
                            dd, mm_, yyyy = date_str.split(".")
                            iso_date = f"{yyyy}-{mm_}-{dd}"
                        except Exception:
                            iso_date = date_str
                    else:
                        body = text
                        iso_date = ""
                    body = re.sub(r"^сновной выпуск\s*", "", body)
                    body = re.sub(r"^основной выпуск\s*", "", body)
                    if len(body) < 30 or len(body) > 2000:
                        continue
                    if is_soviet_content(body):
                        markers = [k for k in SOVIET_KEYWORDS if k in body.lower()][:5]
                        all_entries.append(AnekdotRuSoviet(
                            date=iso_date, year=year, text=body,
                            soviet_markers=markers,
                        ))

        await asyncio.gather(*(fetch_month(u, y, m) for u, y, m in urls))
    log(f"  OK anekdot.ru советских анекдотов: {len(all_entries)}")
    return all_entries


# ---------------------------------------------------------------------------
# Запись
# ---------------------------------------------------------------------------

def write_outputs(aa_books: list[AABook],
                  libru: list[LibRuSoviet],
                  anekdotru: list[AnekdotRuSoviet]) -> None:
    # JSON
    (OUT_DIR / "soviet_corpus_aa.json").write_text(
        json.dumps([asdict(b) for b in aa_books], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (OUT_DIR / "soviet_corpus_libru.json").write_text(
        json.dumps([asdict(b) for b in libru], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (OUT_DIR / "soviet_corpus_anekdotru.json").write_text(
        json.dumps([asdict(b) for b in anekdotru], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Сводный Markdown
    md = OUT_DIR / "soviet_corpus_full.md"
    lines = ["# Советский корпус анекдотов (1917-1991)\n",
             f"_Anna's Archive книг/выпусков_: **{len(aa_books)}**  ",
             f"_lib.ru советских анекдотов_: **{len(libru)}**  ",
             f"_anekdot.ru советских анекдотов_: **{len(anekdotru)}**  ",
             f"_ИТОГО текстов анекдотов_: **{len(libru) + len(anekdotru)}**\n",
             "---\n",
             "## 1. Anna's Archive — книги и выпуски\n",
             "| # | Год | Название | Автор | Формат | Источник | MD5 |",
             "|---|-----|----------|-------|--------|----------|-----|"]
    aa_sorted = sorted(aa_books, key=lambda b: (b.year or "9999", b.title.lower()))
    for i, b in enumerate(aa_sorted[:200], 1):
        title = (b.title or "(без названия)").replace("|", "/")[:80]
        author = (b.author or "—").replace("|", "/")[:40]
        lines.append(f"| {i} | {b.year or '—'} | {title} | {author} | "
                     f"{b.format or '—'} | {b.query} | [`{b.md5[:8]}`]({b.detail_url}) |")
    if len(aa_sorted) > 200:
        lines.append(f"\n_… и ещё {len(aa_sorted) - 200} книг — см. JSON._")

    lines += ["\n---\n",
              "## 2. lib.ru — советские анекдоты\n",
              f"Всего: **{len(libru)}** текстов (с советскими маркерами)\n",
              "### Образцы (первые 30)\n"]
    for i, b in enumerate(libru[:30], 1):
        text = b.text.replace("|", "/").replace("`", "'")[:400]
        markers = ", ".join(b.soviet_markers)
        lines.append(f"**[{i}]** маркеры: _{markers}_")
        lines.append(f"  > {text}")
        lines.append("")

    lines += ["---\n",
              "## 3. anekdot.ru 1996-1999 — советские анекдоты\n",
              f"Всего: **{len(anekdotru)}** текстов\n",
              "### Образцы (первые 30)\n"]
    for i, b in enumerate(anekdotru[:30], 1):
        text = b.text.replace("|", "/").replace("`", "'")[:400]
        markers = ", ".join(b.soviet_markers)
        lines.append(f"**[{b.date}]** маркеры: _{markers}_")
        lines.append(f"  > {text}")
        lines.append("")

    md.write_text("\n".join(lines), encoding="utf-8")
    log(f"\n  -> {md}")
    log(f"  -> {OUT_DIR}/soviet_corpus_aa.json")
    log(f"  -> {OUT_DIR}/soviet_corpus_libru.json")
    log(f"  -> {OUT_DIR}/soviet_corpus_anekdotru.json")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

async def main() -> int:
    log("=" * 70)
    log("Сбор МАКСИМАЛЬНОГО советского корпуса анекдотов")
    log(f"Период: {SOVIET_YEAR_RANGE[0]}-{SOVIET_YEAR_RANGE[1]}")
    log("=" * 70)

    # Режим запуска: можно пропустить отдельные этапы через argv
    args = set(sys.argv[1:])
    skip_aa = "--skip-aa" in args
    skip_libru = "--skip-libru" in args
    skip_anekdotru = "--skip-anekdotru" in args

    aa_books: list[AABook] = []
    libru: list[LibRuSoviet] = []
    anekdotru: list[AnekdotRuSoviet] = []

    # 1. AA
    if not skip_aa:
        aa_queries = [
            "Крокодил журнал 1950", "Крокодил журнал 1960",
            "Крокодил журнал 1970", "Крокодил журнал 1980", "Крокодил журнал 1990",
            "Измозик анекдот", "Измозик политический",
            "Штурман Тиктин", "Штурман советский",
            "советский политический анекдот",
            "Зощенко рассказы", "Хармс анекдоты", "Ильф Петров",
            "Крокодил сатира",
        ]
        log("\n[1/3] Anna's Archive: советские сборники и журналы...")
        aa_books = await scrape_aa_search(aa_queries, max_pages=5)
        log(f"OK AA: {len(aa_books)} книг/выпусков")
        # сохраняем промежуточно
        (OUT_DIR / "soviet_corpus_aa.json").write_text(
            json.dumps([asdict(b) for b in aa_books], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        gc.collect()
    else:
        # пытаемся загрузить существующий
        p = OUT_DIR / "soviet_corpus_aa.json"
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            aa_books = [AABook(**d) for d in data]
            log(f"  AA загружено из кэша: {len(aa_books)} книг")

    # 2. lib.ru
    if not skip_libru:
        log("\n[2/3] lib.ru/ANEKDOTY с советским фильтром...")
        libru = await scrape_libru_soviet()
        (OUT_DIR / "soviet_corpus_libru.json").write_text(
            json.dumps([asdict(b) for b in libru], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        gc.collect()
    else:
        p = OUT_DIR / "soviet_corpus_libru.json"
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            libru = [LibRuSoviet(**d) for d in data]
            log(f"  lib.ru загружено из кэша: {len(libru)} анекдотов")

    # 3. anekdot.ru
    if not skip_anekdotru:
        log("\n[3/3] anekdot.ru 1996-1999 с советским фильтром...")
        anekdotru = await scrape_anekdotru_soviet()
        (OUT_DIR / "soviet_corpus_anekdotru.json").write_text(
            json.dumps([asdict(b) for b in anekdotru], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    else:
        p = OUT_DIR / "soviet_corpus_anekdotru.json"
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            anekdotru = [AnekdotRuSoviet(**d) for d in data]
            log(f"  anekdot.ru загружено из кэша: {len(anekdotru)} анекдотов")

    # 4. Запись сводного MD
    log("\n=== Запись сводного отчёта ===")
    write_outputs(aa_books, libru, anekdotru)

    log("\n=== ИТОГ ===")
    log(f"AA книг: {len(aa_books)}")
    log(f"lib.ru советских анекдотов: {len(libru)}")
    log(f"anekdot.ru советских анекдотов: {len(anekdotru)}")
    log(f"ВСЕГО текстов анекдотов: {len(libru) + len(anekdotru)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
