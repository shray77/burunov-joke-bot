"""
scripts/filter_jokes.py
───────────────────────
Вторичная чистка data/jokes_clean.jsonl.

Проблема: в jokes_clean.jsonl много мусора, который не выкинул prepare_jokes.py:
- Стихи (многострочные с переносами)
- Словари акронимов ("ADN - Any day now", "[WISIFIG] - смотришь в книгу")
- Копирайты / email / URL внутри текста
- Куски программного кода (#include, void main, function...)
- Метаданные в начале ("источник: журнал ЭКО")
- Английские фрагменты (>50% латиницы)
- Слишком короткие (<50) или длинные (>1000)
- Нумерованные списки-цитаты ("106. Добро существует...")

Запуск:
    python scripts/filter_jokes.py
    python scripts/filter_jokes.py --in data/jokes_clean.jsonl --out data/jokes_filtered.jsonl
    python scripts/filter_jokes.py --dry-run   # только отчёт, без записи

После фильтрации нужно перестроить chroma_db:
    python build_vector_db.py --rebuild
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path


# ─── Паттерны ────────────────────────────────────────────────────────────

# Жёсткий выкид: текст содержит эти паттерны целиком — мусор
HARD_REJECT_PATTERNS = [
    re.compile(r'\(с\)\s*copyright', re.I),
    re.compile(r'©\s*\d{4}', re.I),
    re.compile(r'#include\s+[<"]', re.I),
    re.compile(r'void\s+main\s*\(', re.I),
    re.compile(r'public\s+class\s+\w+', re.I),
    re.compile(r'def\s+\w+\s*\(.*\)\s*:', re.I),
    re.compile(r'function\s+\w+\s*\(', re.I),
    # Словарь акронимов: 3+ строки вида "ABBR - Full text" подряд
    re.compile(r'(?:[A-Z]{2,8}\s*[-—:]\s*.+\n){3,}'),
    # Словарь терминов: 3+ строки вида "[термин] - описание"
    re.compile(r'(?:\[[^\]]+\]\s*[-—:]\s*.+\n?){3,}'),
]

# Словарное определение в начале: "ТЕРМИН - 1. ... 2. ..." (числовые пункты)
GLOSSARY_DEF_RE = re.compile(
    r'^[А-ЯЁA-Z][А-ЯЁA-Z\s]{2,30}\s*[-—:]\s*(?:\d+\.|1\.)',
)

# Мягкий выкид: если >50% латиницы — это не русский анекдот
def mostly_english(text: str) -> bool:
    latin = sum(1 for c in text if c.isascii() and c.isalpha())
    total_alpha = sum(1 for c in text if c.isalpha())
    if total_alpha < 20:
        return True  # слишком мало букв
    return latin / total_alpha > 0.5

# Стихи: много коротких строк с заглавными
def is_poem(text: str) -> bool:
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    if len(lines) < 5:
        return False
    short = sum(1 for l in lines if len(l) < 80)
    return short / len(lines) > 0.7 and len(lines) >= 6


# Стихи в одну строку: после чистки переносы схлопнуты в пробелы.
# Стратегия:
# - Стих = много заглавных слов в середине (после пробела, не после точки)
#   + мало диалоговых маркеров
#   + есть рифма (3+ буквы окончаний повторяются)
# - Анекдот = обратное: много реплик "- ...", мало заглавных в середине
def is_poem_inline(text: str) -> bool:
    """Детекция стихов, склеенных в одну строку."""
    text = text.strip()
    if len(text) < 50 or len(text) > 500:
        return False

    # 1. Если есть >= 2 диалоговых маркера — это анекдот с репликами
    dialog_markers = text.count(' - ') + text.count(' — ')
    if dialog_markers >= 2:
        return False

    words = text.split()
    if len(words) < 8:
        return False

    # 2. Считаем заглавные слова в середине (не в начале предложения)
    capitalized_mid = 0
    for i, w in enumerate(words[1:], 1):
        if w and w[0].isupper():
            prev = words[i-1] if i > 0 else ''
            if prev and not prev.endswith(('.', '!', '?', ':', ';', '"', ')', ']')):
                capitalized_mid += 1

    # 3. Рифма: последние 3 буквы повторяются
    endings = []
    for w in words:
        w_clean = re.sub(r'[^\wа-яё]', '', w.lower(), flags=re.I)
        if len(w_clean) >= 4:
            endings.append(w_clean[-3:])
    ending_counts = Counter(endings)
    rhymed_3plus = sum(1 for cnt in ending_counts.values() if cnt >= 3)
    rhymed_2plus = sum(1 for cnt in ending_counts.values() if cnt >= 2)

    # 4. Плотность заглавных в середине (нормируем на длину)
    cap_density = capitalized_mid / max(1, len(text) / 100)  # cap на 100 символов

    # Решение:
    # Стих если:
    # - capitalized_mid >= 3 (хотя бы 3 заглавных в середине)
    # - cap_density >= 1.5 (1.5+ заглавных на 100 символов)
    # - dialog_markers < 2
    # - (rhymed_3plus >= 1) OR (rhymed_2plus >= 2) OR (cap_density >= 2.5)
    if capitalized_mid < 3:
        return False
    if cap_density < 1.5:
        return False
    # Если есть рифма — точно стих
    if rhymed_3plus >= 1 or rhymed_2plus >= 2:
        return True
    # Если очень много заглавных в середине — скорее всего стих без явной рифмы
    if cap_density >= 2.5 and dialog_markers == 0:
        return True
    return False

# Обрезка хвоста: копирайт/авторство в конце
TAIL_TRIM_PATTERNS = [
    re.compile(r'\s*\(P\.?S\.?.*?авторств[а-яё]+.*?\)\s*$', re.I | re.S),
    re.compile(r'\s*\(с\)\s*\d{4}.*?$', re.I | re.S),
    re.compile(r'\s*©\s*\d{4}.*?$', re.I | re.S),
    re.compile(r'\s*автор[:\s].*?$', re.I | re.S),
    re.compile(r'\s*from:\s.*?$', re.I | re.S),
    re.compile(r'\s*email:\s.*?$', re.I | re.S),
    re.compile(r'\s*e-mail:\s.*?$', re.I | re.S),
    re.compile(r'\s*www:\s.*?$', re.I | re.S),
    re.compile(r'\s*[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\s*$', re.I),
]

# Обрезка головы: метаданные в начале
HEAD_TRIM_PATTERNS = [
    re.compile(r'^источник:.*?\n', re.I),
    re.compile(r'^взято с.*?\n', re.I),
    re.compile(r'^взят с сайта.*?\n', re.I),
    re.compile(r'^\d+\.\s+', re.M),  # нумерованный список в начале
]


# ─── Главная функция фильтрации ──────────────────────────────────────────

def filter_one(text: str) -> tuple[str | None, str]:
    """Возвращает (cleaned_text | None, reason).
    
    reason = 'ok' если прошло, иначе описание почему выкинуто.
    """
    text = text.strip()
    if not text:
        return None, 'empty'

    # 1. Жёсткие reject'ы
    for pat in HARD_REJECT_PATTERNS:
        if pat.search(text):
            return None, 'hard_reject_pattern'

    # 1.5. Словарное определение в начале ("ТЕРМИН - 1. ... 2. ...")
    if GLOSSARY_DEF_RE.match(text):
        return None, 'glossary_definition'

    # 2. Слишком короткий
    if len(text) < 50:
        return None, 'too_short'

    # 3. Слишком длинный (>1000)
    if len(text) > 1000:
        return None, 'too_long'

    # 4. Стихи
    if is_poem(text):
        return None, 'poem'
    if is_poem_inline(text):
        return None, 'poem_inline'

    # 5. Английский доминирует
    if mostly_english(text):
        return None, 'mostly_english'

    # 6. Обрезаем хвост (копирайты, авторство, email)
    cleaned = text
    for pat in TAIL_TRIM_PATTERNS:
        cleaned = pat.sub('', cleaned).rstrip()

    # 7. Обрезаем голову (метаданные)
    for pat in HEAD_TRIM_PATTERNS:
        cleaned = pat.sub('', cleaned, count=1).lstrip()

    # 8. Снова проверка длины после обрезки
    if len(cleaned) < 50:
        return None, 'too_short_after_trim'
    if len(cleaned) > 1000:
        return None, 'too_long_after_trim'

    # 9. Финальная проверка: не стал ли английским после обрезки хвоста
    if mostly_english(cleaned):
        return None, 'mostly_english_after_trim'

    # 10. Не стал ли стихом после обрезки (стих в начале мог быть)
    if is_poem_inline(cleaned):
        return None, 'poem_inline_after_trim'

    return cleaned, 'ok'


# ─── CLI ─────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--in', dest='inp', default='data/jokes_clean.jsonl')
    p.add_argument('--out', default='data/jokes_filtered.jsonl')
    p.add_argument('--dry-run', action='store_true',
                   help='Только отчёт, без записи файла')
    p.add_argument('--verbose', action='store_true',
                   help='Показывать примеры выкинутого')
    args = p.parse_args()

    inp = Path(args.inp)
    out = Path(args.out)
    if not inp.exists():
        print(f"❌ Файл не найден: {inp}")
        sys.exit(1)

    total = 0
    kept = 0
    rejected = Counter()
    samples = {}  # reason -> [(id, text), ...]

    print(f"=== Фильтрация {inp} ===\n")

    with inp.open(encoding='utf-8') as f:
        records = [json.loads(line) for line in f]
    total_in = len(records)
    print(f"Загружено: {total_in}")

    kept_records = []
    for r in records:
        total += 1
        cleaned, reason = filter_one(r.get('text', ''))
        if cleaned is None:
            rejected[reason] += 1
            if args.verbose:
                samples.setdefault(reason, []).append(
                    (r.get('id'), r.get('text', '')[:200])
                )
            continue
        # Обновляем text + embed_text
        r['text'] = cleaned
        tags = r.get('tags') or []
        r['embed_text'] = f"{', '.join(tags)}. {cleaned}" if tags else cleaned
        # Пересохраняем id по порядку
        r['id'] = kept
        kept_records.append(r)
        kept += 1

    print(f"\nРезультат:")
    print(f"  Всего входных:  {total_in}")
    print(f"  Оставлено:      {kept} ({100*kept/total_in:.1f}%)")
    print(f"  Выкинуто:       {total_in - kept} ({100*(total_in-kept)/total_in:.1f}%)")

    print(f"\nПричины выкидывания:")
    for reason, cnt in rejected.most_common():
        print(f"  {reason:30s}: {cnt:5d} ({100*cnt/total_in:.2f}%)")

    if args.verbose and samples:
        print(f"\n{'='*60}")
        print(f"Примеры выкинутого:")
        print(f"{'='*60}")
        for reason, items in samples.items():
            print(f"\n--- {reason} ---")
            for id_, txt in items[:3]:
                print(f"  [id={id_}]: {txt!r}")

    if args.dry_run:
        print(f"\n[dry-run] Файл не записан.")
        return

    # Записываем
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open('w', encoding='utf-8') as f:
        for r in kept_records:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
    print(f"\nЗаписано: {out} ({kept} записей)")

    # Статистика по источникам
    src_counter = Counter(r['source'] for r in kept_records)
    print(f"\nПо источникам:")
    for s, n in src_counter.most_common():
        print(f"  {s}: {n}")


if __name__ == '__main__':
    main()
