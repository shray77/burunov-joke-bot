"""
prepare_jokes.py
────────────────
Загружает 4 источника анекдотов, чистит, нормализует, кладёт в JSONL.

Источники:
1. download/libru_anekdoty.json         — текстовые файлы с lib.ru (252 файла)
2. download/anekdotru_1996-2000.json    — отдельные анекдоты (4800 шт.)
3. download/krokodil_1985-1990.json     — выпуски журн. «Крокодил» (558 PDF, метаданные)
4. download/anekdoty_sssr_1985-1990.json — книги Anna's Archive (1264 шт., метаданные)

Книги и PDF «Крокодила» не содержат готовых анекдотов в текстовом виде —
они попадают в JSONL как отдельные «документы» с описанием, чтобы ретривер
мог подсказать пользователю: «вот есть книга 1985 года, читай PDF по ссылке».

Сами анекдоты (для RAG-генерации) берутся из lib.ru + anekdot.ru.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import config


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------

LIBRU_MENUS = {
    "Fine HTML", "Printed version", "txt(Word,КПК)", "Lib.ru html",
    "Fb2.zip", "Epub", "Содержание", "Original", "Encoded",
    "Rus", "Eng", "Фб2.zip", "Текст",
}

LIBRU_HEADER_RE = re.compile(
    r"^(?:Парашютистские|Антология|Книга|Сборник|Остер|Шинкарев|Кривин|"
    r"Федотов|Ланцберг|Митьковский|Митьки|Анекдоты|Блох|Мерфи|Хармс|"
    r"Паперный|Раневская|Романов|Сегаль|Филатов|Шамфор|Шмелева|Шмелев|"
    r"Иртеньев|Барский|Измайлов|Эпиграммисты|Афанасьев|Луковкина|"
    r"Чорич|Антология|Колесников|Воронин|Брешко-Брешковский|Болотников|"
    r"Линник|Посвежинный|Андреев|Сонин|Курганов|Штурман|Тиктин|Паперный)",
    re.MULTILINE,
)


def clean_text(text: str) -> str:
    """Убирает мусор из текста анекдота."""
    if not isinstance(text, str):
        return ""
    text = re.sub(r"\s+", " ", text)
    text = text.strip()
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"!{2,}", "!", text)
    text = re.sub(r"\?{2,}", "?", text)
    return text


def split_into_jokes(raw_text: str) -> list[str]:
    """Разбивает длинный текст lib.ru на отдельные анекдоты.

    Разделители: пустая строка или строка из «*» / «-».
    """
    # Сначала режем по «***» и «-------» (50+ дефисов)
    chunks = re.split(r"\n\s*\*\s*\*\s*\*\s*\n|\n-{50,}\n", raw_text)
    # Дальше — по двойному переводу строки
    pieces: list[str] = []
    for chunk in chunks:
        for p in re.split(r"\n\s*\n", chunk):
            p = p.strip()
            if 30 < len(p) < 2500:
                pieces.append(p)
    return pieces


def clean_libru_text(raw: str) -> str:
    """Чистит служебные метки lib.ru (меню форматов, навигацию)."""
    lines = []
    skip_block = False
    for line in raw.split("\n"):
        stripped = line.strip()
        if stripped in LIBRU_MENUS:
            continue
        if stripped.startswith("Lib.ru") or stripped.startswith("Fine HTML"):
            continue
        if stripped.startswith("URL:"):
            continue
        if "---------------------------------------------" in stripped:
            continue
        lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Загрузчики источников
# ---------------------------------------------------------------------------

def load_libru(path: Path) -> list[dict]:
    """Источник 1: lib.ru/ANEKDOTY — текстовые файлы."""
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    out: list[dict] = []
    for book in data:
        raw = book.get("raw_text", "")
        title = book.get("title", "").strip()
        url = book.get("url", "")
        # Чистим служебные метки
        clean = clean_libru_text(raw)
        # Бьём на отдельные анекдоты
        for piece in split_into_jokes(clean):
            piece = clean_text(piece)
            if len(piece) < 30 or len(piece) > 2000:
                continue
            # выкидываем оглавления и технические строки
            if piece.lower().startswith(("содержание", "оглавление", "fb2", "epub")):
                continue
            # авто-теги по заголовку файла
            tags: list[str] = []
            tlow = title.lower()
            if "штирлиц" in tlow: tags.append("Штирлиц")
            if "вовочк" in tlow: tags.append("Вовочка")
            if "чапаев" in tlow or "петьк" in tlow: tags.append("Чапаев")
            if "армейск" in tlow or "военн" in tlow or "погран" in tlow: tags.append("Армия")
            if "милиц" in tlow or "мент" in tlow: tags.append("Менты")
            if "компьют" in tlow or "программ" in tlow: tags.append("Компьютеры")
            if "шкóла" in tlow or "школ" in tlow: tags.append("Школа")
            if "касп" in tlow or "ксп" in tlow: tags.append("КСП")
            if "митьк" in tlow: tags.append("Митьки")
            if "парашют" in tlow: tags.append("Парашютисты")
            if "еврей" in tlow: tags.append("Еврейские")
            if "одесс" in tlow: tags.append("Одесса")
            if "полит" in tlow: tags.append("Политика")
            if "сказк" in tlow: tags.append("Сказки")
            if not tags:
                tags.append("lib.ru")
            out.append({
                "source": "lib.ru",
                "source_url": url,
                "text": piece,
                "year": 1990,   # lib.ru — общее наследие, нет чёткого года
                "tags": tags,
                "title": title,
            })
    return out


def load_anekdotru(path: Path) -> list[dict]:
    """Источник 2: anekdot.ru 1996-2000."""
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    out: list[dict] = []
    for e in data:
        text = clean_text(e.get("text", ""))
        # Срезаем шапку «сновной выпуск» (остаток парсинга)
        text = re.sub(r"^сновной выпуск\s*", "", text)
        text = re.sub(r"^основной выпуск\s*", "", text)
        if len(text) < 30 or len(text) > 2000:
            continue
        year_str = e.get("year", "")
        try:
            year = int(year_str)
        except (TypeError, ValueError):
            year = 1996
        tags: list[str] = ["anekdot.ru"]
        # авто-теги по тексту
        tlow = text.lower()
        if "штирлиц" in tlow or "мюллер" in tlow: tags.append("Штирлиц")
        if "вовочк" in tlow: tags.append("Вовочка")
        if "чапаев" in tlow or "петьк" in tlow: tags.append("Чапаев")
        if "рабинович" in tlow: tags.append("Рабинович")
        if "брежнев" in tlow: tags.append("Брежнев")
        if "лендин" in tlow or "ленина" in tlow: tags.append("Ленин")
        if "сталин" in tlow: tags.append("Сталин")
        if "горбачёв" in tlow or "горбачев" in tlow: tags.append("Горбачёв")
        if "новый русский" in tlow: tags.append("Новый русский")
        if "поручик" in tlow or "ротмистр" in tlow: tags.append("Царская Россия")
        out.append({
            "source": "anekdot.ru",
            "source_url": e.get("source_url", ""),
            "text": text,
            "year": year,
            "tags": tags,
            "date": e.get("date", ""),
        })
    return out


def load_krokodil(path: Path) -> list[dict]:
    """Источник 3: выпуски «Крокодил» 1985-1990 (только метаданные, не тексты)."""
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    out: list[dict] = []
    for k in data:
        year_str = k.get("year", "")
        try:
            year = int(year_str)
        except (TypeError, ValueError):
            year = 1986
        text = (
            f"Журнал «Крокодил» {k.get('title', 'выпуск')} — "
            f"сатирический еженедельник СССР. "
            f"Содержит фельетоны, читательские анекдоты, карикатуры. "
            f"PDF-выпуск доступен по ссылке {k.get('detail_url', '')}"
        )
        out.append({
            "source": "Крокодил",
            "source_url": k.get("detail_url", ""),
            "text": text,
            "year": year,
            "tags": ["Крокодил", "СССР", "сатира", "фельетон"],
            "md5": k.get("md5", ""),
        })
    return out


def load_aa_books(path: Path) -> list[dict]:
    """Источник 4: книги Anna's Archive (только релевантные про анекдоты)."""
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    out: list[dict] = []
    for b in data:
        title = b.get("title", "").strip()
        if not title:
            continue
        # фильтр: только про анекдоты/юмор
        blob = " ".join([title, b.get("author", ""), " ".join(b.get("alt_filenames") or [])]).lower()
        if not any(k in blob for k in ("анекдот", "шутк", "joke", "анекдоты", "сатир", "фельетон", "юмор")):
            continue
        year_str = b.get("year", "")
        try:
            year = int(year_str)
        except (TypeError, ValueError):
            year = 1990
        author = b.get("author", "").strip()[:100]
        publisher = b.get("publisher", "").strip()[:100]
        text = (
            f"Книга: {title}. "
            + (f"Автор: {author}. " if author else "")
            + (f"Издатель: {publisher}. " if publisher else "")
            + f"Год: {year}. "
            + f"Формат: {b.get('format', '?')}. "
            + f"Источник: Anna's Archive, md5={b.get('md5', '')}"
        )
        out.append({
            "source": "Anna's Archive",
            "source_url": b.get("detail_url", ""),
            "text": text,
            "year": year,
            "tags": ["книга", "сборник"],
            "author": author,
            "title": title,
            "md5": b.get("md5", ""),
        })
    return out


# ---------------------------------------------------------------------------
# Главная функция
# ---------------------------------------------------------------------------

def main():
    # Пути к собранным JSON (теперь в download/)
    download_dir = Path(__file__).resolve().parent / "download"
    paths = {
        "lib.ru": download_dir / "libru_anekdoty.json",
        "anekdot.ru": download_dir / "anekdotru_1996-2000.json",
        "Крокодил": download_dir / "krokodil_1985-1990.json",
        "Anna's Archive": download_dir / "anekdoty_sssr_1985-1990.json",
    }

    all_jokes: list[dict] = []
    for name, p in paths.items():
        if not p.exists():
            print(f"  ! {name}: файл не найден {p}")
            continue
        if name == "lib.ru":
            items = load_libru(p)
        elif name == "anekdot.ru":
            items = load_anekdotru(p)
        elif name == "Крокодил":
            items = load_krokodil(p)
        else:
            items = load_aa_books(p)
        print(f"  {name}: {len(items)} записей")
        all_jokes.extend(items)

    print(f"\nВсего сырых записей: {len(all_jokes)}")

    # Нормализация + фильтрация
    cleaned: list[dict] = []
    dropped = 0
    for idx, j in enumerate(all_jokes):
        text = clean_text(j.get("text", ""))
        if len(text) < 20:
            dropped += 1
            continue
        if len(text) > 2000:
            dropped += 1
            continue
        tags = j.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        try:
            year = int(j.get("year", 1986))
        except (TypeError, ValueError):
            year = 1986
        cleaned.append({
            "id": idx,
            "source": j.get("source", ""),
            "source_url": j.get("source_url", ""),
            "text": text,
            "year": year,
            "tags": tags,
            "embed_text": f"{', '.join(tags)}. {text}" if tags else text,
            **({"date": j["date"]} if "date" in j else {}),
            **({"md5": j["md5"]} if "md5" in j else {}),
            **({"title": j["title"]} if "title" in j else {}),
            **({"author": j["author"]} if "author" in j else {}),
        })
    print(f"После чистки: {len(cleaned)} (выкинуто {dropped})")

    # Дедупликация по тексту
    seen = set()
    unique: list[dict] = []
    for j in cleaned:
        key = j["text"][:200].lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(j)
    print(f"После дедупликации: {len(unique)} (убрано {len(cleaned) - len(unique)} дублей)")

    # Сохраняем
    with config.CLEAN_JOKES_PATH.open("w", encoding="utf-8") as f:
        for j in unique:
            f.write(json.dumps(j, ensure_ascii=False) + "\n")
    print(f"\nГотово → {config.CLEAN_JOKES_PATH}")

    # Статистика
    from collections import Counter
    src_counter = Counter(j["source"] for j in unique)
    print("\nПо источникам:")
    for s, n in src_counter.most_common():
        print(f"  {s}: {n}")

    tag_counter = Counter()
    for j in unique:
        tag_counter.update(j["tags"])
    print("\nТоп-15 тегов:")
    for tag, cnt in tag_counter.most_common(15):
        print(f"  {tag}: {cnt}")

    year_counter = Counter()
    for j in unique:
        year_counter[j["year"]] += 1
    print("\nПо годам (топ-10):")
    for y, n in sorted(year_counter.items(), key=lambda x: -x[1])[:10]:
        print(f"  {y}: {n}")


if __name__ == "__main__":
    main()
