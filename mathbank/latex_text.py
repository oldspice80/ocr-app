from __future__ import annotations

import re


NUMBER_TOKEN = re.compile(r"(?<![A-Za-z0-9\\])[-+]?\d+(?:\.\d+)?(?![A-Za-z0-9])")


def latexize_plain_numbers(value: str) -> str:
    """Wrap numbers outside existing math delimiters in inline LaTeX."""
    text = str(value or "")
    parts = re.split(r"(\\\(|\\\)|\\\[|\\\]|\$\$|\$)", text)
    depth = 0
    result: list[str] = []
    for part in parts:
        if part in ("$", "$$"):
            depth = 0 if depth else 1
            result.append(part)
        elif part in (r"\(", r"\["):
            depth += 1
            result.append(part)
        elif part in (r"\)", r"\]") and depth:
            depth -= 1
            result.append(part)
        elif depth:
            result.append(part)
        else:
            result.append(NUMBER_TOKEN.sub(lambda match: rf"\({match.group(0)}\)", part))
    return "".join(result)
