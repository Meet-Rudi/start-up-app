"""
Identifiers — `{cat}_` plus 32 Crockford base32 characters, one registry of prefixes.

What this guards: a prefix silently reused (Enum turns a duplicate value into an alias, so two
categories would share ids), the docs table drifting from the code, and end-user ids that leak
across tenants.

Run:  python -m unittest discover -s packages/rudi_platform/tests -v
"""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "packages"))

from rudi_platform.ids import (
    ALPHABET,
    BODY_LEN,
    DESCRIPTIONS,
    Cat,
    category_of,
    is_id,
    new_id,
    pseudonym_id,
    require,
)

SALT_A = b"tenant-a-salt-0123456789"
SALT_B = b"tenant-b-salt-0123456789"
CLONE = "cln_" + "0" * 32


class Registry(unittest.TestCase):
    def test_prefixes_are_unique_three_letter_words(self):
        values = [m.value for m in Cat.__members__.values()]
        self.assertEqual(
            len(values), len(set(values)), "a prefix is used twice (Enum made an alias)"
        )
        for v in values:
            self.assertRegex(v, r"^[a-z]{3}$")

    def test_every_category_is_described(self):
        self.assertEqual(set(DESCRIPTIONS), set(Cat))

    def test_conventions_doc_lists_every_prefix(self):
        path = os.path.join(ROOT, "docs", "platform", "conventions.md")
        with open(path, encoding="utf-8") as f:
            doc = f.read()
        missing = [c.value for c in Cat if f"`{c.value}_`" not in doc]
        self.assertEqual(
            missing, [], "add these prefixes to docs/platform/conventions.md"
        )


class NewId(unittest.TestCase):
    def test_shape_and_category(self):
        for cat in Cat:
            if cat is Cat.USER:
                continue
            value = new_id(cat)
            self.assertEqual(len(value), 4 + BODY_LEN)
            self.assertTrue(value.startswith(cat.value + "_"))
            self.assertTrue(set(value[4:]) <= set(ALPHABET))
            self.assertIs(category_of(value), cat)

    def test_random(self):
        ids = {new_id(Cat.TURN) for _ in range(2000)}
        self.assertEqual(len(ids), 2000)

    def test_user_ids_are_never_random(self):
        with self.assertRaises(ValueError):
            new_id(Cat.USER)


class Pseudonym(unittest.TestCase):
    def test_stable_for_the_same_person(self):
        a = pseudonym_id(SALT_A, "whatsapp", "+32470000000", scope=CLONE)
        self.assertEqual(
            a, pseudonym_id(SALT_A, "whatsapp", "+32470000000", scope=CLONE)
        )
        self.assertIs(category_of(a), Cat.USER)

    def test_unrelated_across_tenants_channels_and_scopes(self):
        base = pseudonym_id(SALT_A, "whatsapp", "+32470000000", scope=CLONE)
        others = {
            pseudonym_id(SALT_B, "whatsapp", "+32470000000", scope=CLONE),
            pseudonym_id(SALT_A, "voice", "+32470000000", scope=CLONE),
            pseudonym_id(SALT_A, "whatsapp", "+32470000000"),
            pseudonym_id(SALT_A, "whatsapp", "+32470000001", scope=CLONE),
        }
        self.assertNotIn(base, others)
        self.assertEqual(len(others), 4)

    def test_rejects_weak_input(self):
        with self.assertRaises(ValueError):
            pseudonym_id(b"short", "whatsapp", "+32470000000")
        with self.assertRaises(ValueError):
            pseudonym_id(SALT_A, "WhatsApp", "+32470000000")
        with self.assertRaises(ValueError):
            pseudonym_id(SALT_A, "whatsapp", "")
        with self.assertRaises(ValueError):
            pseudonym_id(SALT_A, "whatsapp", "+32470000000", scope="ten_" + "0" * 32)


class Parsing(unittest.TestCase):
    def test_rejects_malformed(self):
        good = new_id(Cat.TENANT)
        for bad in (
            good.upper(),
            good[:-1],
            good + "0",
            "ten-" + good[4:],
            "zzz_" + good[4:],  # unknown category
            "ten_" + "i" * 32,  # i, l, o, u are not in the alphabet
            "ten_" + "u" * 32,
            None,
            42,
        ):
            self.assertIsNone(category_of(bad) if isinstance(bad, str) else None)
            self.assertFalse(is_id(bad))

    def test_require_checks_category(self):
        tid = new_id(Cat.TENANT)
        self.assertEqual(require(tid, Cat.TENANT), tid)
        with self.assertRaises(ValueError):
            require(tid, Cat.CLONE)


if __name__ == "__main__":
    unittest.main()
