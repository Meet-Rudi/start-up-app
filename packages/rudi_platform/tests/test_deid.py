"""
Two-tier de-identification — the platform copy (ported with the service tests, 2026-10-28).

No real PII anywhere: every national number below is synthetic with a computed mod-97 checksum,
and the names are stock examples.

Run:  python -m unittest discover -s packages/rudi_platform/tests -v
"""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..")))

from rudi_platform.privacy import deid


def _rrn(base9: str) -> str:
    """A checksum-valid 11-digit national number from 9 leading digits (pre-2000 rule)."""
    return base9 + f"{97 - (int(base9) % 97):02d}"


VALID = _rrn("850730033")


class NationalId(unittest.TestCase):
    def test_dotted_format_is_redacted(self):
        d = VALID
        out, found = deid.redact(
            f"my number is {d[0:2]}.{d[2:4]}.{d[4:6]}-{d[6:9]}.{d[9:]} ok"
        )
        self.assertIn("<| users-national-id |>", out)
        self.assertEqual(found["users-national-id"], 1)
        self.assertNotIn(d[:6], out)

    def test_bare_integer_format_is_redacted(self):
        out, _ = deid.redact(f"rijksregisternummer {VALID} please")
        self.assertIn("<| users-national-id |>", out)

    def test_post_2000_checksum_variant(self):
        base = "050415123"
        d = base + f"{97 - (int('2' + base) % 97):02d}"
        self.assertIn("<| users-national-id |>", deid.redact(f"id {d}")[0])

    def test_bis_number_month_offset(self):
        self.assertIn(
            "<| users-national-id |>", deid.redact(f"bis {_rrn('854730033')}")[0]
        )

    def test_bad_checksum_is_left_alone(self):
        bad = VALID[:-2] + ("00" if VALID[-2:] != "00" else "11")
        out, found = deid.redact(f"code {bad}")
        self.assertNotIn("users-national-id", found)
        self.assertIn(bad, out)

    def test_ordinary_numbers_survive(self):
        for benign in (
            "I walked 12000 steps",
            "I weigh 82.5 kg",
            "see you at 10.30",
            "my glucose was 7.8 this morning",
            "I did 3 sets of 12",
        ):
            self.assertEqual(deid.redact(benign), (benign, {}), benign)


class OtherDirectIdentifiers(unittest.TestCase):
    def test_email_and_iban(self):
        out, _ = deid.redact(
            "mail me at jan.peeters@example.be or send to BE68539007547034"
        )
        self.assertIn("<| email-address |>", out)
        self.assertIn("<| bank-account |>", out)
        self.assertNotIn("example.be", out)

    def test_invalid_iban_untouched(self):
        self.assertNotIn("bank-account", deid.redact("ref BE68539007547035 here")[1])

    def test_international_phone(self):
        self.assertIn(
            "<| phone-number |>", deid.redact("call me on +32 470 12 34 56")[0]
        )

    def test_payment_card_luhn(self):
        self.assertIn("<| payment-card |>", deid.redact("card 4111 1111 1111 1111")[0])
        self.assertNotIn("payment-card", deid.redact("order 4111 1111 1111 1112")[1])


class AliasVault(unittest.TestCase):
    def setUp(self):
        self.det = deid.HeuristicDetector(gazetteer={"peter", "sophie", "anouk"})
        self.vault = deid.AliasVault()

    def test_mask_then_unmask_round_trip(self):
        masked = self.vault.mask("and I went running with Peter today", self.det)
        self.assertEqual(masked, "and I went running with <| Person_A |> today")
        self.assertEqual(
            self.vault.unmask(
                "and did <| Person_A |>'s presence improve your results?"
            ),
            "and did Peter's presence improve your results?",
        )

    def test_alias_is_stable_across_turns(self):
        self.vault.mask("I ran with Peter", self.det)
        self.assertIn("<| Person_A |>", self.vault.mask("Peter was tired", self.det))

    def test_second_person_gets_a_new_alias(self):
        masked = self.vault.mask("Peter and Sophie came along", self.det)
        self.assertIn("<| Person_A |>", masked)
        self.assertIn("<| Person_B |>", masked)

    def test_unresolvable_placeholder_never_reaches_the_user(self):
        self.assertEqual(
            self.vault.unmask("hi <| Person_Z |> there", fallback="they"),
            "hi they there",
        )
        self.assertFalse(deid.has_placeholder(self.vault.unmask("<| Person_Z |>")))

    def test_role_descriptor_survives_vault_loss(self):
        self.vault.mask("I ran with Peter", self.det)
        self.vault.set_role("Person_A", "running partner")
        dropped = deid.AliasVault(roles=self.vault.roles)
        self.assertEqual(
            dropped.unmask("how is <| Person_A |>?"), "how is running partner?"
        )

    def test_serialization_round_trip(self):
        self.vault.mask("with Peter", self.det)
        self.vault.set_role("Person_A", "colleague")
        copy = deid.AliasVault.from_dict(self.vault.to_dict())
        self.assertEqual(
            (copy.mapping, copy.roles), (self.vault.mapping, self.vault.roles)
        )

    def test_trigger_grammar_catches_unknown_names(self):
        masked = deid.AliasVault().mask(
            "I went with Aleksandra yesterday", deid.HeuristicDetector()
        )
        self.assertEqual(masked, "I went with <| Person_A |> yesterday")

    def test_whitespace_drift_in_placeholder_is_tolerated(self):
        self.vault.mask("with Peter", self.det)
        for variant in ("<|Person_A|>", "<| Person_A|>", "<|  Person_A  |>"):
            self.assertEqual(self.vault.unmask(f"hi {variant}"), "hi Peter")

    def test_relation_without_a_name_masks_nothing(self):
        text = "I went with my wife to the hospital"
        vault = deid.AliasVault()
        self.assertEqual(vault.mask(text, deid.HeuristicDetector()), text)
        self.assertEqual(vault.mapping, {})

    def test_title_is_kept_and_the_name_is_masked(self):
        masked = deid.AliasVault().mask(
            "I spoke with Doctor Smith about my sugar", deid.HeuristicDetector()
        )
        self.assertEqual(masked, "I spoke with Doctor <| Person_A |> about my sugar")


class PersonaNames(unittest.TestCase):
    """Platform change: the persona is whoever the clone says, not a hard-coded 'Rudi'."""

    def test_persona_names_are_kept(self):
        det = deid.HeuristicDetector(
            gazetteer={"emma", "rudi"}, keep=frozenset({"Emma"})
        )
        self.assertEqual(deid.AliasVault().mask("thanks Emma, I went with Emma", det),
                         "thanks Emma, I went with Emma")  # fmt: skip

    def test_rudi_is_a_person_unless_the_clone_says_so(self):
        masked = deid.AliasVault().mask("I went with Rudi", deid.HeuristicDetector())
        self.assertEqual(masked, "I went with <| Person_A |>")
        kept = deid.AliasVault().mask(
            "I went with Rudi", deid.HeuristicDetector(keep=frozenset({"Rudi"}))
        )
        self.assertEqual(kept, "I went with Rudi")


class ChannelHelpers(unittest.TestCase):
    def test_tier1_runs_before_tier2_and_survives_it(self):
        vault = deid.AliasVault()
        masked, found = deid.scrub_inbound(
            vault,
            f"I'm {VALID} and I ran with Peter",
            deid.HeuristicDetector(gazetteer={"peter"}),
        )
        self.assertIn("<| users-national-id |>", masked)
        self.assertIn("<| Person_A |>", masked)
        self.assertEqual(found, {"users-national-id": 1})

    def test_restore_outbound_never_leaves_markup(self):
        vault = deid.AliasVault(mapping={"Peter": "Person_A"})
        text = "Say hi to <| Person_A |> and <| Person_Q |>, mail <| email-address |>"
        self.assertEqual(
            deid.restore_outbound(vault, text), "Say hi to Peter and them, mail them"
        )
        self.assertEqual(
            deid.restore_outbound(vault, text, redacted="[{} removed]"),
            "Say hi to Peter and them, mail [email-address removed]",
        )


if __name__ == "__main__":
    unittest.main()
