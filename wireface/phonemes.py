"""Text -> phonemes for lip sync (and sentence splitting for the TTS).

Pronunciations come from the CMU dictionary that ships with the keyword spotter
(``en.phone``: 126k words, ARPAbet with stress). Numbers, times and symbols are spelled
out roughly the way the TTS reads them; unknown words fall back to letter rules. A few
mismatches don't matter: the aligner only needs the right order of mouth shapes.
"""
from __future__ import annotations

import functools
import re
from pathlib import Path

from . import config

# --- sentences ---------------------------------------------------------------------------------
_ABBREV = {"mr", "mrs", "ms", "dr", "st", "vs", "etc", "e.g", "i.e", "approx", "no", "jr", "sr", "prof", "inc"}


def split_sentences(text: str) -> list[str]:
    """Sentences (and lines) in order; abbreviations and decimals don't end a sentence."""
    out: list[str] = []
    for line in re.split(r"\n+", text):
        start = 0
        for m in re.finditer(r"[.!?]+[\"')\]]*(?=\s+|$)", line):
            end = m.end()
            word = re.findall(r"[\w.]+$", line[start:m.start()])
            if m.group().startswith(".") and word and word[0].lower().rstrip(".") in _ABBREV:
                continue
            piece = line[start:end].strip()
            if piece:
                out.append(piece)
            start = end
        rest = line[start:].strip()
        if rest:
            out.append(rest)
    # glue fragments without letters (e.g. "...") onto their neighbour
    merged: list[str] = []
    for s in out:
        if merged and not re.search(r"[A-Za-z0-9]", s):
            merged[-1] += " " + s
        else:
            merged.append(s)
    return merged


# --- numbers ---------------------------------------------------------------------------------------
_ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen " \
        "sixteen seventeen eighteen nineteen".split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
_ORD = {"one": "first", "two": "second", "three": "third", "five": "fifth", "eight": "eighth", "nine": "ninth",
        "twelve": "twelfth"}


def number_words(n: int) -> str:
    if n < 0:
        return "minus " + number_words(-n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + ("" if n % 10 == 0 else " " + _ONES[n % 10])
    if n < 1000:
        return _ONES[n // 100] + " hundred" + ("" if n % 100 == 0 else " " + number_words(n % 100))
    for size, name in ((10 ** 9, "billion"), (10 ** 6, "million"), (1000, "thousand")):
        if n >= size:
            rest = n % size
            return number_words(n // size) + " " + name + ("" if rest == 0 else " " + number_words(rest))
    return str(n)


def ordinal_words(n: int) -> str:
    words = number_words(n).split()
    last = words[-1]
    if last in _ORD:
        words[-1] = _ORD[last]
    elif last.endswith("y"):
        words[-1] = last[:-1] + "ieth"
    else:
        words[-1] = last + "th"
    return " ".join(words)


def _year(n: int) -> str:
    hi, lo = divmod(n, 100)
    if lo == 0:
        return number_words(hi) + " hundred"
    if 2000 <= n < 2010:
        return number_words(n)
    return number_words(hi) + " " + ("oh " + _ONES[lo] if lo < 10 else number_words(lo))


def normalize(text: str) -> str:
    """Spell out what the TTS will say for numbers, times and common symbols."""
    t = text
    t = re.sub(r"https?://\S+", " a link ", t)
    t = re.sub(r"(\d{1,2}):(\d{2})\b", lambda m: number_words(int(m[1])) + " " + (
        "o'clock" if m[2] == "00" else ("oh " + _ONES[int(m[2])] if m[2][0] == "0" else number_words(int(m[2])))), t)
    t = re.sub(r"\$(\d[\d,]*)(?:\.(\d\d))?", lambda m: m[1] + " dollars" + (f" {int(m[2])} cents" if m[2] else ""), t)
    t = re.sub(r"(\d)\s*%", r"\1 percent", t)
    t = re.sub(r"(\d+)(st|nd|rd|th)\b", lambda m: ordinal_words(int(m[1])), t)
    t = re.sub(r"\b(1[1-9]\d\d|20\d\d)\b(?![,.]\d)", lambda m: _year(int(m[1])), t)
    t = re.sub(r"(\d+)\.(\d+)", lambda m: number_words(int(m[1])) + " point " + " ".join(_ONES[int(d)] for d in m[2]), t)
    t = re.sub(r"\d{1,3}(?:,\d{3})+|\d+", lambda m: number_words(int(m[0].replace(",", ""))), t)
    for sym, word in (("&", " and "), ("+", " plus "), ("=", " equals "), ("@", " at "), ("°", " degrees ")):
        t = t.replace(sym, word)
    return t


# --- pronunciations ----------------------------------------------------------------------------------
@functools.lru_cache(maxsize=1)
def lexicon(path: str | None = None) -> dict[str, list[str]]:
    lex: dict[str, list[str]] = {}
    p = Path(path) if path else config.KWS_DIR / "en.phone"
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) > 1:
                lex.setdefault(parts[0], parts[1:])
    except OSError:
        pass
    return lex


# letter rules for unknown words: longest match first (rough, but keeps lip closures and rounding)
_RULES = [
    ("tion", "SH AH0 N"), ("sion", "ZH AH0 N"), ("ough", "OW1"), ("igh", "AY1"), ("tch", "CH"), ("sch", "S K"),
    ("ph", "F"), ("th", "TH"), ("sh", "SH"), ("ch", "CH"), ("ck", "K"), ("ng", "NG"), ("qu", "K W"),
    ("wh", "W"), ("kn", "N"), ("wr", "R"), ("gh", "G"), ("oo", "UW1"), ("ee", "IY1"), ("ea", "IY1"),
    ("ai", "EY1"), ("ay", "EY1"), ("oa", "OW1"), ("ow", "OW1"), ("ou", "AW1"), ("oi", "OY1"), ("oy", "OY1"),
    ("au", "AO1"), ("aw", "AO1"), ("ew", "UW1"), ("ie", "IY1"), ("ei", "EY1"),
    ("a", "AE1"), ("b", "B"), ("c", "K"), ("d", "D"), ("e", "EH1"), ("f", "F"), ("g", "G"), ("h", "HH"),
    ("i", "IH1"), ("j", "JH"), ("k", "K"), ("l", "L"), ("m", "M"), ("n", "N"), ("o", "AA1"), ("p", "P"),
    ("q", "K"), ("r", "R"), ("s", "S"), ("t", "T"), ("u", "AH1"), ("v", "V"), ("w", "W"), ("x", "K S"),
    ("y", "Y"), ("z", "Z"),
]


def letters_to_phones(word: str) -> list[str]:
    w, out, i = word.lower(), [], 0
    if len(w) > 2 and w.endswith("e") and w[-2] not in "aeiou":
        w = w[:-1]                                   # silent final e
    while i < len(w):
        for pat, ph in _RULES:
            if w.startswith(pat, i):
                out += ph.split()
                i += len(pat)
                break
        else:
            i += 1
    return out


def word_phones(word: str, lex: dict[str, list[str]] | None = None) -> list[str]:
    lex = lexicon() if lex is None else lex
    w = word.upper().strip("'")
    if not w:
        return []
    if w in lex:
        return lex[w]
    if w.endswith("'S") and w[:-2] in lex:          # possessive
        base = lex[w[:-2]]
        return base + (["IH0", "Z"] if base[-1] in ("S", "Z", "SH", "ZH", "CH", "JH") else ["Z"])
    if w.endswith("S") and w[:-1] in lex:           # plural not in the dictionary
        return lex[w[:-1]] + ["Z"]
    if w.isalpha() and w.isupper() and len(w) <= 5 and word.isupper() and all(c in lex for c in w):
        return [p for c in w for p in lex[c]]       # acronym: spell it (GPU, NASA is in the dictionary)
    return letters_to_phones(w)


PAUSE = "|"      # punctuation: a likely pause


def phones(text: str, lex: dict[str, list[str]] | None = None) -> list[list[str]]:
    """Words as phone lists, with ``[PAUSE]`` entries where punctuation suggests a break."""
    out: list[list[str]] = []
    for tok in re.findall(r"[A-Za-z][A-Za-z']*|[,;:.!?()—–-]+", normalize(text)):
        if tok[0].isalpha():
            ph = word_phones(tok, lex)
            if ph:
                out.append(ph)
        elif out and out[-1] != [PAUSE] and not re.fullmatch(r"-+", tok):
            out.append([PAUSE])
    while out and out[-1] == [PAUSE]:
        out.pop()
    return out
