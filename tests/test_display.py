"""Tests for irsync.display: rendering file names so they are safe to print."""

import pytest

from irsync.display import display_name


class TestDisplayName:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            # os.fsdecode turns each byte that is not valid UTF-8 into a lone
            # surrogate, U+DC00 + byte: b"\xff" arrives as "\udcff".
            ("bad-\udcff.txt", "bad-\\xff.txt"),
            ("\udc80\udcfe", "\\x80\\xfe"),
        ],
    )
    def test_undecodable_byte_is_shown_as_the_original_byte(self, name, expected):
        # Bug caught: a lone surrogate passed through raises
        # UnicodeEncodeError on any strict UTF-8 stdout, and on the pipe to
        # `less`, which is how a moved non-UTF-8 file name made a whole
        # backup exit 2 before this fix.
        assert display_name(name) == expected

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("two\nlines", "two\\nlines"),
            ("tab\there", "tab\\there"),
            ("cr\rhere", "cr\\rhere"),
            ("\x1b[31mred", "\\x1b[31mred"),
            ("del\x7f", "del\\x7f"),
            ("nul\x00", "nul\\x00"),
        ],
    )
    def test_ascii_control_characters_are_escaped(self, name, expected):
        # Bug caught: a newline in a file name prints as a second preview
        # line the name's owner wrote, and an ESC sequence drives the
        # terminal instead of being shown.
        assert display_name(name) == expected

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("next\u0085line", "next\\u0085line"),  # C1 control (NEL)
            ("evil‮txt.exe", "evil\\u202etxt.exe"),  # bidi override
            ("zero​width", "zero\\u200bwidth"),  # zero-width space
            ("line sep", "line\\u2028sep"),  # line separator
            ("para sep", "para\\u2029sep"),  # paragraph separator
            ("tag\U000e0001", "tag\\U000e0001"),  # format char above U+FFFF
        ],
    )
    def test_other_invisible_or_line_breaking_characters_are_escaped(
        self, name, expected
    ):
        # Bug caught: escaping only ASCII controls leaves characters that
        # break a line or reorder the text of a name on screen, so a preview
        # line could still look like something it is not.
        assert display_name(name) == expected

    def test_backslash_is_doubled_so_escapes_are_unambiguous(self):
        # Bug caught: without doubling, a name that literally contains the
        # four characters "\xff" prints exactly like a name holding the
        # undecodable byte 0xff, so the preview could not tell them apart.
        assert display_name("back\\slash") == "back\\\\slash"
        assert display_name("a\\xff") == "a\\\\xff"
        assert display_name("a\\xff") != display_name("a\udcff")

    @pytest.mark.parametrize(
        "name",
        ["Films", "name with spaces", "café", "日本語", "😀", "a-b_c.d", ""],
    )
    def test_ordinary_names_are_unchanged(self, name):
        # Bug caught: over-escaping (for example ascii() or repr()) turns
        # every accented or non-Latin name into an unreadable escape string.
        assert display_name(name) == name
