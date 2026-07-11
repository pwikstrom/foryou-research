#!/usr/bin/env python3
"""Admin-editable hashtag stoplist (the former config.toml ``IRRELEVANT_WORDS``).

The stoplist filters junk tokens out of hashtag extraction
(``recode_variables.recode_tokenise``). This module owns it as
``irrelevant_words.json`` (storage location ``"users"``, next to
``admin_settings.json`` / ``var_presentation.json``):

    {"version": 1, "words": [...], "updated_at": "...", "updated_by": "..."}

When the store is missing it is seeded once from the config.toml
``[labels] IRRELEVANT_WORDS`` list (idempotent — concurrent seeding writes
identical content), after which the store is authoritative and the config
list is only a fallback for read failures.

Matching is smarter than the exact membership test the config list needed:

* Repeated characters are collapsed on both sides (``squeeze``), so one
  ``fyp`` entry catches ``fyyyyp`` / ``fypp`` / ``fyppppppppppppppppppppppp``.
* An entry ending in ``*`` is a prefix wildcard on the squeezed forms, so
  ``fyp*`` catches ``fypage`` / ``fypシ゚viral``.

Stoplist edits never touch the study hash (the list is applied only when
hashtags are extracted at scrape/annotation recode time; already-stored
``<field>_hashtags`` parquet columns are unchanged).
"""

import datetime as _dt
import hashlib
import json
import re

from fyp.logging_setup import get_logger

logger = get_logger(__name__)

FILENAME = "irrelevant_words.json"
LOCATION = "users"

# Squeezed wildcard prefixes shorter than this are rejected — too broad.
MIN_WILDCARD_PREFIX = 2

_RUN_RE = re.compile(r"(.)\1+")


class IrrelevantWordsConflict(Exception):
    """Raised when a save's expected etag does not match the stored state."""






def _data_io():
    """Lazy fyp.data_io accessor (avoids the fyp_config import cycle)."""
    import fyp.data_io as data_io

    return data_io






def _cf():
    """Lazy fyp_config accessor (avoids import-time config side effects)."""
    from fyp.fyp_config import fyp_cf

    return fyp_cf






def squeeze(s: str) -> str:
    """Collapse every run of an identical code point to a single occurrence.

    ``fyyyyp`` → ``fyp``, ``all`` → ``al``. Multi-codepoint sequences (emoji
    with modifiers, ``fypシ゚``) are not runs of one character and pass through
    untouched.
    """
    return _RUN_RE.sub(r"\1", s)






def normalize_entry(word: str) -> str:
    """Lowercase and strip an entry; return "" when it is unusable.

    Rejected (→ ""): empty/whitespace entries, a bare ``*``, and wildcard
    entries whose squeezed prefix is shorter than ``MIN_WILDCARD_PREFIX``
    (e.g. ``f*`` would swallow every f-hashtag).
    """
    w = str(word).strip().lower()
    if not w:
        return ""
    if w.endswith("*") and len(squeeze(w[:-1])) < MIN_WILDCARD_PREFIX:
        return ""
    return w






def dedupe_words(words: list[str]) -> list[str]:
    """Normalize, drop unusable entries, and dedupe by matching behaviour.

    Two entries that squeeze to the same form match the same tokens, so only
    the first is kept (``fyp``/``fypp``/``fyppp`` collapse to one entry).
    Wildcard and non-wildcard entries dedupe separately. Returns a sorted list.
    """
    seen: set[tuple[bool, str]] = set()
    kept: list[str] = []
    for word in words:
        w = normalize_entry(word)
        if not w:
            continue
        is_wild = w.endswith("*")
        key = (is_wild, squeeze(w[:-1] if is_wild else w))
        if key in seen:
            continue
        seen.add(key)
        kept.append(w)
    return sorted(kept)






def build_matcher(words: list[str]):
    """Return ``match(token) -> bool`` implementing squeeze + prefix wildcards.

    Tokens are expected pre-cleaned (lowercased, punctuation-stripped) the way
    ``recode_tokenise`` prepares them; the matcher only applies ``squeeze``.
    """
    exact = {squeeze(w) for w in words if not w.endswith("*")}
    prefixes = tuple(sorted({squeeze(w[:-1]) for w in words if w.endswith("*")}))

    def match(token: str) -> bool:
        t = squeeze(token)
        return t in exact or (bool(prefixes) and t.startswith(prefixes))

    return match






def _config_words() -> list[str]:
    """The config.toml seed/fallback list (empty on any failure)."""
    try:
        return [str(w) for w in _cf()["labels"]["IRRELEVANT_WORDS"]]
    except Exception as e:
        logger.warning(f"WARNING: config IRRELEVANT_WORDS unavailable ({e}).")
        return []






def load_payload() -> dict | None:
    """Load the store payload, or None when it does not exist / is unreadable."""
    try:
        if _data_io().exists(storage_location=LOCATION, filename=FILENAME):
            payload = _data_io().load_json(storage_location=LOCATION, filename=FILENAME)
            if isinstance(payload, dict) and isinstance(payload.get("words"), list):
                return payload
    except Exception as e:
        logger.warning(f"WARNING: irrelevant_words store unreadable ({e}).")
    return None






def load_words() -> list[str]:
    """Return the current stoplist, seeding the store from config on first use.

    Never raises — this sits inside the recode path. A missing store is seeded
    from the config.toml list (idempotent: concurrent seeds write identical
    content); when both the store and the seed write are unavailable the
    config list itself is returned.
    """
    payload = load_payload()
    if payload is not None:
        return [str(w) for w in payload["words"]]

    words = dedupe_words(_config_words())
    try:
        save_words(words, updated_by="config-seed")
    except Exception as e:
        logger.warning(f"WARNING: could not seed irrelevant_words store ({e}).")
    return words






def compute_words_etag(payload: dict | None = None) -> str:
    """Deterministic etag of the stoplist content (sha256 of canonical JSON)."""
    if payload is None:
        payload = load_payload()
    if payload is None:
        return "missing"
    canonical = json.dumps(sorted(payload.get("words", []) or []))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]






def save_words(
    words: list[str],
    expected_etag: str | None = None,
    updated_by: str = "",
) -> dict:
    """Persist the stoplist (full replace); returns ``{"etag", "words"}``.

    Args:
        words: the complete new list; entries are normalized and deduped by
            squeezed form (see ``dedupe_words``).
        expected_etag: when given, the save is refused (IrrelevantWordsConflict)
            if the stored content has changed since the caller read it.
        updated_by: username recorded in the payload for audit.

    Raises:
        IrrelevantWordsConflict: etag mismatch (concurrent edit).
        ValueError: malformed payload (not a list of strings, or an entry that
            normalizes away — empty, bare ``*``, too-short wildcard prefix).
    """
    if not isinstance(words, list) or not all(isinstance(w, str) for w in words):
        raise ValueError("words must be a list of strings")
    bad = [w for w in words if not normalize_entry(w)]
    if bad:
        raise ValueError(
            f"invalid entries (empty, or wildcard prefix shorter than "
            f"{MIN_WILDCARD_PREFIX} characters): {bad[:5]}"
        )

    if expected_etag is not None and expected_etag != compute_words_etag():
        raise IrrelevantWordsConflict(
            "irrelevant-words store changed since it was loaded — reload and retry"
        )

    cleaned = dedupe_words(words)
    payload = {
        "version": 1,
        "words": cleaned,
        "updated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "updated_by": updated_by,
    }
    _data_io().save_json(data=payload, storage_location=LOCATION, filename=FILENAME)
    return {"etag": compute_words_etag(payload), "words": cleaned}
