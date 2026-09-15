"""
MEET_RUDI — offline eval for the inbound objection gate (CLAUDE.md §8).

The gate freezes a conversation and pulls in a human, so both directions of error are expensive
and they are NOT symmetric:

  a MISS  keeps Rudi messaging somebody who told us to stop — the failure that becomes a
          complaint, a block, and a damaged number rating (§3).
  a FALSE POSITIVE freezes a real patient mid-coaching. A health coach is told "ik wil stoppen
          met roken" constantly, so this is not hypothetical.

The gate is therefore held to DIFFERENT bars in the two directions, and the lexicon is held to a
stricter false-positive bar than the model, because it is the layer that runs on every message
and cannot be talked out of a match.

Run (lexicon only — no network, no spend, this is the one CI should run):

    python evals/objection/run.py

Run including the model layer (costs tokens, needs the gateway configured):

    python evals/objection/run.py --model

Exits non-zero when a threshold is missed, so it can gate a deploy.
"""

import os
import sys
import json
import argparse

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "services", "whatsapp", "src"))

import objection  # noqa: E402

DATASET = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dataset.jsonl")

# Thresholds. Deliberately asymmetric — see the module docstring.
#
# ZERO false positives is the only defensible bar for the lexicon: every one of them is a real
# patient silenced mid-conversation by a phrase list we wrote, which is the one failure here we
# control completely. Recall is allowed to be imperfect on the lexicon alone precisely because
# the model layer exists to cover the rest.
MAX_FALSE_POSITIVES = 0
MIN_RECALL_LEXICON = 0.85      # of cases not marked lexicon_miss
MIN_RECALL_WITH_MODEL = 0.95   # including the indirect phrasings


def load():
    rows = []
    with open(DATASET, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", action="store_true",
                    help="also run the classifier layer (costs tokens)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    rows = load()
    hits = misses = false_positives = 0
    kind_wrong = []
    failures = []

    for row in rows:
        expected = bool(row.get("objection"))
        # A case the phrase list is not expected to catch is only counted towards recall when
        # the model layer is actually in play. Scoring it against the lexicon alone would make
        # the deterministic run fail for doing exactly what it was designed to do.
        if row.get("lexicon_miss") and not args.model:
            continue
        got = objection.detect(row["text"], use_model=args.model)

        if expected and got.fired:
            hits += 1
            if row.get("kind") and got.kind != row["kind"]:
                kind_wrong.append((row["id"], row["kind"], got.kind))
        elif expected and not got.fired:
            misses += 1
            failures.append(("MISS", row["id"], row["text"], ""))
        elif not expected and got.fired:
            false_positives += 1
            failures.append(("FALSE POSITIVE", row["id"], row["text"], got.evidence))
        if args.verbose:
            print("%-12s %-6s %s" % (row["id"], "FIRED" if got.fired else "-", row["text"][:60]))

    total_obj = hits + misses
    recall = (hits / total_obj) if total_obj else 1.0
    min_recall = MIN_RECALL_WITH_MODEL if args.model else MIN_RECALL_LEXICON

    print("\n=== objection gate — %s ===" % ("lexicon + model" if args.model else "lexicon only"))
    print("objections   : %d  (caught %d, missed %d)  recall %.2f  [min %.2f]"
          % (total_obj, hits, misses, recall, min_recall))
    print("ordinary     : %d  false positives %d  [max %d]"
          % (len(rows) - total_obj - (0 if args.model else
             sum(1 for r in rows if r.get("lexicon_miss"))),
             false_positives, MAX_FALSE_POSITIVES))
    if kind_wrong:
        # Not a failure: every kind freezes. It only misroutes the operator's triage.
        print("kind mismatches (triage only, not fatal): %d" % len(kind_wrong))
        for cid, want, got_kind in kind_wrong:
            print("   %-12s expected %-13s got %s" % (cid, want, got_kind))

    if failures:
        print("\n--- failures ---")
        for tag, cid, text, evidence in failures:
            print("%-15s %-12s %r%s" % (tag, cid, text[:70],
                                        ("  <- matched %r" % evidence) if evidence else ""))

    ok = (false_positives <= MAX_FALSE_POSITIVES) and (recall >= min_recall)
    print("\n%s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
