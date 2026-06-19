"""
MEET_RUDI — meetrudi-wa-webhook handler.

Receives Twilio inbound webhooks (and status callbacks), validates the signature, normalizes
the message, and enqueues it to the FIFO queue — then returns 200 fast. All real work happens
asynchronously in meetrudi-wa-processor.
"""

import os
import json

import boto3

import provider

sqs = boto3.client("sqs")
QUEUE_URL = os.environ["INBOUND_QUEUE_URL"]


def _ok(body=""):
    return {"statusCode": 200, "headers": {"Content-Type": "text/plain"}, "body": body}


def handler(event, context):
    try:
        params = provider.parse_body(event)

        if not provider.verify_signature(event, params):
            print("WARN: invalid Twilio signature")
            return {"statusCode": 403, "body": "invalid signature"}

        # Delivery status callbacks (sent/delivered/read/failed): log, don't enqueue.
        if params.get("MessageStatus") and not params.get("Body") and params.get("NumMedia", "0") == "0":
            print("STATUS %s -> %s" % (params.get("MessageSid"), params.get("MessageStatus")))
            return _ok()

        msg = provider.normalize(params)
        if not msg.provider_msg_id or not msg.user_phone:
            return _ok()

        sqs.send_message(
            QueueUrl=QUEUE_URL,
            MessageBody=json.dumps(msg.to_dict()),
            MessageGroupId="wa:" + msg.user_phone,           # per-user ordering
            MessageDeduplicationId=msg.provider_msg_id,        # dedupe Twilio retries
        )
        return _ok()

    except Exception as e:  # noqa: BLE001 - never 500 to Twilio on our bug; we logged it
        print("ERROR webhook: %s" % e)
        return _ok()
