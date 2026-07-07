#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Скрапер Anna's Archive: поиск сборников советских анекдотов 1986 года.

Стратегия:
1. Несколько поисковых запросов параллельно (сборник анекдотов 1986, анекдоты 1986, ...).
2. Обход всех страниц пагинации (page=N).
3. Сбор /md5/ ссылок с дедупликацией.
4. Параллельная загрузка детальных страниц (asyncio + httpx, concurrency=20).
5. Извлечение метаданных: title, author, language, format, size, year, source, alt-filenames.
6. Фильтр: книга релевантна, если про анекдоты (по названию/автору/имени файла)
   И упоминает 1986 (в году, в названии или имени файла).
7. Запись в Markdown с метаданными + JSON-дамп.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

BASE_URL = "https://ru.annas-archive.gl"
SEARCH_PATH = "/search"

# Поисковые запросы. Anna's Archive ищет по словам в именах файлов и метаданных.
# Расширение: советская тематика + персонажи + политические + перестройка.
QUERIES = [
    # === широкие запросы (контекст, фильтр позже) ===
    "сборник анекдотов",
    "анекдоты ссср",
    "советские анекдоты",
    "русские анекдоты",
    "книга анекдотов",
    # === по годам 1985-1990 ===
    "анекдоты 1985",
    "анекдоты 1986",
    "анекдоты 1987",
    "анекдоты 1988",
    "анекдоты 1989",
    "анекдоты 1990",
    # === советские персонажи и темы ===
    "анекдоты Штирлиц",
    "анекдоты Вовочка",
    "анекдоты Чапаев",
    "анекдоты Петька",
    "анекдоты Брежнев",
    "анекдоты Ленин",
    "анекдоты Сталин",
    "анекдоты Райкин",
    "анекдоты Хазанов",
    "анекдоты Никулин",
    # === политические / самиздат / ФИДО ===
    "политический анекдот",
    "Измозик анекдот",
    "анекдоты ФИДО",
    "анекдоты самиздат",
    "советский фольклор анекдот",
    "перестройка анекдоты",
    "гласность анекдоты",
    "анекдоты чекист",
    "анекдоты КГБ",
    "анекдоты партия",
    # === серия «Анекдоты» (юнкор-пресс, 1991-1993,但还是 советские) ===
    "Анекдоты Серия Ю",
    "Библиотечка анекдотов",
    "Коллекция анекдотов",
    "золотые анекдоты",
    # === журнальные источники (Крокодил, Шмель, Литературная газета) ===
    "Крокодил журнал юмор",
    "Шмель сатира",
    # === забугорные samizdat-компиляции ===
    "soviet joke",
    "soviet anecdote",
    "anekdot soviet",
    "russian joke book",
]

YEAR_FILTER = "1985-1990"   # расширенный диапазон
YEAR_RANGE = (1985, 1990)   # основной фильтр: только эти годы считать релевантными
CONTEXT_YEAR_RANGE = (1984, 1993)  # расширенный контекст: перестройка + ранний постсовет
MAX_PAGES_PER_QUERY = 20        # 50 результатов на страницу = до 1000 на запрос
DETAIL_CONCURRENCY = 25         # увеличенный параллелизм
SEARCH_CONCURRENCY = 8          # больше параллельных поисковых запросов
REQUEST_TIMEOUT = 30.0
OUTPUT_MD = Path("/home/z/my-project/download/anekdoty_sssr_1985-1990.md")
OUTPUT_JSON = Path("/home/z/my-project/download/anekdoty_sssr_1985-1990.json")
OUTPUT_LOG = Path("/home/z/my-project/scripts/scraper.log")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
}

MD5_RE = re.compile(r"/md5/([a-f0-9]{32})")


# ---------------------------------------------------------------------------
# Модель данных
# ---------------------------------------------------------------------------

@dataclass
class Book:
    md5: str = ""
    title: str = ""
    author: str = ""
    publisher: str = ""
    year: str = ""
    language: str = ""
    format: str = ""
    size: str = ""
    category: str = ""
    source: str = ""
    detail_url: str = ""
    alt_filenames: list[str] = field(default_factory=list)
    raw_snippet: str = ""


# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with OUTPUT_LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---------------------------------------------------------------------------
# Парсинг строки метаданных
# ---------------------------------------------------------------------------

def _find_alt_field(soup, label: str) -> str:
    """Ищет structured-поле «Альтернативное название», «Альтернативный автор»,
    «Альтернативный издатель» — значение лежит в следующем div'е."""
    for el in soup.find_all(string=re.compile(rf"^{re.escape(label)}$")):
        # Соседний div справа
        sib = el.parent.find_next_sibling("div")
        if sib:
            return sib.get_text(" ", strip=True)[:300]
        # Если не нашли, попробуем родителя
        parent = el.parent.parent
        if parent:
            for d in parent.find_all("div", recursive=False):
                t = d.get_text(" ", strip=True)
                if t and t != label:
                    return t[:300]
    return ""


def _parse_meta_segments(meta_str: str) -> dict[str, str]:
    """
    Парсит строку вида:
      'русский [ru] · EPUB · 0.9MB · 1986 · 📕 Книга (Художественная Литература) · 🚀/lgli/zlib ·'
    Возвращает {language, format, size, year, category, source}.
    Год — опциональный сегмент (есть не всегда).
    """
    out = {"language": "", "format": "", "size": "", "year": "", "category": "", "source": ""}
    segs = [s.strip() for s in meta_str.split("·") if s.strip()]
    if not segs:
        return out
    # 0: language [code]
    m = re.match(r"(\S+)\s*\[(\S+)\]", segs[0])
    if m:
        out["language"] = f"{m.group(1)} [{m.group(2)}]"
    else:
        out["language"] = segs[0][:40]
    # 1: format (PDF/EPUB/DJVU/FB2/MOBI/...)
    if len(segs) > 1:
        out["format"] = segs[1][:20]
    # Размер — XX.Y[bB] / XX[kKmMgG][bB]
    for s in segs[2:]:
        if re.match(r"^[\d.,]+\s*[KMGkmg]?[Bb]$", s):
            out["size"] = s
            break
    # Год (4 цифры 19xx-20xx)
    for s in segs[2:]:
        if re.match(r"^(19[5-9]\d|20[0-2]\d)$", s):
            out["year"] = s
            break
    # Категория — сегмент с эмодзи 📕/📘/📰/📚/🗂️/📐
    for s in segs[2:]:
        if any(e in s for e in ("📕", "📘", "📰", "📚", "🗂", "📐", "📦", "📄")):
            out["category"] = re.sub(r"^\S+\s+", "", s).strip()[:120]
            break
    # Источник — сегмент с 🚀/🐢/⚡/📂
    for s in segs:
        if any(e in s for e in ("🚀", "🐢", "⚡", "📂")):
            out["source"] = s.lstrip("🚀🐢⚡📂 ").strip()[:60]
            break
    return out


# ---------------------------------------------------------------------------
# Поиск: обход страниц
# ---------------------------------------------------------------------------

async def fetch_page(client: httpx.AsyncClient, url: str, attempt: int = 0) -> str | None:
    """GET с повторами при сетевых ошибках / 5xx / DDoS-Guard challenges."""
    try:
        r = await client.get(url)
        if r.status_code == 200 and len(r.text) > 5000:
            return r.text
        if r.status_code in (429, 503) and attempt < 4:
            await asyncio.sleep(2 ** attempt)
            return await fetch_page(client, url, attempt + 1)
        log(f"  ! {url[:90]} -> HTTP {r.status_code}, len={len(r.text)}")
    except (httpx.HTTPError, asyncio.TimeoutError) as e:
        if attempt < 4:
            await asyncio.sleep(2 ** attempt)
            return await fetch_page(client, url, attempt + 1)
        log(f"  ! {url[:90]} -> ERROR {e!r}")
    return None


def _extract_row_text(container) -> str:
    """Берём текст строки поиска без 'Save | base score: ... | final score: ...'."""
    txt = container.get_text(" | ", strip=True)
    txt = re.split(r"\|\s*Save\s*\|", txt)[0]
    return re.sub(r"\s+", " ", txt).strip()


def parse_search_page(html: str) -> tuple[set[str], list[Book], int, int]:
    """
    Возвращает (множество md5, список Book-каркасов, total, page_total).
    Структура Anna's Archive: каждая строка результата обёрнута в
    <div class="flex pt-3 pb-3 border-b ..."> с одним /md5/XXX внутри.
    """
    soup = BeautifulSoup(html, "lxml")
    md5s: set[str] = set()
    skeletons: list[Book] = []

    # Текст пагинации: «Результаты 1–50 (216 всего)»
    text = soup.get_text(" ", strip=True)
    m = re.search(r"Результаты\s+\d+[–-]\d+\s+\((\d+)\s+всего\)", text)
    total = int(m.group(1)) if m else 0

    # Контейнер строки: <div class="flex pt-3 pb-3 border-b ...">
    seen_md5: set[str] = set()
    for div in soup.find_all("div", class_="flex"):
        cls = div.get("class") or []
        if not ("pt-3" in cls and "pb-3" in cls and "border-b" in cls):
            continue
        a = div.find("a", href=MD5_RE)
        if not a:
            continue
        md5 = MD5_RE.search(a["href"]).group(1)
        md5s.add(md5)
        if md5 in seen_md5:
            continue
        seen_md5.add(md5)
        row_text = _extract_row_text(div)
        skeletons.append(Book(
            md5=md5,
            detail_url=f"{BASE_URL}/md5/{md5}",
            raw_snippet=row_text[:600],
        ))

    # Сколько страниц в пагинации
    page_total = 0
    for a in soup.find_all("a", href=True):
        m = re.search(r"page=(\d+)", a["href"])
        if m:
            page_total = max(page_total, int(m.group(1)))
    if total:
        page_total = max(page_total, (total + 49) // 50)

    return md5s, skeletons, total, page_total


# ---------------------------------------------------------------------------
# Парсинг детальной страницы /md5/XXX
# ---------------------------------------------------------------------------

def parse_detail_page(html: str, md5: str, skeleton: Book) -> Book:
    """Заполняет поля Book по HTML детальной страницы Anna's Archive.

    Структура: первый <div class="mb-4"> содержит строку
      'Сообщить о качестве файла | <src>/ | <filepath> | <title> | 🔍 | <author> | <meta>'
    где <meta> = 'lang [code] · FORMAT · SIZE · YEAR? · 📕 Книга · 🚀/source ·'
    """
    soup = BeautifulSoup(html, "lxml")
    book = Book(
        md5=md5,
        detail_url=f"{BASE_URL}/md5/{md5}",
        raw_snippet=skeleton.raw_snippet,
    )

    # 1. Главный блок метаданных — первый div.mb-4
    main_block = soup.find("div", class_="mb-4")
    if main_block:
        txt = main_block.get_text(" | ", strip=True)
        parts = [p.strip() for p in txt.split("|") if p.strip()]
        # Убираем служебные: «Сообщить о качестве файла», «🔍», «lgli/», «zlib/» и т.п.
        clean_parts = []
        for p in parts:
            if p in ("Сообщить о качестве файла", "🔍", "Save", "описание"):
                continue
            if re.match(r"^[a-z]+/$", p):  # префикс источника: lgli/, zlib/, nexusstc/...
                continue
            # Прерываемся на «Альтернативное имя файла» — дальше идут доп. секции
            if "Альтернативное" in p or "комментарии к метаданным" in p:
                break
            clean_parts.append(p)
        # Находим индекс строки с метаданными (содержит «·» и «[xx]»)
        meta_idx = None
        for i, p in enumerate(clean_parts):
            if "·" in p and re.search(r"\[[a-z]{2}\]", p):
                meta_idx = i
                break
        if meta_idx is not None:
            meta_str = clean_parts[meta_idx]
            meta = _parse_meta_segments(meta_str)
            book.language = meta["language"]
            book.format = meta["format"]
            book.size = meta["size"]
            book.category = meta["category"]
            book.source = meta["source"]
            if meta["year"]:
                book.year = meta["year"]
            # Эвристика для title/author/publisher:
            # Если clean_parts[meta_idx - 1] содержит год -> это publisher
            # Тогда author = clean_parts[meta_idx - 2], title = clean_parts[meta_idx - 3]
            # Иначе author = clean_parts[meta_idx - 1], title = clean_parts[meta_idx - 2]
            before_meta = clean_parts[:meta_idx]
            # Уберём filepath-ы (содержат расширение .pdf/.epub/.djvu/... или вид md5)
            content_parts = []
            for p in before_meta:
                if re.search(r"\.(pdf|epub|djvu|fb2|mobi|prc|txt|rtf|cb[rz])$", p, re.I):
                    continue
                if re.match(r"^[a-f0-9]{32}\.\w+$", p):
                    continue
                # Путь файла: содержит / или \
                if re.search(r"[\\/]", p) and re.search(r"\.(pdf|epub|djvu|fb2|mobi|prc|txt|rtf)", p, re.I):
                    continue
                content_parts.append(p)
            # Теперь content_parts — это [title, author?, publisher?]
            # Publisher определяется по наличию года в последнем сегменте
            if len(content_parts) >= 3 and re.search(r"\b(19[5-9]\d|20[0-2]\d)\b", content_parts[-1]):
                book.title = content_parts[-3][:300]
                book.author = content_parts[-2][:200]
                book.publisher = content_parts[-1][:200]
            elif len(content_parts) >= 2:
                book.title = content_parts[-2][:300]
                book.author = content_parts[-1][:200]
            elif len(content_parts) == 1:
                book.title = content_parts[0][:300]

    # 1b. Дополнительные structured-поля: «Альтернативное название», «Альтернативный автор»,
    #     «Альтернативный издатель». Они могут точно заполнить пропущенные поля.
    alt_title = _find_alt_field(soup, "Альтернативное название")
    alt_author = _find_alt_field(soup, "Альтернативный автор")
    alt_publisher = _find_alt_field(soup, "Альтернативный издатель")
    if alt_title and (not book.title or book.title.lower() in {"сборник", "анекдоты", "книга"}):
        book.title = alt_title[:300]
    if alt_author and not book.author:
        book.author = alt_author[:200]
    elif alt_author and book.author and book.author.startswith("[") and "Сост" in book.author:
        # Если текущий автор — «[Сост. ...]», но есть альтернатива — используем её
        book.author = alt_author[:200]
    if alt_publisher and not book.publisher:
        book.publisher = alt_publisher[:200]

    # 2. Альтернативные имена файлов
    alt_names: list[str] = []
    for el in soup.find_all(string=re.compile("Альтернативное имя файла")):
        section = el.parent
        for _ in range(4):
            if section is None:
                break
            section = section.parent
        if section is None:
            continue
        for d in section.find_all("div", recursive=True):
            t = d.get_text(" ", strip=True)
            if ("Альтернативное" in t) or ("Имя файла" in t):
                continue
            # Отсеиваем мусор Anna's Archive: «АА: Поиск...», «Исследователь кодов...»,
            # «копировать скопировано!», «URL:», «Filepath: копировать...»
            if any(skip in t for skip in (
                "АА:", "Исследователь кодов", "копировать скопировано",
                "URL: ", "AA Record ID", "Anna\u2019s Archive record",
                "Anna's Archive record", "Visualizing All ISBNs",
            )):
                continue
            # Имя файла — содержит расширение или путь с годом
            if re.search(r"\.(pdf|epub|djvu|fb2|mobi|prc|txt|rtf|cb[rz])", t, re.I) or \
               re.search(r"[\\/]\d{4}[\\/]", t):
                if 5 < len(t) < 400 and t not in alt_names:
                    alt_names.append(t)
    book.alt_filenames = alt_names[:8]

    # 2b. Если заголовок слишком общий («Сборник», «ACDSee», «Книга», или пустой) —
    #     попробуем взять его из имени файла
    weak_titles = {"", "сборник", "acdsee", "книга", "автор неизвестен", "анекдоты"}
    if book.title.strip().lower() in weak_titles:
        for src in [*book.alt_filenames, book.raw_snippet]:
            # Имя файла часто начинается с пути, берём последнюю часть
            # lgli/ Андрей Чорич - Название.fb2  ->  Андрей Чорич - Название
            m = re.search(r"[/\\]\s*([^/\\]+?\.\w+)\s*$", src)
            if m:
                name = m.group(1)
                # убираем расширение
                name = re.sub(r"\.\w{2,5}$", "", name)
                # убираем идентификаторы вида _123456789
                name = re.sub(r"_\d{6,}", "", name)
                name = re.sub(r"\[\d+\]", "", name).strip()
                if name and name.lower() not in weak_titles and len(name) > 5:
                    book.title = name[:300]
                    break

    # 3. Если год всё ещё не извлечён — пробуем из имени файла/пути/названия
    if not book.year:
        candidates: list[str] = []
        for src in [book.raw_snippet, *book.alt_filenames, book.title]:
            # путь файла с годом как директорией: /1986/ или \1986\
            for m in re.finditer(r"[\\/](19[5-9]\d|20[0-2]\d)[\\/]", src):
                candidates.append(m.group(1))
            # [1986] или (1986)
            for m in re.finditer(r"[\[(](19[5-9]\d|20[0-2]\d)[\])]", src):
                candidates.append(m.group(1))
            # 'YYYY-NN' формат журнала
            for m in re.finditer(r"\b(19[5-9]\d)-(?:0[1-9]|1[0-2])\b", src):
                candidates.append(m.group(1))
        if candidates:
            book.year = Counter(candidates).most_common(1)[0][0]

    return book


# ---------------------------------------------------------------------------
# Главная асинхронная логика
# ---------------------------------------------------------------------------

async def scrape_queries(client: httpx.AsyncClient) -> tuple[dict[str, Book], dict[str, int]]:
    """Обходит все запросы + пагинацию. Возвращает md5 -> Book (каркас) и статистику."""
    all_skeletons: dict[str, Book] = {}
    stats: dict[str, int] = {}
    sem = asyncio.Semaphore(SEARCH_CONCURRENCY)

    async def do_query(q: str) -> None:
        async with sem:
            url = f"{BASE_URL}{SEARCH_PATH}?q={quote(q)}"
            log(f"-> query: {q!r}")
            html = await fetch_page(client, url)
            if not html:
                stats[q] = 0
                return
            md5s, skels, total, page_total = parse_search_page(html)
            log(f"   page 1: {len(md5s)} books, total={total}, pages={page_total}")
            stats[q] = total
            for s in skels:
                all_skeletons[s.md5] = s

            tasks = []
            for p in range(2, min(page_total, MAX_PAGES_PER_QUERY) + 1):
                u = f"{url}&page={p}"
                tasks.append(fetch_page(client, u))
            pages = await asyncio.gather(*tasks)
            for i, h in enumerate(pages, start=2):
                if not h:
                    continue
                md5s_p, skels_p, _, _ = parse_search_page(h)
                log(f"   page {i}: {len(md5s_p)} books")
                for s in skels_p:
                    all_skeletons[s.md5] = s

    await asyncio.gather(*(do_query(q) for q in QUERIES))
    return all_skeletons, stats


async def enrich_details(
    client: httpx.AsyncClient,
    skeletons: dict[str, Book],
) -> list[Book]:
    """Параллельно открывает детальные страницы и достаёт метаданные."""
    sem = asyncio.Semaphore(DETAIL_CONCURRENCY)
    results: list[Book] = []
    progress = {"done": 0, "total": len(skeletons)}
    lock = asyncio.Lock()

    async def do_one(md5: str, skel: Book) -> Book | None:
        async with sem:
            url = f"{BASE_URL}/md5/{md5}"
            html = await fetch_page(client, url)
            book: Book | None = None
            if html:
                book = parse_detail_page(html, md5, skel)
            async with lock:
                progress["done"] += 1
                if progress["done"] % 10 == 0 or progress["done"] == progress["total"]:
                    log(f"   detail {progress['done']}/{progress['total']}")
            return book

    tasks = [do_one(m, s) for m, s in skeletons.items()]
    for coro in asyncio.as_completed(tasks):
        b = await coro
        if b:
            results.append(b)
    return results


# ---------------------------------------------------------------------------
# Фильтрация по 1986 году
# ---------------------------------------------------------------------------

def is_relevant(book: Book) -> bool:
    """Книга релевантна, если про анекдоты И год в диапазоне 1985-1990."""
    primary = [book.title, book.author, " ".join(book.alt_filenames)]
    primary_blob = " \n ".join(primary).lower()

    # 1. Анекдоты?
    anekdot_keywords = ("анекдот", "анекдоты", "анекдотов", "шутк", "joke", "anekdot", "шутки",
                        "satire", "сатир", "фельетон", "humor", "юмор")
    if not any(k in primary_blob for k in anekdot_keywords):
        return False

    # 2. Год в диапазоне 1985-1990?
    if book.year and book.year.isdigit() and YEAR_RANGE[0] <= int(book.year) <= YEAR_RANGE[1]:
        return True
    # явно год из диапазона в названии / имени файла
    for src in [book.title, *book.alt_filenames]:
        for m in re.finditer(r"\b(19[5-9]\d|20[0-2]\d)\b", src):
            y = int(m.group(1))
            if YEAR_RANGE[0] <= y <= YEAR_RANGE[1]:
                return True
    return False


def is_anekdot_book(book: Book) -> bool:
    """Книга про анекдоты (без фильтра по году) — для контекстного раздела."""
    primary = [book.title, book.author, " ".join(book.alt_filenames)]
    primary_blob = " \n ".join(primary).lower()
    anekdot_keywords = ("анекдот", "анекдоты", "анекдотов", "шутк", "joke", "anekdot", "шутки",
                        "satire", "сатир", "фельетон", "humor", "юмор")
    return any(k in primary_blob for k in anekdot_keywords)


def is_context_book(book: Book) -> bool:
    """Книга про анекдоты И год в расширенном диапазоне 1984-1993 — для контекстного раздела."""
    if not is_anekdot_book(book):
        return False
    if book.year and book.year.isdigit() and CONTEXT_YEAR_RANGE[0] <= int(book.year) <= CONTEXT_YEAR_RANGE[1]:
        return True
    for src in [book.title, *book.alt_filenames]:
        for m in re.finditer(r"\b(19[5-9]\d|20[0-2]\d)\b", src):
            y = int(m.group(1))
            if CONTEXT_YEAR_RANGE[0] <= y <= CONTEXT_YEAR_RANGE[1]:
                return True
    return False


# ---------------------------------------------------------------------------
# Запись в Markdown
# ---------------------------------------------------------------------------

def write_markdown(books: list[Book], all_books: list[Book], stats: dict[str, int]) -> None:
    """books — релевантные 1986; all_books — все собранные (для контекста)."""
    OUTPUT_MD.parent.mkdir(parents=True, exist_ok=True)
    books_sorted = sorted(
        books,
        key=lambda b: (b.year or "9999", b.title.lower()),
    )
    lines: list[str] = []
    lines.append("# Сборники советских анекдотов — 1985-1990 (перестройка)\n")
    lines.append(f"_Источник_: [{BASE_URL}]({BASE_URL})  ")
    lines.append(f"_Запросов_: {len(QUERIES)} | _Всего собрано_: {len(all_books)} | "
                 f"_Релевантных 1985-1990_: **{len(books_sorted)}**\n")
    lines.append("## Статистика по запросам\n")
    for q, n in sorted(stats.items(), key=lambda x: -x[1]):
        lines.append(f"- `{q}`: {n} результатов")
    lines.append("")
    lines.append("---\n")

    # Контекстный раздел: историческая справка
    lines.append("## Исторический контекст\n")
    lines.append(
        "Период **1985-1990** — это перестройка, гласность и распад СССР. В это время "
        "политический анекдот выходит из подполья: появляются первые легальные сборники "
        "(Измозик, Хазанов, Никулин), в прессе (\"Крокодил\", \"Литературная газета\", \"Юность\") "
        "печатают фельетоны и подборки читательского юмора, а в ФИДО-сетях и самиздате "
        "формируются огромные коллекции. Все указанные источники бесплатны на "
        "Anna's Archive. Ниже — все найденные релевантные издания 1985-1990 годов, "
        "а также контекстный список (1984-1993)."
    )
    lines.append("")
    lines.append("---\n")

    # Основной раздел: релевантные книги 1985-1990
    lines.append(f"## Издания 1985-1990 годов ({len(books_sorted)})\n")
    if not books_sorted:
        lines.append("_В индексе Anna's Archive не найдено книг, которые были бы и про анекдоты, "
                     "и изданы строго в 1985-1990 годах._\n")
    else:
        for i, b in enumerate(books_sorted, 1):
            title = b.title or "(без названия)"
            lines.append(f"### {i}. [{b.year or '?'}] {title}\n")
            if b.author:
                lines.append(f"- **Автор**: {b.author}")
            if b.publisher:
                lines.append(f"- **Издатель**: {b.publisher}")
            if b.language:
                lines.append(f"- **Язык**: {b.language}")
            if b.format:
                lines.append(f"- **Формат**: {b.format}")
            if b.size:
                lines.append(f"- **Размер**: {b.size}")
            if b.category:
                lines.append(f"- **Категория**: {b.category}")
            if b.source:
                lines.append(f"- **Источник**: {b.source}")
            lines.append(f"- **MD5**: `{b.md5}`")
            lines.append(f"- **Source URL**: <{b.detail_url}>")
            if b.alt_filenames:
                lines.append("- **Альтернативные имена файлов**:")
                for n in b.alt_filenames[:5]:
                    lines.append(f"  - `{n}`")
            if b.raw_snippet:
                short = re.sub(r"\s+", " ", b.raw_snippet)[:240]
                lines.append(f"- **Снапшот поиска**: _{short}_")
            lines.append("")
    lines.append("---\n")

    # Контекст: сборники анекдотов 1984-1993 (расширенный диапазон)
    context_books = [b for b in all_books if is_context_book(b)]
    context_books.sort(key=lambda b: (b.year or "9999", b.title.lower()))
    lines.append(f"## Контекст: сборники анекдотов 1984-1993 ({len(context_books)})\n")
    lines.append("_Эти книги изданы в расширенном диапазоне (перестройка + ранний постсовет) — "
                 "тематически релевантны._")
    lines.append("")
    if context_books:
        lines.append("| # | Год | Название | Автор | Формат | MD5 |")
        lines.append("|---|-----|----------|-------|--------|-----|")
        for i, b in enumerate(context_books[:120], 1):
            title = (b.title or "(без названия)").replace("|", "/")[:80]
            author = (b.author or "—").replace("|", "/")[:40]
            fmt = b.format or "—"
            year = b.year or "—"
            lines.append(f"| {i} | {year} | {title} | {author} | {fmt} | [`{b.md5[:8]}`]({b.detail_url}) |")
        if len(context_books) > 120:
            lines.append(f"\n_… и ещё {len(context_books) - 120} книг — см. полный JSON-дамп._")
    lines.append("")

    # Полный список всех сборников анекдотов (без фильтра по году)
    all_anekdot = [b for b in all_books if is_anekdot_book(b)]
    all_anekdot.sort(key=lambda b: (b.year or "9999", b.title.lower()))
    lines.append(f"## Все сборники анекдотов в индексе ({len(all_anekdot)})\n")
    lines.append("_Полный список всех найденных книг про анекдоты (любой год) — "
                 "полезно для дальнейшей фильтрации._")
    lines.append("")
    if all_anekdot:
        lines.append("| # | Год | Название | Автор | Формат | MD5 |")
        lines.append("|---|-----|----------|-------|--------|-----|")
        for i, b in enumerate(all_anekdot[:200], 1):
            title = (b.title or "(без названия)").replace("|", "/")[:80]
            author = (b.author or "—").replace("|", "/")[:40]
            fmt = b.format or "—"
            year = b.year or "—"
            lines.append(f"| {i} | {year} | {title} | {author} | {fmt} | [`{b.md5[:8]}`]({b.detail_url}) |")
        if len(all_anekdot) > 200:
            lines.append(f"\n_… и ещё {len(all_anekdot) - 200} книг — см. полный JSON-дамп._")
    lines.append("")

    OUTPUT_MD.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

async def main() -> int:
    OUTPUT_LOG.write_text("", encoding="utf-8")
    log("=" * 70)
    log("Anna's Archive — скрапер сборников советских анекдотов 1985-1990 (перестройка)")
    log(f"Queries: {QUERIES}")
    log(f"Output: {OUTPUT_MD}")
    log("=" * 70)

    async with httpx.AsyncClient(
        http2=True,
        follow_redirects=True,
        timeout=REQUEST_TIMEOUT,
        headers=HEADERS,
    ) as client:
        log("\n[1/3] Сбор md5-ссылок со всех страниц поиска...")
        skeletons, stats = await scrape_queries(client)
        log(f"\nOK  Уникальных книг для детализации: {len(skeletons)}")

        log(f"\n[2/3] Загрузка детальных страниц (concurrency={DETAIL_CONCURRENCY})...")
        books = await enrich_details(client, skeletons)
        log(f"OK  Получено метаданных: {len(books)}")

        log("\n[3/3] Фильтрация по диапазону 1985-1990 и релевантности...")
        relevant = [b for b in books if is_relevant(b)]
        log(f"OK  Релевантных: {len(relevant)}")

        all_data = [asdict(b) for b in books]
        OUTPUT_JSON.write_text(json.dumps(all_data, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"OK  Полный дамп: {OUTPUT_JSON}")

        write_markdown(relevant, books, stats)
        log(f"OK  Markdown: {OUTPUT_MD}")

        log("\n=== ИТОГ ===")
        log(f"Запросов: {len(QUERIES)} | Всего книг: {len(books)} | "
            f"Релевантных {YEAR_RANGE[0]}-{YEAR_RANGE[1]}: {len(relevant)}")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
