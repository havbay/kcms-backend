"""Small, deterministic normalizer for reply-rule matching."""

from __future__ import annotations

import re
import unicodedata

_KHMER_DIGITS = str.maketrans("០១២៣៤៥៦៧៨៩", "0123456789")
_INVISIBLE = {"\u200b", "\u200c", "\u200d", "\u2060", "\ufeff"}
_WHITESPACE = re.compile(r"\s+")


def _canonical_reorder(text: str) -> str:
    """Reorder combining marks by their canonical class within each base.

    NFC composes characters but does not repair every ordering difference in
    Khmer input. The result is intentionally modest and deterministic: it
    never transliterates or changes visible letters.
    """
    decomposed = unicodedata.normalize("NFD", text)
    output: list[str] = []
    marks: list[str] = []
    for char in decomposed:
        if unicodedata.combining(char):
            marks.append(char)
            continue
        if marks:
            output.extend(sorted(marks, key=lambda mark: (unicodedata.combining(mark), mark)))
            marks.clear()
        output.append(char)
    output.extend(sorted(marks, key=lambda mark: (unicodedata.combining(mark), mark)))
    return unicodedata.normalize("NFC", "".join(output))


def normalize_text(value: str) -> str:
    """Normalize keywords and inbound text identically."""
    value = _canonical_reorder(value.translate(_KHMER_DIGITS))
    value = "".join(
        char
        for char in value
        if char not in _INVISIBLE and unicodedata.category(char) != "Cf"
    )
    return _WHITESPACE.sub(" ", value).strip().lower()


def written_unit_count(value: str) -> int:
    """Return a conservative count of written units for the warning guard.

    Khmer subscript consonants are counted with their preceding coeng rather
    than as a new unit. For Latin text, letters and numbers are counted as
    units. This is only a write-time warning, never a match-time filter.
    """
    text = normalize_text(value)
    count = 0
    previous_was_coeng = False
    for char in text:
        if "ក" <= char <= "៿":
            if char == "្":
                previous_was_coeng = True
            elif unicodedata.category(char).startswith("L") and not previous_was_coeng:
                count += 1
            else:
                previous_was_coeng = False
        elif char.isalnum():
            count += 1
        else:
            previous_was_coeng = False
    return count
