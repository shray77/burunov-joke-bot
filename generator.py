"""
generator.py
────────────
Вызов LLM через Ollama с промптом Бурунова.

Перед запуском:
  1. Установи Ollama: https://ollama.com
  2. ollama pull gemma3:4b   (или как у них Gemma 4 называется)
  3. ollama serve  (или она сама стартует)
"""
import httpx

import config


def generate_burunov(topic: str, context_jokes: list[dict]) -> str:
    """
    Принимает тему + список анекдотов (из retriever).
    Возвращает текст, готовый для TTS.
    """
    if not context_jokes:
        return (
            "Хм... Не припомню я такого... "
            "Может, помягче тему выберешь, а?"
        )

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

    print("Генерирую в стиле Бурунова...\n")
    text = generate_burunov(topic, hits)
    print("─" * 60)
    print(text)
    print("─" * 60)
