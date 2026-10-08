"""标题、机构名和外部 id 的规范化。

机构匹配用 token 集合，不用子串。这样别名 TRI 不会撞上 TRIangle，
同时 NVIDIA 仍能命中 NVIDIA Research，因为 nvidia 这个 token 被包含。
"""

from __future__ import annotations

import re

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_DOI_PREFIX = re.compile(r"^https?://(dx\.)?doi\.org/", re.I)
_ARXIV_IN_TEXT = re.compile(r"(\d{4}\.\d{4,5})(?:v\d+)?", re.I)


def tokens(value: str) -> list[str]:
    """把字符串收成小写 token。连字符和标点都当成分隔符。"""
    return _TOKEN_RE.findall((value or "").lower())


def normalize_phrase(value: str) -> str:
    return " ".join(tokens(value))


def alias_matches(affiliation: str, alias: str) -> bool:
    """别名与机构名全等，或别名 token 是机构名 token 的子集。

    禁止 `alias in affiliation`。TRIangle 规范化后是一个 token triangle，
    不包含 tri，因此不会命中别名 TRI。
    """
    alias_tokens = tokens(alias)
    affiliation_tokens = tokens(affiliation)
    if not alias_tokens or not affiliation_tokens:
        return False
    if alias_tokens == affiliation_tokens:
        return True
    return set(alias_tokens).issubset(affiliation_tokens)


def phrase_in_text(text: str, phrase: str) -> bool:
    """按词边界匹配短语。sim-to-real 与 sim to real 视为同一种写法。"""
    parts = tokens(phrase)
    if not parts or not text:
        return False
    pattern = r"\b" + r"\W+".join(re.escape(part) for part in parts) + r"\b"
    return re.search(pattern, text, flags=re.I) is not None


def normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    doi = _DOI_PREFIX.sub("", value.strip())
    doi = doi.split("?", 1)[0].strip().rstrip(" .)")
    return doi.lower() or None


def normalize_arxiv_id(value: str | None) -> str | None:
    if not value:
        return None
    match = _ARXIV_IN_TEXT.search(value.replace("arXiv:", ""))
    if not match:
        return None
    return match.group(1)


def normalize_openalex_id(value: str | None) -> str | None:
    if not value:
        return None
    tail = value.rstrip("/").split("/")[-1]
    return tail or None


def normalize_ror(value: str | None) -> str | None:
    if not value:
        return None
    tail = value.rstrip("/").split("/")[-1]
    return tail.lower() or None


def year_from_date(value: str | None) -> int | None:
    if not value or len(value) < 4 or not value[:4].isdigit():
        return None
    return int(value[:4])
