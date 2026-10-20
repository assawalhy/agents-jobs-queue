"""The escape stripper: the sequences go, the text stays.

The daemon feeds the child's raw bytes to `StreamStripper` and writes what
comes out to `out.log`, so the file the agents read is plain text.
"""

from __future__ import annotations

import unittest

import helpers

from ajq.ansi import StreamStripper, strip_escapes


class TestStreamStripper(unittest.TestCase):
    def test_the_sequences_go_and_the_text_stays(self):
        stripper = StreamStripper()
        self.assertEqual(stripper.feed(b"\x1b[31mred\x1b[0m plain\n"), b"red plain\n")
        self.assertEqual(stripper.flush(), b"")

    def test_a_sequence_split_across_two_feeds_is_removed(self):
        stripper = StreamStripper()
        self.assertEqual(stripper.feed(b"a\x1b[3"), b"a")
        self.assertEqual(stripper.feed(b"1mred\n"), b"red\n")
        self.assertEqual(stripper.flush(), b"")

    def test_a_lone_escape_is_removed(self):
        stripper = StreamStripper()
        self.assertEqual(stripper.feed(b"a\x1bb"), b"ab")
        self.assertEqual(stripper.flush(), b"")

    def test_a_lone_escape_at_the_end_of_the_stream_is_removed(self):
        stripper = StreamStripper()
        self.assertEqual(stripper.feed(b"text\x1b"), b"text")
        self.assertEqual(stripper.flush(), b"")

    def test_flush_returns_the_held_back_tail(self):
        """The tail the stripper held back is not lost at the end of the stream."""
        stripper = StreamStripper()
        self.assertEqual(stripper.feed(b"text\x1b[3"), b"text")
        # the incomplete sequence comes back without its escape byte
        self.assertEqual(stripper.flush(), b"[3")
        self.assertEqual(stripper.flush(), b"")

    def test_the_osc_sequence_is_removed(self):
        stripper = StreamStripper()
        self.assertEqual(stripper.feed(b"\x1b]0;title\x07hello"), b"hello")
        self.assertEqual(stripper.feed(b" world"), b" world")
        self.assertEqual(stripper.flush(), b"")


class TestStripEscapes(unittest.TestCase):
    def test_the_sequences_go_and_the_text_stays(self):
        self.assertEqual(strip_escapes("\x1b[1;32mgreen\x1b[0m text"), "green text")

    def test_a_lone_escape_is_removed(self):
        self.assertEqual(strip_escapes("a\x1bb"), "ab")


if __name__ == "__main__":
    unittest.main()
