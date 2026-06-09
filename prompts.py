"""Загрузчик модульного промпта: режет prompt.md на блоки, склеивает ЯДРО + режим."""
import re, os

_PATH = os.path.join(os.path.dirname(__file__), "prompt.md")

def _load_blocks(path=_PATH):
    text = open(path, encoding="utf-8").read()
    blocks, cur, buf = {}, None, []
    for line in text.splitlines():
        m = re.match(r"^## === (.+?) ===", line)
        if m:
            if cur:
                blocks[cur] = "\n".join(buf).strip()
            cur, buf = m.group(1).strip(), []
        elif cur:
            buf.append(line)
    if cur:
        blocks[cur] = "\n".join(buf).strip()
    # убрать хвостовые разделители ---
    for k in blocks:
        blocks[k] = re.sub(r"\n*-{3,}\s*$", "", blocks[k]).strip()
    return blocks

BLOCKS = _load_blocks()
MODE_KEYS = {"new": "NEW", "review": "REVIEW", "scenario": "SCENARIO",
             "flow": "FLOW", "input": "INPUT"}

def assemble(mode, with_summary=False):
    """Системный промпт для вызова модели: ЯДРО + модуль режима (+ ИТОГ при завершении)."""
    parts = [BLOCKS["ЯДРО"], BLOCKS[MODE_KEYS[mode]]]
    if with_summary:
        parts.append(BLOCKS["ИТОГ"])
    return "\n\n".join(parts)
