"""
MEET_RUDI — structured call record for meetrudi-voice-bench.

Every bench call leaves an analysable trail in the shared data bucket:

    voice-bench/calls/{call_id}/manifest.json        config, state, running totals, outcome
    voice-bench/calls/{call_id}/turns/{seq:04d}.json one object per turn — never appended to,
                                                     so turns can't race or truncate each other
    voice-bench/calls/{call_id}/audio/{seq:04d}-user.{ext}
    voice-bench/calls/{call_id}/audio/{seq:04d}-rudi.{ext}
    voice-bench/index/{YYYY-MM-DD}/{call_id}.json    one light row per call, for listing

The manifest is also the call's *state*: the browser is a microphone and a speaker, nothing
more, so a reload or a flaky connection cannot lose the conversation.

Audio retention is a config switch and is written only when `store_audio` is true. That is
right for a bench staffed by the team and wrong for real patients — see the README.
"""

import os
import json
import uuid
import datetime

import boto3

s3 = boto3.client("s3")
DATA_BUCKET = os.environ["DATA_BUCKET"]
PREFIX = os.environ.get("VOICE_BENCH_PREFIX", "voice-bench")


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def iso(dt=None):
    return (dt or _now()).isoformat()


def new_call_id():
    """Sortable and unique: 20260812-143002-9f3a1c."""
    return "%s-%s" % (_now().strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:6])


def _manifest_key(call_id):
    return "%s/calls/%s/manifest.json" % (PREFIX, call_id)


def _turn_key(call_id, seq):
    return "%s/calls/%s/turns/%04d.json" % (PREFIX, call_id, seq)


def audio_key(call_id, seq, who, ext):
    return "%s/calls/%s/audio/%04d-%s.%s" % (PREFIX, call_id, seq, who, ext)


def _index_key(call_id, started_at):
    day = (started_at or iso())[:10]
    return "%s/index/%s/%s.json" % (PREFIX, day, call_id)


def _put_json(key, obj):
    s3.put_object(Bucket=DATA_BUCKET, Key=key,
                  Body=json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8"),
                  ContentType="application/json")


def _get_json(key):
    # Matched by name rather than `s3.exceptions.NoSuchKey` so this works against any S3 double.
    try:
        return json.loads(s3.get_object(Bucket=DATA_BUCKET, Key=key)["Body"].read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        if "NoSuchKey" not in type(e).__name__ and "NoSuchKey" not in str(e):
            print("WARN: could not read %s: %s" % (key, e))
        return None


def put_audio(call_id, seq, who, data, ext, content_type):
    """Store one side of one turn. Returns the key, or None when retention is off."""
    key = audio_key(call_id, seq, who, ext)
    s3.put_object(Bucket=DATA_BUCKET, Key=key, Body=data, ContentType=content_type)
    return key


def start(call_id, config, state, meta):
    manifest = {
        "call_id": call_id,
        "schema": 1,
        "status": "in_progress",
        "started_at": iso(),
        "ended_at": None,
        "end_reason": None,
        "config": config,
        "state": state,
        "meta": meta,
        "totals": {"turns": 0, "asr_ms": 0, "llm_ms": 0, "tts_ms": 0, "server_ms": 0},
        "outcome": {"goal": None, "goal_domain": None, "final_phase": state.get("phase")},
    }
    _put_json(_manifest_key(call_id), manifest)
    _put_json(_index_key(call_id, manifest["started_at"]), {
        "call_id": call_id,
        "started_at": manifest["started_at"],
        "status": "in_progress",
        "user_name": config.get("user_name"),
        "topic": config.get("topic"),
        "language": config.get("language"),
    })
    return manifest


def load(call_id):
    return _get_json(_manifest_key(call_id))


def record_turn(manifest, seq, record):
    """Persist one turn and fold its timings into the manifest. Returns the updated manifest."""
    call_id = manifest["call_id"]
    _put_json(_turn_key(call_id, seq), dict(record, call_id=call_id, seq=seq))

    totals = manifest.setdefault(
        "totals", {"turns": 0, "asr_ms": 0, "llm_ms": 0, "tts_ms": 0, "server_ms": 0})
    totals["turns"] = seq
    for field in ("asr_ms", "llm_ms", "tts_ms", "server_ms"):
        totals[field] = totals.get(field, 0) + int(record.get("timings", {}).get(field) or 0)

    manifest["state"] = record.get("state", manifest.get("state"))
    manifest["outcome"] = {
        "goal": manifest["state"].get("goal"),
        "goal_domain": manifest["state"].get("goal_domain"),
        "final_phase": manifest["state"].get("phase"),
    }
    _put_json(_manifest_key(call_id), manifest)
    return manifest


def finish(manifest, reason="hangup"):
    call_id = manifest["call_id"]
    manifest["status"] = "completed"
    manifest["ended_at"] = iso()
    manifest["end_reason"] = reason

    try:
        started = datetime.datetime.fromisoformat(manifest["started_at"])
        ended = datetime.datetime.fromisoformat(manifest["ended_at"])
        manifest["duration_s"] = round((ended - started).total_seconds(), 1)
    except (ValueError, TypeError, KeyError):
        manifest["duration_s"] = None

    turns = max(1, manifest.get("totals", {}).get("turns", 0))
    t = manifest.get("totals", {})
    manifest["averages"] = {
        "asr_ms": round(t.get("asr_ms", 0) / turns),
        "llm_ms": round(t.get("llm_ms", 0) / turns),
        "tts_ms": round(t.get("tts_ms", 0) / turns),
        "server_ms": round(t.get("server_ms", 0) / turns),
    }

    _put_json(_manifest_key(call_id), manifest)
    _put_json(_index_key(call_id, manifest["started_at"]), {
        "call_id": call_id,
        "started_at": manifest["started_at"],
        "ended_at": manifest["ended_at"],
        "status": "completed",
        "end_reason": reason,
        "duration_s": manifest.get("duration_s"),
        "turns": manifest.get("totals", {}).get("turns"),
        "averages": manifest["averages"],
        "user_name": manifest.get("config", {}).get("user_name"),
        "topic": manifest.get("config", {}).get("topic"),
        "language": manifest.get("config", {}).get("language"),
        "outcome": manifest.get("outcome"),
    })
    return manifest
