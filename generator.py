"""
generator.py
────────────
Вызов LLM через Ollama с промптом Бурунова.

Перед запуском:
  1. Установи Ollama: https://ollama.com
  2. ollama pull gemma3:4b   (или как у них Gemma 4 называется)
  3. ollama serve  (или она сама стартует)

Если Ollama недоступен — fallback: берём топ-1 анекдот из retrieval
и оборачиваем его в "буруновский" финальный коммент.
"""
import re
import random

import httpx

import config


# ─── Fallback-стилизация (без LLM) ─────────────────────────────────────

BURUNOV_INTROS = [
    "Ну, слушай...",
    "Так, значит...",
    "Короче, дело было так...",
    "Слышал я такую историю...",
    "Ну, понимаешь...",
    "Значит, так...",
]

BURUNOV_OUTROS = [
    "Ну, ты понял...",
    "Вот так вот...",
    "Ну, бывает...",
    "Хе-хе... ну, дальше сам додумаешь...",
    "Такие дела, дорогой...",
    "Ну, ты сам понимаешь...",
]


def _fallback_burunov(topic: str, jokes: list[dict]) -> str:
    """Если Ollama недоступен — собираем ответ из топ-1 анекдота."""
    if not jokes:
        return (
            "Хм... Не припомню я такого... "
            "Может, помягче тему выберешь, а?"
        )
    top = jokes[0]
    text = top["text"].strip()
    # Чистим кодовские хвосты вида «17» / «1 Вася» (номера оригинала на anekdot.ru)
    text = re.sub(r"\s+\d+\s*$", "", text)
    text = re.sub(r"\s+\d+\s+Вася\s*$", "", text, flags=re.IGNORECASE)
    # Лёгкая небрежность — заменяем некоторые знаки
    text = text.replace("!", "...").replace("!!", "...")
    intro = random.choice(BURUNOV_INTROS)
    outro = random.choice(BURUNOV_OUTROS)
    return f"{intro} {text} {outro}"


# ─── Основной вызов Ollama ─────────────────────────────────────────────

def _ollama_available() -> bool:
    """Быстрая проверка — отвечает ли Ollama."""
    try:
        with httpx.Client(timeout=2.0) as c:
            r = c.get(f"{config.OLLAMA_HOST}/api/tags")
            return r.status_code == 200
    except Exception:
        return False


def generate_burunov(topic: str, context_jokes: list[dict]) -> str:
    """
    Принимает тему + список анекдотов (из retriever).
    Возвращает текст, готовый для TTS.

    Если Ollama недоступен — использует fallback.
    """
    if not context_jokes:
        return (
            "Хм... Не припомню я такого... "
            "Может, помягче тему выберешь, а?"
        )

    # Проверяем Ollama
    if not _ollama_available():
        return _fallback_burunov(topic, context_jokes)

    # Собираем контекст — нумеруем анекдоты, чтобы LLM понимала где какой
    context_lines = []
    for i, j in enumerate(context_jokes, 1):
        tags_str = f" [{', '.join(j['tags'])}]" if j["tags"] else ""
        context_lines.append(f"Анекдот {i}{tags_str}:\n{j['text']}")
    context = "\n\n".join(context_lines)

    user_prompt = config.USER_PROMPT_TEMPLATE.format(
        context=context,
        topic=topic,
    )

    payload = {
        "model": config.OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": config.SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "options": config.OLLAMA_OPTIONS,
    }

    # Timeout побольше — анекдот может генериться 10-20 сек на CPU
    with httpx.Client(timeout=120.0) as client:
        resp = client.post(
            f"{config.OLLAMA_HOST}/api/chat",
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()

    text = data["message"]["content"].strip()
    return text


# ─── Тест из консоли ──────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    from retriever import Retriever

    topic = " ".join(sys.argv[1:]) or "Штирлиц"
    print(f"Тема: {topic}\n")

    hits = Retriever.search(topic, top_k=config.TOP_K)
    print(f"Найдено анекдотов: {len(hits)}\n")
    if not hits:
        print("Ничего не нашли в базе.")
        sys.exit(0)

    if _ollama_available():
        print("🟢 Ollama доступна — генерация через LLM...\n")
    else:
        print("🟡 Ollama недоступна — fallback-стилизация...\n")

    text = generate_burunov(topic, hits)
    print("─" * 60)
    print(text)
    print("─" * 60)
