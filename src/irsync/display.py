"""Rendering of file names for previews: always printable, never ambiguous."""

from __future__ import annotations

import unicodedata

_NAMED_ESCAPES: dict[str, str] = {"\n": "\\n", "\r": "\\r", "\t": "\\t", "\\": "\\\\"}

_ESCAPED_CATEGORIES: frozenset[str] = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
"""Control, format, surrogate, and line/paragraph-separator characters."""

_SURROGATE_ESCAPE_LOW, _SURROGATE_ESCAPE_HIGH = 0xDC80, 0xDCFF
"""The lone surrogates ``surrogateescape`` decodes the bytes 0x80-0xFF to."""


def display_name(name: str) -> str:
    r"""Return ``name`` escaped so it is safe to print and cannot be misread.

    File names on Linux are arbitrary bytes. ``os.fsdecode`` turns a byte that
    is not valid UTF-8 into a lone surrogate, which a strict UTF-8 stdout and
    the pipe to ``less`` both refuse to encode, and a name may also hold a
    newline or a terminal escape that would fake or restyle a preview line.

    Escapes, each one unambiguous:

    - a byte that was not valid UTF-8 as ``\xNN``, the original byte;
    - a backslash, newline, carriage return, or tab as ``\\``, ``\n``,
      ``\r``, ``\t``;
    - any other control character below U+0080 as ``\xNN``. Those bytes are
      always valid UTF-8, so a ``\xNN`` below ``\x80`` never means a raw byte;
    - any other control, format, surrogate, or line/paragraph-separator code
      point as ``\uXXXX``, or ``\UXXXXXXXX`` above U+FFFF.

    Every other character, accented and non-Latin text included, is kept.

    Args:
        name: A file name or path, as ``os.fsdecode`` returns it.

    Returns:
        The printable rendering of ``name``.
    """
    if name.isascii() and name.isprintable() and "\\" not in name:
        return name
    out: list[str] = []
    for ch in name:
        code = ord(ch)
        if ch in _NAMED_ESCAPES:
            out.append(_NAMED_ESCAPES[ch])
        elif _SURROGATE_ESCAPE_LOW <= code <= _SURROGATE_ESCAPE_HIGH:
            out.append(f"\\x{code - 0xDC00:02x}")
        elif unicodedata.category(ch) not in _ESCAPED_CATEGORIES:
            out.append(ch)
        elif code < 0x80:
            out.append(f"\\x{code:02x}")
        elif code <= 0xFFFF:
            out.append(f"\\u{code:04x}")
        else:
            out.append(f"\\U{code:08x}")
    return "".join(out)
