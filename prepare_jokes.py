"""
prepare_jokes.py
────────────────
Берёт сырой JSON от скраппера, чистит, нормализует, кладёт в JSONL.

Ожидаемый формат входа (друг-скраппер должен отдать такое):
[
  {"id": 1, "text": "...", "year": 1986, "tags": ["Штирлиц"]},
  ...
]

Если у него другой формат — поправь load_raw() под него.
"""
import json
import re
from pathlib import Path

import config


def load_raw(path: Path) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(
            f"Не найден {path}. Попроси друга-скраппера положить туда JSON."
        )
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("Ожидался список объектов в JSON.")
    return data


def clean_text(text: str) -> str:
    """Убирает мусор из текста анекдота."""
    if not isinstance(text, str):
        return ""
    # Несколько пробелов → один
    text = re.sub(r"\s+", " ", text)
    # Ведущие/хвостовые пробелы
    text = text.strip()
    # HTML-остатки (если парсил другу попался кривой источник)
    text = re.sub(r"<[^>]+>", "", text)
    # Множественные восклицательные/вопросительные
    text = re.sub(r"!{2,}", "!", text)
    text = re.sub(r"\?{2,}", "?", text)
    return text


def normalize_joke(raw: dict, idx: int) -> dict | None:
    """Приводит анекдот к каноническому виду. None = выкинуть."""
    text = clean_text(raw.get("text", ""))
    if len(text) < 20:
        # Слишком короткий, скорее всего мусор
        return None
    if len(text) > 2000:
        # Слишком длинный — LLM его плохо перескажет
        return None

    tags = raw.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]

    year = raw.get("year", 1986)
    try:
        year = int(year)
    except (TypeError, ValueError):
        year = 1986

    return {
        "id": raw.get("id", idx),
        "text": text,
        "year": year,
        "tags": tags,
        # Поле для эмбеддинга: текст + теги (помогает ретриверу)
        "embed_text": f"{', '.join(tags)}. {text}" if tags else text,
    }


def main():
    raw = load_raw(config.RAW_JOKES_PATH)
    print(f"Загружено сырых анекдотов: {len(raw)}")

    cleaned = []
    dropped = 0
    for idx, item in enumerate(raw):
        norm = normalize_joke(item, idx)
        if norm is None:
            dropped += 1
            continue
        cleaned.append(norm)

    print(f"После чистки: {len(cleaned)} (выкинуто {dropped})")

    # Дедупликация по тексту (скрапперы часто ловят повторы)
    seen = set()
    unique = []
    for j in cleaned:
        key = j["text"][:200].lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(j)
    print(f"После дедупликации: {len(unique)} (убрано {len(cleaned) - len(unique)} дублей)")

    with config.CLEAN_JOKES_PATH.open("w", encoding="utf-8") as f:
        for j in unique:
            f.write(json.dumps(j, ensure_ascii=False) + "\n")

    print(f"Готово → {config.CLEAN_JOKES_PATH}")

    # Краткая статистика по тегам
    from collections import Counter
    tag_counter = Counter()
    for j in unique:
        tag_counter.update(j["tags"])
    print("\nТоп-10 тегов:")
    for tag, cnt in tag_counter.most_common(10):
        print(f"  {tag}: {cnt}")


if __name__ == "__main__":
    main()
