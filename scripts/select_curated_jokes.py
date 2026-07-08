"""
scripts/select_curated_jokes.py
────────────────────────────────
Расширение пресетов Бурунова с 5 фиксированных тем до ~40 отобранных анекдотов.

Идея: полноценный live-синтез на роботе недоступен (см. preset_audio.py —
XTTS слишком тяжёлый для Jetson, Python 3.8 против требуемого 3.10). Вместо
этого расширяем ЧИСЛО тем, для которых заранее озвучен голос Бурунова: не 5,
а ~40, отобранных из data/jokes_filtered.jsonl по реальным тегам датасета
(не выдуманным). RAG (retriever.py) продолжает искать по смыслу как раньше,
просто теперь целится в этот курируемый набор — у любого найденного анекдота
из него уже будет готовый Бурунов-wav.

Запуск:
  python scripts/select_curated_jokes.py
  python scripts/select_curated_jokes.py --per-topic 6 --out data/curated_jokes.json

Дальше: сгенерировать Бурунов-аудио на каждый текст из data/curated_jokes.json
через colab_xtts_v2.ipynb (тот же пайплайн что уже дал 16 текущих пресетов),
положить wav в data/preset_wav/ и добавить записи в manifest.json.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
IN_PATH = BASE_DIR / "data" / "jokes_filtered.jsonl"
OUT_PATH = BASE_DIR / "data" / "curated_jokes.json"

# Реальные теги из датасета (см. вывод filter_jokes.py + ручная проверка
# частот). Берём узнаваемых персонажей/сюжеты, избегаем тегов-источников
# (lib.ru, anekdot.ru, Крокодил/сатира/фельетон/СССР — это жанр/источник,
# не тема) и политических лидеров (Брежнев/Ленин/Сталин/Горбачёв — не тот
# тон для демо жюри).
TARGET_TAGS = [
    "Вовочка", "Чапаев", "Штирлиц", "Новый русский",
    "Армия", "Одесса", "Рабинович",
]
# Отбрасывали "Менты" и "Школа": ручная проверка показала, что там в
# основном не анекдоты — обрывки глоссариев, "экзаменационные вопросы"
# (не смешно, не то), цитаты про "стреляем без предупреждения" (не тот тон
# для демо). Тег в датасете есть, а нормальных анекдотов под него — нет.

MIN_LEN, MAX_LEN = 80, 350


def slugify(tag: str) -> str:
    table = {
        "Вовочка": "vovochka", "Чапаев": "chapaev", "Штирлиц": "shtirlits",
        "Новый русский": "new_russian", "Армия": "army", "Менты": "menty",
        "Одесса": "odessa", "Рабинович": "rabinovich", "Школа": "shkola",
    }
    return table.get(tag, re.sub(r"\W+", "_", tag.lower()))


def score(text: str) -> float:
    """Выше — лучше кандидат для озвучки: не слишком длинный/короткий,
    похож на анекдот с репликами (легче звучит вслух), без явного мусора."""
    s = 0.0
    n = len(text)
    if MIN_LEN <= n <= MAX_LEN:
        s += 3.0
    elif n < MIN_LEN or n > MAX_LEN * 1.5:
        s -= 2.0
    dialog_markers = text.count(" - ") + text.count(" — ")
    s += min(dialog_markers, 3) * 1.0
    # штраф за цифры/спецсимволы кроме обычной пунктуации (вероятно мусор)
    junk = len(re.findall(r"[0-9#@_/\\<>{}]", text))
    s -= junk * 0.3
    # бонус за вопрос/восклицание — живее звучит
    if "?" in text or "!" in text:
        s += 0.5
    return s


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--in", dest="inp", default=str(IN_PATH))
    p.add_argument("--out", default=str(OUT_PATH))
    p.add_argument("--per-topic", type=int, default=5)
    args = p.parse_args()

    inp = Path(args.inp)
    if not inp.exists():
        print(f"Нет {inp} — сначала прогони scripts/filter_jokes.py")
        return

    by_tag = defaultdict(list)
    with inp.open(encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            tags = d.get("tags") or []
            for t in tags:
                if t in TARGET_TAGS:
                    by_tag[t].append(d)

    curated = []
    seen_prefixes = set()
    for tag in TARGET_TAGS:
        candidates = by_tag.get(tag, [])
        candidates.sort(key=lambda d: score(d["text"]), reverse=True)
        picked = 0
        for d in candidates:
            if picked >= args.per_topic:
                break
            text = d["text"].strip()
            prefix = text[:40]
            if prefix in seen_prefixes:
                continue  # дедуп почти одинаковых анекдотов
            seen_prefixes.add(prefix)
            slug = slugify(tag)
            curated.append({
                "preset_id": f"{slug}_{picked + 1:02d}",
                "topic": tag,
                "source_id": d.get("id"),
                "text": text,
            })
            picked += 1
        print(f"{tag:20s}: доступно {len(candidates):4d}, отобрано {picked}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(curated, f, ensure_ascii=False, indent=2)

    print(f"\nВсего отобрано: {len(curated)} анекдотов -> {out_path}")
    print("Дальше: скорми curated_jokes.json в colab_xtts_v2.ipynb, "
          "сложи готовые wav в data/preset_wav/, допиши manifest.json.")


if __name__ == "__main__":
    main()
