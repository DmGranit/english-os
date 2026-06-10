"""
Тонкая обёртка над LLM. Провайдер-агностична (OpenAI-совместимый формат по умолчанию).
Ключ берётся из переменной окружения. Заполни MODEL под свой бюджет.
"""
import os, json, logging, urllib.request

log = logging.getLogger("english_os.llm")

PROVIDER  = os.environ.get("LLM_PROVIDER", "openai")     # openai | anthropic
MODEL     = os.environ.get("LLM_MODEL", "gpt-4o-mini")   # дешёвая модель для бота
API_KEY   = os.environ.get("LLM_API_KEY", "")
STT_MODEL = os.environ.get("STT_MODEL", "whisper-1")     # распознавание речи (OpenAI)

def chat(system, messages, max_tokens=600, model=None):
    """
    system   — системный промпт (ЯДРО + режим, из prompts.assemble()).
    messages — история [{'role':'user'|'assistant','content':...}].
    Возвращает строку ответа. model переопределяет модель (иначе берётся MODEL).
    """
    mdl = model or MODEL
    if not API_KEY:
        # режим без ключа — заглушка, чтобы каркас можно было гонять локально
        return "[LLM-заглушка: задай LLM_API_KEY] " + (messages[-1]["content"] if messages else "")

    try:
        if PROVIDER == "anthropic":
            url = "https://api.anthropic.com/v1/messages"
            headers = {"x-api-key": API_KEY, "anthropic-version": "2023-06-01",
                       "content-type": "application/json"}
            body = {"model": mdl, "max_tokens": max_tokens,
                    "system": system, "messages": messages}
            req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read())
            return "".join(b["text"] for b in data["content"] if b["type"] == "text")

        # openai-совместимый
        url = "https://api.openai.com/v1/chat/completions"
        headers = {"Authorization": f"Bearer {API_KEY}", "content-type": "application/json"}
        body = {"model": mdl, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, *messages]}
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
        return data["choices"][0]["message"]["content"]
    except Exception:
        log.exception("LLM call failed (model=%s)", mdl)   # причина в лог, пользователю — мягко
        return "⚠️ Связь с ИИ сейчас недоступна. Попробуй ещё раз через минуту."


def transcribe(audio_bytes, filename="voice.ogg"):
    """Распознать речь из аудио (OpenAI). Возвращает текст или '' при сбое/без ключа.
    Multipart/form-data собираем вручную (без внешних зависимостей)."""
    if not API_KEY:
        return ""
    boundary = "----englishosBoundary7MA4YWxkTrZu0gW"
    pre = (f"--{boundary}\r\n"
           f"Content-Disposition: form-data; name=\"model\"\r\n\r\n{STT_MODEL}\r\n"
           f"--{boundary}\r\n"
           f"Content-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
           f"Content-Type: audio/ogg\r\n\r\n").encode()
    post = f"\r\n--{boundary}--\r\n".encode()
    body = pre + bytes(audio_bytes) + post
    req = urllib.request.Request(
        "https://api.openai.com/v1/audio/transcriptions", data=body,
        headers={"Authorization": f"Bearer {API_KEY}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read()).get("text", "").strip()
    except Exception:
        return ""
