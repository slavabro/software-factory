"""Flat YAML front matter without a YAML library: one `key: value` per line, values read as YAML 1.1 reads them.

Task specs only need scalars. A value comes back as None, bool, int, float, datetime.date or str, the types
`yaml.safe_load` gives, so a spec reads the same here and in any YAML tool. Anything richer (lists, nested maps,
block scalars, anchors) is refused with the line, never guessed. `scalar()` writes a value plain when it reads back
unchanged, else double-quoted (JSON escapes are valid YAML).
"""

from __future__ import annotations

import datetime as dt
import json
import re

NULL = {"", "~", "null", "Null", "NULL"}
TRUE = {"yes", "Yes", "YES", "true", "True", "TRUE", "on", "On", "ON"}
FALSE = {"no", "No", "NO", "false", "False", "FALSE", "off", "Off", "OFF"}
# PyYAML's resolver patterns (YAML 1.1).
INT = re.compile(r"[-+]?(0|[1-9][0-9_]*)")
OCTAL = re.compile(r"[-+]?0[0-7_]+")
HEX = re.compile(r"[-+]?0x[0-9a-fA-F_]+")
BINARY = re.compile(r"[-+]?0b[01_]+")
SEXAGESIMAL = re.compile(r"[-+]?[1-9][0-9_]*(:[0-5]?[0-9])+(\.[0-9_]*)?")
FLOAT = re.compile(r"[-+]?[0-9][0-9_]*\.[0-9_]*([eE][-+][0-9]+)?|\.[0-9][0-9_]*([eE][-+][0-9]+)?")
SPECIAL_FLOAT = {".inf": "inf", "+.inf": "inf", "-.inf": "-inf", ".nan": "nan"}
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
KEY = re.compile(r"[A-Za-z_][\w-]*")
# A plain scalar may not start with an indicator, and may not hold ": " or " #" (a mapping or a comment).
PLAIN_START = set("-?:,[]{}#&*!|>'\"%@`")


class FrontMatterError(ValueError):
    pass


def _value(raw: str, where: str):
    text = raw.strip()
    if text.startswith('"'):
        if not text.endswith('"') or len(text) < 2:
            raise FrontMatterError(f"{where}: unclosed double quote")
        try:
            return json.loads(text)
        except ValueError as e:
            raise FrontMatterError(f"{where}: bad double-quoted value ({e.msg})") from None
    if text.startswith("'"):
        if not text.endswith("'") or len(text) < 2:
            raise FrontMatterError(f"{where}: unclosed single quote")
        return text[1:-1].replace("''", "'")
    if " #" in text:  # a comment
        text = text.split(" #", 1)[0].rstrip()
    if text.startswith("#"):
        text = ""
    if text in NULL:
        return None
    if text in TRUE:
        return True
    if text in FALSE:
        return False
    if INT.fullmatch(text):
        return int(text.replace("_", ""))
    if OCTAL.fullmatch(text):
        return int(text.replace("_", ""), 8)
    if HEX.fullmatch(text) or BINARY.fullmatch(text):
        return int(text.replace("_", ""), 0)
    if FLOAT.fullmatch(text):
        return float(text.replace("_", ""))
    if SEXAGESIMAL.fullmatch(text):  # 12:30 is 750 in YAML 1.1
        sign = -1 if text.startswith("-") else 1
        total = 0.0
        for part in text.lstrip("+-").replace("_", "").split(":"):
            total = total * 60 + float(part)
        return sign * (total if "." in text else int(total))
    if text.lower() in SPECIAL_FLOAT and text[-3:] in {"inf", "Inf", "INF", "nan", "NaN", "NAN"}:
        return float(SPECIAL_FLOAT[text.lower()])
    if DATE.fullmatch(text):
        try:
            return dt.date.fromisoformat(text)
        except ValueError:
            raise FrontMatterError(f"{where}: `{text}` looks like a date but is not one (quote it)") from None
    if text[0] in PLAIN_START and not (text[0] in "-?:" and len(text) > 1 and text[1] != " "):
        raise FrontMatterError(f"{where}: `{text}` needs quotes (it starts with `{text[0]}`)")
    if ": " in text or text.endswith(":"):
        raise FrontMatterError(f"{where}: `{text}` needs quotes (it holds `: `)")
    return text


def load(block: str, where: str = "front matter") -> dict:
    """The mapping in a front matter block (the text between the `---` lines)."""
    data: dict = {}
    for n, line in enumerate(block.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, rest = line.partition(":")
        here = f"{where} line {n}"
        if not sep or not KEY.fullmatch(key) or (rest and not rest.startswith(" ")):
            raise FrontMatterError(f"{here}: not a flat `key: value` line: {line!r}")
        if rest.strip() in {"|", ">", "|-", ">-"} or rest.strip()[:1] in {"[", "{", "&", "*", "!"}:
            raise FrontMatterError(f"{here}: only plain values are supported, got {rest.strip()!r}")
        if key in data:
            raise FrontMatterError(f"{here}: duplicate key `{key}`")
        data[key] = _value(rest, here)
    return data


def scalar(value) -> str:
    """`value` as YAML text: plain when it reads back unchanged, else JSON-quoted."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    text = str(value)
    if text and text == text.strip() and "\n" not in text and "\t" not in text:
        try:
            back = _value(text, "")
        except FrontMatterError:
            back = None
        if back == text or (isinstance(back, dt.date) and back.isoformat() == text):
            return text
    return json.dumps(text, ensure_ascii=False)
