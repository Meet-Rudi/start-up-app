"""
Resource names — meetrudi-internal-… and meetrudi-tenant-…, checked against AWS limits.

What this guards: an IAM grant on meetrudi-tenant-* that also matches a platform function, and a
long cell or skill slug that would only fail halfway through a CloudFormation deploy.

Run:  python -m unittest discover -s packages/rudi_platform/tests -v
"""

from __future__ import annotations

import fnmatch
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..")))

from rudi_platform.ids import Cat, new_id
from rudi_platform.naming import (
    Plane,
    bucket_name,
    iam_pattern,
    internal_name,
    kms_alias,
    platform_s3_key,
    queue_name,
    secret_path,
    skill_function,
    tenant_name,
    tenant_s3_key,
    tenant_s3_prefix,
    tenant_secret_path,
)

ACCOUNT = "949753869755"


class Names(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(
            internal_name("prod", "console-api"), "meetrudi-internal-prod-console-api"
        )
        self.assertEqual(
            tenant_name("prod", "pool", "engine"), "meetrudi-tenant-prod-pool-engine"
        )
        self.assertEqual(
            skill_function("dev", "d-acme", "knowledge-retrieval"),
            "meetrudi-tenant-dev-d-acme-skill-knowledge-retrieval",
        )
        self.assertEqual(
            queue_name(tenant_name("prod", "pool", "inbound", kind="queue"), fifo=True),
            "meetrudi-tenant-prod-pool-inbound.fifo",
        )

    def test_planes_never_overlap_in_iam_patterns(self):
        internal = internal_name("prod", "tenant-sync")
        tenant = tenant_name("prod", "pool", "internal-sync")
        self.assertTrue(fnmatch.fnmatch(internal, iam_pattern(Plane.INTERNAL)))
        self.assertFalse(fnmatch.fnmatch(internal, iam_pattern(Plane.TENANT)))
        self.assertTrue(fnmatch.fnmatch(tenant, iam_pattern(Plane.TENANT, "prod")))
        self.assertFalse(fnmatch.fnmatch(tenant, iam_pattern(Plane.INTERNAL)))

    def test_rejects_bad_parts(self):
        for call in (
            lambda: internal_name("staging", "engine"),
            lambda: tenant_name("prod", "shared", "engine"),
            lambda: tenant_name("prod", "d-toolongtenant", "engine"),
            lambda: tenant_name("prod", "pool", "Engine"),
            lambda: tenant_name("prod", "pool", "my_engine"),
        ):
            with self.assertRaises(ValueError):
                call()

    def test_aws_length_limits(self):
        with self.assertRaises(ValueError):
            skill_function(
                "prod", "d-abcdefghij", "a-very-long-skill-slug-that-overflows"
            )
        self.assertLessEqual(
            len(skill_function("prod", "d-abcdefghij", "knowledge-retrieval")), 64
        )

    def test_buckets(self):
        self.assertEqual(
            bucket_name(Plane.TENANT, "prod", ACCOUNT, "pool"),
            "meetrudi-tenant-prod-pool-data-" + ACCOUNT,
        )
        self.assertEqual(
            bucket_name(Plane.INTERNAL, "dev", ACCOUNT),
            "meetrudi-internal-dev-data-" + ACCOUNT,
        )
        with self.assertRaises(ValueError):
            bucket_name(Plane.INTERNAL, "dev", ACCOUNT, "pool")
        with self.assertRaises(ValueError):
            bucket_name(Plane.TENANT, "dev", ACCOUNT)
        self.assertLessEqual(
            len(bucket_name(Plane.TENANT, "prod", ACCOUNT, "d-abcdefghij")), 63
        )

    def test_secrets_and_keys(self):
        tid = new_id(Cat.TENANT)
        self.assertEqual(
            secret_path(Plane.INTERNAL, "prod", "arena", "bootstrap"),
            "meetrudi/internal/prod/arena/bootstrap",
        )
        self.assertEqual(
            tenant_secret_path("prod", tid, "models", "openai"),
            f"meetrudi/tenant/prod/{tid}/models/openai",
        )
        self.assertEqual(kms_alias("prod", tid), "alias/meetrudi-tenant-prod-" + tid)
        with self.assertRaises(ValueError):
            kms_alias("prod", new_id(Cat.CLONE))
        with self.assertRaises(ValueError):
            secret_path(Plane.INTERNAL, "prod", "a/b")


class S3Keys(unittest.TestCase):
    """Every key holding a tenant's data sits in tenants/{slug}/ (decision #41)."""

    def test_a_tenant_key_sits_in_the_tenant_folder(self):
        cnv = new_id(Cat.CONVERSATION)
        self.assertEqual(tenant_s3_prefix("cm"), "tenants/cm/")
        self.assertEqual(
            tenant_s3_key("cm", "conversations", cnv, "snapshot.json"),
            f"tenants/cm/conversations/{cnv}/snapshot.json",
        )
        self.assertEqual(
            tenant_s3_key("acme-health", "runtime/checkpoints", "x.json"),
            "tenants/acme-health/runtime/checkpoints/x.json",
        )
        self.assertEqual(
            tenant_s3_key("cm", "assets", "brief@voice.md"),
            "tenants/cm/assets/brief@voice.md",
        )

    def test_objects_of_no_tenant_go_under_platform(self):
        self.assertEqual(
            platform_s3_key("arena", "results.json"), "platform/arena/results.json"
        )

    def test_a_key_cannot_leave_its_folder(self):
        for parts in (
            ("..", "other", "x.json"),
            ("a/../../other",),
            ("/abs",),
            ("a//b",),
            ("a/",),
            (".",),
            ("",),
            ("with space",),
            (),
        ):
            with self.subTest(parts=parts), self.assertRaises(ValueError):
                tenant_s3_key("cm", *parts)

    def test_the_folder_is_a_valid_slug(self):
        for slug in ("", "CM", "cm/", "../cm", "cm_x", "-cm", new_id(Cat.TENANT)):
            with self.subTest(slug=slug), self.assertRaises(ValueError):
                tenant_s3_prefix(slug)

    def test_key_length_is_checked(self):
        with self.assertRaises(ValueError):
            tenant_s3_key("cm", "a" * 1020)


if __name__ == "__main__":
    unittest.main()
