"""Tests for the two-tier de-identification helper (§0.1 / §5).

No real PII anywhere: every "national number" below is a synthetic value whose mod-97 checksum was
computed for the test, and the names are stock examples.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import deid  # noqa: E402


def _rrn(base9: str) -> str:
    """Build a checksum-valid 11-digit RRN from 9 leading digits (pre-2000 rule)."""
    return base9 + "%02d" % (97 - (int(base9) % 97))


VALID = _rrn("850730033")          # e.g. 85.07.30-033.xx


class TestNationalId(unittest.TestCase):
    def test_dotted_format_is_redacted(self):
        d = VALID
        text = "my number is %s.%s.%s-%s.%s ok" % (d[0:2], d[2:4], d[4:6], d[6:9], d[9:])
        out, found = deid.redact(text)
        self.assertIn("<| users-national-id |>", out)
        self.assertEqual(found["users-national-id"], 1)
        self.assertNotIn(d[:6], out)

    def test_bare_integer_format_is_redacted(self):
        out, found = deid.redact("rijksregisternummer %s please" % VALID)
        self.assertIn("<| users-national-id |>", out)
        self.assertEqual(found["users-national-id"], 1)

    def test_post_2000_checksum_variant(self):
        base = "050415123"
        d = base + "%02d" % (97 - (int("2" + base) % 97))
        out, _ = deid.redact("id %s" % d)
        self.assertIn("<| users-national-id |>", out)

    def test_bis_number_month_offset(self):
        # Non-resident "bis" numbers carry month + 20 or + 40; must still redact.
        d = _rrn("854730033")   # month 47 = July + 40
        out, _ = deid.redact("bis %s" % d)
        self.assertIn("<| users-national-id |>", out)

    def test_bad_checksum_is_left_alone(self):
        bad = VALID[:-2] + ("00" if VALID[-2:] != "00" else "11")
        out, found = deid.redact("code %s" % bad)
        self.assertNotIn("users-national-id", found)
        self.assertIn(bad, out)

    def test_ordinary_numbers_survive(self):
        """The mod-97 gate is what keeps step counts, weights and dates out of the redactor."""
        for benign in ("I walked 12000 steps", "I weigh 82.5 kg", "see you at 10.30",
                       "my glucose was 7.8 this morning", "I did 3 sets of 12"):
            out, found = deid.redact(benign)
            self.assertEqual(out, benign, benign)
            self.assertEqual(found, {}, benign)


class TestOtherDirectIdentifiers(unittest.TestCase):
    def test_email_and_iban(self):
        out, found = deid.redact("mail me at jan.peeters@example.be or send to BE68539007547034")
        self.assertIn("<| email-address |>", out)
        self.assertIn("<| bank-account |>", out)
        self.assertNotIn("example.be", out)

    def test_invalid_iban_untouched(self):
        out, found = deid.redact("ref BE68539007547035 here")
        self.assertNotIn("bank-account", found)

    def test_international_phone(self):
        out, found = deid.redact("call me on +32 470 12 34 56")
        self.assertIn("<| phone-number |>", out)

    def test_payment_card_luhn(self):
        out, found = deid.redact("card 4111 1111 1111 1111")
        self.assertIn("<| payment-card |>", out)
        out2, found2 = deid.redact("order 4111 1111 1111 1112")
        self.assertNotIn("payment-card", found2)


class TestAliasVault(unittest.TestCase):
    def setUp(self):
        self.det = deid.HeuristicDetector(gazetteer={"peter", "sophie", "anouk"})
        self.vault = deid.AliasVault()

    def test_mask_then_unmask_round_trip(self):
        text = "and I went running with Peter today"
        masked = self.vault.mask(text, self.det)
        self.assertEqual(masked, "and I went running with <| Person_A |> today")
        self.assertNotIn("Peter", masked)

        reply = "and did <| Person_A |>'s presence improve your results?"
        self.assertEqual(self.vault.unmask(reply),
                         "and did Peter's presence improve your results?")

    def test_alias_is_stable_across_turns(self):
        self.vault.mask("I ran with Peter", self.det)
        # Later turn, no trigger word — the known-name pass must still mask it.
        self.assertIn("<| Person_A |>", self.vault.mask("Peter was tired", self.det))

    def test_second_person_gets_a_new_alias(self):
        masked = self.vault.mask("Peter and Sophie came along", self.det)
        self.assertIn("<| Person_A |>", masked)
        self.assertIn("<| Person_B |>", masked)
        self.assertEqual(len(self.vault.mapping), 2)

    def test_unresolvable_placeholder_never_reaches_the_user(self):
        """A hallucinated or expired alias must not be sent as raw markup."""
        self.assertEqual(self.vault.unmask("hi <| Person_Z |> there", fallback="they"),
                         "hi they there")
        self.assertFalse(deid.has_placeholder(self.vault.unmask("<| Person_Z |>")))

    def test_role_descriptor_survives_vault_loss(self):
        self.vault.mask("I ran with Peter", self.det)
        self.vault.set_role("Person_A", "running partner")
        roles = self.vault.roles
        dropped = deid.AliasVault(roles=roles)          # new session: names gone, roles kept
        self.assertEqual(dropped.unmask("how is <| Person_A |>?"), "how is running partner?")

    def test_serialization_round_trip(self):
        self.vault.mask("with Peter", self.det)
        self.vault.set_role("Person_A", "colleague")
        clone = deid.AliasVault.from_dict(self.vault.to_dict())
        self.assertEqual(clone.mapping, self.vault.mapping)
        self.assertEqual(clone.roles, self.vault.roles)

    def test_trigger_grammar_catches_unknown_names(self):
        """A name in no gazetteer must still be caught by the frame."""
        v = deid.AliasVault()
        masked = v.mask("I went with Aleksandra yesterday", deid.HeuristicDetector())
        self.assertIn("<| Person_A |>", masked)
        self.assertNotIn("Aleksandra", masked)

    def test_whitespace_drift_in_placeholder_is_tolerated(self):
        """Models reformat the markup; unmasking must not be brittle about it."""
        self.vault.mask("with Peter", self.det)
        for variant in ("<|Person_A|>", "<| Person_A|>", "<|  Person_A  |>"):
            self.assertEqual(self.vault.unmask("hi %s" % variant), "hi Peter")

    def test_relation_without_a_name_masks_nothing(self):
        """Regression: a case-insensitive frame made [A-Z] match lowercase, so "my wife to the
        hospital" detected "to" as a person and mangled the sentence."""
        v = deid.AliasVault()
        text = "I went with my wife to the hospital"
        self.assertEqual(v.mask(text, deid.HeuristicDetector()), text)
        self.assertEqual(v.mapping, {})

    def test_title_is_kept_and_the_name_is_masked(self):
        """Regression: the title was captured as the name, leaking the real one."""
        v = deid.AliasVault()
        masked = v.mask("I spoke with Doctor Smith about my sugar", deid.HeuristicDetector())
        self.assertEqual(masked, "I spoke with Doctor <| Person_A |> about my sugar")
        self.assertNotIn("Smith", masked)

    def test_rudi_is_not_a_person(self):
        v = deid.AliasVault()
        self.assertEqual(v.mask("thanks Rudi", deid.HeuristicDetector(gazetteer={"rudi"})),
                         "thanks Rudi")


class TestPipelineOrder(unittest.TestCase):
    def test_tier1_runs_before_tier2_and_survives_it(self):
        """A Tier-1 redaction must pass through Tier-2 masking untouched."""
        clean, _ = deid.redact("I'm %s and I ran with Peter" % VALID)
        masked = deid.AliasVault().mask(clean, deid.HeuristicDetector(gazetteer={"peter"}))
        self.assertIn("<| users-national-id |>", masked)
        self.assertIn("<| Person_A |>", masked)


if __name__ == "__main__":
    unittest.main()
