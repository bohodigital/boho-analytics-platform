from __future__ import annotations

import copy
import json
import os
import pickle
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from boho_analytics_platform import models as analytics_models
from boho_analytics_platform.models import (
    AnalyticsDefinition,
    DefinitionIdentity,
    DefinitionType,
    DefinitionValidationError,
    ValidatedDefinition,
    validate_analytics_definition,
)
from boho_analytics_platform.storage import (
    SCHEMA_VERSION,
    DefinitionCollisionError,
    DefinitionIntegrityError,
    DefinitionNotActiveError,
    DefinitionNotFoundError,
    SQLiteMetricStore,
)


BASE_TIME = datetime(2026, 7, 26, 12, tzinfo=UTC)
FIXTURE_RECIPIENT_DIGEST_KEY = bytes(range(32))
SUBSCRIPTION_IDENTITY = DefinitionIdentity(
    "portfolio:example",
    DefinitionType.REPORT_SUBSCRIPTION,
    "weekly-portfolio",
)


def subscription_recipient_inputs(
    recipients: tuple[str, ...] = ("operator@example.invalid",),
    digest_key: bytes = FIXTURE_RECIPIENT_DIGEST_KEY,
) -> dict[DefinitionIdentity, tuple[tuple[str, ...], bytes]]:
    return {SUBSCRIPTION_IDENTITY: (recipients, digest_key)}


def goal(
    *,
    key: str = "qualified-pageview",
    metric: str = "ga4.page-views",
    metadata: dict[str, str] | None = None,
) -> AnalyticsDefinition:
    return AnalyticsDefinition(
        definition_type=DefinitionType.GOAL,
        definition_key=key,
        scope_key="site:example",
        content={
            "aggregation": "sum",
            "confidence": "high",
            "coverage_requirement": 0.95,
            "date_basis": "site_local",
            "goal_type": "page",
            "maturity_lag_days": 2,
            "metric": metric,
            "provider_bindings": [
                {"metric": metric, "role": "canonical", "source": "google"}
            ],
            "site_ids": ["example"],
            "source": "google",
            "unit": "count",
        },
        metadata=metadata or {"label": "Qualified page view"},
    )


def ratio_goal(*, key: str = "qualified-ratio") -> AnalyticsDefinition:
    return replace(
        goal(key=key),
        content={
            **goal(key=key).content,
            "aggregation": "ratio",
            "denominator": {
                "completeness_policy": "final_only",
                "date_basis": "site_local",
                "grain": "day",
                "metric": "ga4.sessions",
                "scope": "same-site",
                "unit": "count",
                "window": 1,
                "zero_behavior": "unknown",
            },
            "unit": "ratio",
        },
    )


def segment() -> AnalyticsDefinition:
    return AnalyticsDefinition(
        definition_type=DefinitionType.SEGMENT,
        definition_key="organic-guides",
        scope_key="site:example",
        content={
            "site_ids": ["example"],
            "expression": {
                "all": [
                    {
                        "dimension": "channel",
                        "operator": "equals",
                        "value": "organic",
                    },
                    {
                        "dimension": "route",
                        "operator": "starts_with",
                        "value": "/guides/",
                    },
                ]
            },
        },
    )


def alert_rule() -> AnalyticsDefinition:
    return AnalyticsDefinition(
        definition_type=DefinitionType.ALERT_RULE,
        definition_key="coverage-drop",
        scope_key="site:example",
        content={
            "cooldown_minutes": 120,
            "evaluation_grain": "day",
            "incomplete_data_policy": "suppress",
            "maturity_lag_days": 2,
            "minimum_baseline": 50,
            "quiet_periods": [{"end": "06:00", "start": "00:00"}],
            "rule_type": "coverage_drop",
            "severity": "warning",
            "site_ids": ["example"],
            "threshold": 0.8,
        },
    )


def absolute_threshold_alert() -> AnalyticsDefinition:
    return replace(
        alert_rule(),
        definition_key="absolute-threshold",
        content={
            **alert_rule().content,
            "rule_type": "absolute_threshold",
        },
    )


def subscription() -> AnalyticsDefinition:
    return AnalyticsDefinition(
        definition_type=DefinitionType.REPORT_SUBSCRIPTION,
        definition_key="weekly-portfolio",
        scope_key="portfolio:example",
        content={
            "formats": ["html", "csv"],
            "frequency": "weekly",
            "incomplete_data_policy": "suppress",
            "maturity_lag_days": 2,
            "report_type": "portfolio_summary",
            "site_ids": ["example"],
            "timezone": "America/Chicago",
        },
    )


class DefinitionRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = SQLiteMetricStore(Path(self.temporary.name) / "state.db")
        self.store.initialize()

    def snapshot(self) -> tuple[list[tuple], list[tuple], list[tuple]]:
        with self.store.connect(readonly=True) as db:
            versions = db.execute(
                "SELECT * FROM analytics_definition_versions ORDER BY id"
            ).fetchall()
            activations = db.execute(
                "SELECT * FROM analytics_definition_activations ORDER BY id"
            ).fetchall()
            retirements = db.execute(
                "SELECT * FROM analytics_definition_retirements ORDER BY id"
            ).fetchall()
        return (
            [tuple(row) for row in versions],
            [tuple(row) for row in activations],
            [tuple(row) for row in retirements],
        )

    def test_closed_definition_types_validate_and_canonicalize(self) -> None:
        for definition in (goal(), segment(), alert_rule()):
            validated = validate_analytics_definition(definition)
            self.assertEqual(len(validated.content_hash), 64)
            self.assertFalse(validated.content_json.endswith(" "))
            self.assertNotIn(": ", validated.content_json)
        validated = validate_analytics_definition(
            subscription(),
            recipient_set=("operator@example.invalid",),
            recipient_digest_key=FIXTURE_RECIPIENT_DIGEST_KEY,
        )
        self.assertEqual(len(validated.content_hash), 64)
        self.assertNotIn(": ", validated.content_json)

    def test_create_reuse_replace_retire_reactivate_and_rollback(self) -> None:
        first = self.store.apply_definition_package(
            [goal()], transaction_time=BASE_TIME
        ).changes[0]
        self.assertEqual(first.outcome, "created")
        self.assertEqual(first.version.version, 1)

        unchanged = self.store.apply_definition_package(
            [goal(metadata={"label": "Ignored immutable metadata"})],
            transaction_time=BASE_TIME + timedelta(minutes=1),
        ).changes[0]
        self.assertEqual(unchanged.outcome, "unchanged")
        self.assertEqual(unchanged.version.id, first.version.id)
        self.assertEqual(unchanged.activation.id, first.activation.id)

        second = self.store.apply_definition_package(
            [goal(metric="ga4.engaged-page-views")],
            transaction_time=BASE_TIME + timedelta(minutes=2),
        ).changes[0]
        self.assertEqual(second.version.version, 2)
        self.assertEqual(second.activation.activated_at, BASE_TIME + timedelta(minutes=2))
        with self.store.connect(readonly=True) as db:
            prior = db.execute(
                """SELECT retired_at
                     FROM analytics_definition_retirements
                    WHERE activation_id=?""",
                (first.activation.id,),
            ).fetchone()[0]
        self.assertEqual(prior, (BASE_TIME + timedelta(minutes=2)).isoformat())

        retired = self.store.retire_definition(
            DefinitionIdentity(
                "site:example", DefinitionType.GOAL, "qualified-pageview"
            ),
            transaction_time=BASE_TIME + timedelta(minutes=3),
        )
        self.assertEqual(retired.retired_at, BASE_TIME + timedelta(minutes=3))
        self.assertIsNone(
            self.store.get_current_definition(
                DefinitionIdentity(
                    "site:example", DefinitionType.GOAL, "qualified-pageview"
                )
            )
        )

        reused = self.store.apply_definition_package(
            [goal(metric="ga4.engaged-page-views")],
            transaction_time=BASE_TIME + timedelta(minutes=4),
        ).changes[0]
        self.assertEqual(reused.outcome, "reactivated")
        self.assertEqual(reused.version.id, second.version.id)
        self.assertNotEqual(reused.activation.id, second.activation.id)

        rolled_back = self.store.activate_definition_version(
            first.version.id, transaction_time=BASE_TIME + timedelta(minutes=5)
        )
        self.assertEqual(rolled_back.outcome, "reactivated")
        self.assertEqual(rolled_back.version.id, first.version.id)
        self.assertEqual(rolled_back.version.version, 1)
        self.assertEqual(self.store.verify_definition_integrity(), {
            "versions": 2,
            "activations": 4,
            "retirements": 3,
        })

    def test_same_timestamp_reactivation_retains_distinct_history(self) -> None:
        first = self.store.apply_definition_package(
            [goal()], transaction_time=BASE_TIME
        ).changes[0]
        second = self.store.apply_definition_package(
            [goal(metric="ga4.engaged-page-views")],
            transaction_time=BASE_TIME,
        ).changes[0]
        reactivated = self.store.activate_definition_version(
            first.version.id, transaction_time=BASE_TIME
        )
        self.assertNotEqual(first.activation.id, reactivated.activation.id)
        self.assertEqual(
            {
                first.activation.activated_at,
                second.activation.activated_at,
                reactivated.activation.activated_at,
            },
            {BASE_TIME},
        )
        self.assertEqual(
            self.store.verify_definition_integrity(),
            {"versions": 2, "activations": 3, "retirements": 2},
        )

    def test_missing_or_inactive_operations_fail_without_writes(self) -> None:
        before = self.snapshot()
        with self.assertRaises(DefinitionNotFoundError):
            self.store.activate_definition_version(
                "f" * 64, transaction_time=BASE_TIME
            )
        self.assertEqual(self.snapshot(), before)

        identity = DefinitionIdentity(
            "site:example", DefinitionType.GOAL, "qualified-pageview"
        )
        with self.assertRaises(DefinitionNotActiveError):
            self.store.retire_definition(identity, transaction_time=BASE_TIME)
        self.assertEqual(self.snapshot(), before)

        self.store.apply_definition_package([goal()], transaction_time=BASE_TIME)
        self.store.retire_definition(
            identity, transaction_time=BASE_TIME + timedelta(minutes=1)
        )
        inactive = self.snapshot()
        with self.assertRaises(DefinitionNotActiveError):
            self.store.retire_definition(
                identity, transaction_time=BASE_TIME + timedelta(minutes=2)
            )
        self.assertEqual(self.snapshot(), inactive)

    def test_digest_collision_fails_without_writes(self) -> None:
        original = validate_analytics_definition(goal())
        self.store.apply_definition_package([goal()], transaction_time=BASE_TIME)
        before = self.snapshot()
        collision = ValidatedDefinition(
            definition_type=original.definition_type,
            definition_key=original.definition_key,
            scope_key=original.scope_key,
            content_json=original.content_json.replace(
                "ga4.page-views", "ga4.other-view"
            ),
            metadata_json=original.metadata_json,
            content_hash=original.content_hash,
        )
        with patch(
            "boho_analytics_platform.storage.validate_analytics_definition",
            return_value=collision,
        ):
            with self.assertRaises(DefinitionCollisionError):
                self.store.apply_definition_package(
                    [goal()], transaction_time=BASE_TIME + timedelta(minutes=1)
                )
        self.assertEqual(self.snapshot(), before)

    def test_package_validation_and_interruptions_are_all_or_nothing(self) -> None:
        self.store.apply_definition_package([goal()], transaction_time=BASE_TIME)
        before = self.snapshot()
        invalid = replace(goal(key="invalid"), content={"metric": "only"})
        with self.assertRaises(DefinitionValidationError):
            self.store.apply_definition_package(
                [segment(), invalid],
                transaction_time=BASE_TIME + timedelta(minutes=1),
            )
        self.assertEqual(self.snapshot(), before)

        replacement = goal(metric="ga4.engaged-page-views")
        for target_step in ("after_version", "after_retirement", "after_activation"):
            def interrupt(step: str, target: str = target_step) -> None:
                if step == target:
                    raise RuntimeError(f"interrupt {target}")

            with self.assertRaisesRegex(RuntimeError, "interrupt"):
                self.store.apply_definition_package(
                    [replacement, segment()],
                    transaction_time=BASE_TIME + timedelta(minutes=2),
                    _step_hook=interrupt,
                )
            self.assertEqual(self.snapshot(), before)

        identity = DefinitionIdentity(
            "site:example", DefinitionType.GOAL, "qualified-pageview"
        )
        with self.assertRaisesRegex(RuntimeError, "interrupt"):
            self.store.retire_definition(
                identity,
                transaction_time=BASE_TIME + timedelta(minutes=3),
                _step_hook=lambda _step: (_ for _ in ()).throw(
                    RuntimeError("interrupt retirement")
                ),
            )
        self.assertEqual(self.snapshot(), before)

    def test_omission_is_not_retirement(self) -> None:
        self.store.apply_definition_package([goal()], transaction_time=BASE_TIME)
        self.store.apply_definition_package(
            [segment()], transaction_time=BASE_TIME + timedelta(minutes=1)
        )
        self.assertIsNotNone(
            self.store.get_current_definition(
                DefinitionIdentity(
                    "site:example", DefinitionType.GOAL, "qualified-pageview"
                )
            )
        )

    def test_cross_references_are_type_checked_before_writing(self) -> None:
        initial = self.store.apply_definition_package(
            [goal(), segment()], transaction_time=BASE_TIME
        ).changes
        goal_version = initial[0].version.id
        segment_version = initial[1].version.id
        alert = replace(
            alert_rule(),
            content={
                **alert_rule().content,
                "goal_version_id": goal_version,
                "segment_version_id": segment_version,
            },
        )
        report = replace(
            subscription(),
            content={
                **subscription().content,
                "goal_version_ids": [goal_version],
                "segment_version_id": segment_version,
            },
        )
        composite = replace(
            goal(key="composite-outcome"),
            content={
                **goal(key="composite-outcome").content,
                "goal_type": "composite",
                "goal_version_ids": [goal_version],
            },
        )
        applied = self.store.apply_definition_package(
            [alert, report, composite],
            recipient_inputs=subscription_recipient_inputs(),
            transaction_time=BASE_TIME + timedelta(minutes=1),
        )
        self.assertEqual(len(applied.changes), 3)

        before = self.snapshot()
        missing = replace(
            alert_rule(),
            definition_key="missing-reference",
            content={**alert_rule().content, "goal_version_id": "f" * 64},
        )
        with self.assertRaisesRegex(DefinitionNotFoundError, "does not exist"):
            self.store.apply_definition_package(
                [missing], transaction_time=BASE_TIME + timedelta(minutes=2)
            )
        wrong_type = replace(
            alert_rule(),
            definition_key="wrong-reference-type",
            content={**alert_rule().content, "goal_version_id": segment_version},
        )
        with self.assertRaisesRegex(DefinitionNotFoundError, "does not exist"):
            self.store.apply_definition_package(
                [wrong_type], transaction_time=BASE_TIME + timedelta(minutes=2)
            )
        self.assertEqual(self.snapshot(), before)

    def test_sql_constraints_enforce_immutability_and_scoped_identity(self) -> None:
        change = self.store.apply_definition_package(
            [goal()], transaction_time=BASE_TIME
        ).changes[0]
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            with self.store.connect() as db:
                db.execute(
                    "UPDATE analytics_definition_versions SET version=2 WHERE id=?",
                    (change.version.id,),
                )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "cannot be deleted"):
            with self.store.connect() as db:
                db.execute(
                    "DELETE FROM analytics_definition_versions WHERE id=?",
                    (change.version.id,),
                )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            with self.store.connect() as db:
                db.execute(
                    "UPDATE analytics_definition_activations SET scope_key='site:other' WHERE id=?",
                    (change.activation.id,),
                )
        with self.assertRaisesRegex(sqlite3.OperationalError, "no such column"):
            with self.store.connect() as db:
                db.execute(
                    """UPDATE analytics_definition_activations
                          SET retired_at=?
                        WHERE id=?""",
                    (
                        (BASE_TIME + timedelta(minutes=1)).isoformat(),
                        change.activation.id,
                    ),
                )
        self.store.retire_definition(
            DefinitionIdentity(
                "site:example", DefinitionType.GOAL, "qualified-pageview"
            ),
            transaction_time=BASE_TIME + timedelta(minutes=1),
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            with self.store.connect() as db:
                db.execute(
                    """UPDATE analytics_definition_retirements
                          SET retired_at=?
                        WHERE activation_id=?""",
                    (
                        (BASE_TIME + timedelta(minutes=2)).isoformat(),
                        change.activation.id,
                    ),
                )
        with self.store.connect(readonly=True) as db:
            version_columns = {
                row["name"]: row["notnull"]
                for row in db.execute(
                    "PRAGMA table_info(analytics_definition_versions)"
                )
            }
            activation_columns = {
                row["name"]: row["notnull"]
                for row in db.execute(
                    "PRAGMA table_info(analytics_definition_activations)"
                )
            }
            retirement_columns = {
                row["name"]: row["notnull"]
                for row in db.execute(
                    "PRAGMA table_info(analytics_definition_retirements)"
                )
            }
        self.assertEqual(version_columns["id"], 1)
        self.assertEqual(activation_columns["id"], 1)
        self.assertEqual(retirement_columns["id"], 1)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.connect() as db:
                db.execute(
                    """INSERT INTO analytics_definition_versions(
                         id,scope_key,definition_type,definition_key,version,
                         content_hash,content_json,metadata_json,created_at,record_hash
                       ) VALUES(NULL,?,?,?,?,?,?,?,?,?)""",
                    (
                        "site:null",
                        "goal",
                        "null-id",
                        1,
                        "a" * 64,
                        "{}",
                        "{}",
                        BASE_TIME.isoformat(),
                        "b" * 64,
                    ),
                )
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.connect() as db:
                db.execute(
                    """INSERT INTO analytics_definition_activations(
                         id,definition_version_id,scope_key,definition_type,
                         definition_key,activated_at,record_hash
                       ) VALUES(NULL,?,?,?,?,?,?)""",
                    (
                        change.version.id,
                        change.version.scope_key,
                        change.version.definition_type.value,
                        change.version.definition_key,
                        (BASE_TIME + timedelta(minutes=2)).isoformat(),
                        "c" * 64,
                    ),
                )
        chronology = self.store.apply_definition_package(
            [goal(key="chronology")], transaction_time=BASE_TIME
        ).changes[0]
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.connect() as db:
                db.execute(
                    """INSERT INTO analytics_definition_retirements(
                         id,activation_id,scope_key,definition_type,definition_key,
                         activated_at,retired_at,record_hash
                       ) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        "d" * 64,
                        chronology.activation.id,
                        chronology.activation.scope_key,
                        chronology.activation.definition_type.value,
                        chronology.activation.definition_key,
                        chronology.activation.activated_at.isoformat(),
                        (BASE_TIME - timedelta(minutes=1)).isoformat(),
                        "e" * 64,
                    ),
                )
        microsecond_activation = self.store.apply_definition_package(
            [goal(key="microsecond-chronology")],
            transaction_time=BASE_TIME.replace(microsecond=900),
        ).changes[0]
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.connect() as db:
                db.execute(
                    """INSERT INTO analytics_definition_retirements(
                         id,activation_id,scope_key,definition_type,definition_key,
                         activated_at,retired_at,record_hash
                       ) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        "d" * 64,
                        microsecond_activation.activation.id,
                        microsecond_activation.activation.scope_key,
                        microsecond_activation.activation.definition_type.value,
                        microsecond_activation.activation.definition_key,
                        microsecond_activation.activation.activated_at.isoformat(),
                        BASE_TIME.replace(microsecond=100).isoformat(),
                        "e" * 64,
                    ),
                )
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.connect() as db:
                db.execute(
                    """INSERT INTO analytics_definition_retirements(
                         id,activation_id,scope_key,definition_type,definition_key,
                         activated_at,retired_at,record_hash
                       ) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        "d" * 64,
                        chronology.activation.id,
                        chronology.activation.scope_key,
                        chronology.activation.definition_type.value,
                        chronology.activation.definition_key,
                        chronology.activation.activated_at.isoformat(),
                        "2026-07-26 13:00:00+00:00",
                        "e" * 64,
                    ),
                )

    def test_private_or_unbounded_content_is_rejected(self) -> None:
        unsafe_values = [
            {"description": "owner@example.com"},
            {"description": "δοκιμή@παράδειγμα.δοκιμή"},
            {"description": "https://example.com/private"},
            {"description": "//example.com/private"},
            {"description": "Bearer abcdefghijklmnop"},
            {
                "description":
                    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature123"
            },
            {"description": "SELECT email FROM users"},
            {"description": "/Users/operator/private/config.toml"},
            {"description": "prefix=/srv/" + "private/analytics"},
            {"description": '[reports]\nmetric = "google.pageviews"'},
            {"description": '"title" = "friendly"'},
            {"description": "safe label # private operator note"},
            {"description": "safe label #private operator note"},
            {"description": "safe#private operator note"},
            {"description": "e\u0301@example.com"},
            {
                "description":
                    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.--------"
            },
        ]
        for metadata in unsafe_values:
            with self.subTest(metadata=metadata):
                with self.assertRaises(DefinitionValidationError):
                    validate_analytics_definition(goal(metadata=metadata))
        with self.assertRaisesRegex(DefinitionValidationError, "private or prohibited"):
            validate_analytics_definition(
                replace(goal(), content={**goal().content, "recipient": "hidden"})
            )
        with self.assertRaisesRegex(DefinitionValidationError, "unknown fields"):
            validate_analytics_definition(
                replace(goal(), content={**goal().content, "raw_provider_payload": {}})
            )
        with self.assertRaises(DefinitionValidationError):
            validate_analytics_definition(
                replace(goal(), definition_key="x" * 129)
            )
        for prohibited_filter in ("session-id", "sessionId", "sessionid"):
            with self.subTest(prohibited_filter=prohibited_filter):
                with self.assertRaisesRegex(
                    DefinitionValidationError, "private or prohibited"
                ):
                    validate_analytics_definition(
                        replace(
                            goal(),
                            content={
                                **goal().content,
                                "filters": {prohibited_filter: "hidden"},
                            },
                        )
                    )
        unsafe_pattern = replace(
            segment(),
            content={
                **segment().content,
                "expression": {
                    "dimension": "route",
                    "operator": "matches_safe_pattern",
                    "value": "^(a+)+$",
                },
            },
        )
        with self.assertRaisesRegex(DefinitionValidationError, "too complex"):
            validate_analytics_definition(unsafe_pattern)
        safe_pattern = replace(
            segment(),
            content={
                **segment().content,
                "expression": {
                    "dimension": "route",
                    "operator": "matches_safe_pattern",
                    "value": r"/guides/[a-z-]+/",
                },
            },
        )
        validate_analytics_definition(safe_pattern)

    def test_public_definition_construction_rejects_and_cannot_gain_recipients(
        self,
    ) -> None:
        for changes in (
            {"content": {**goal().content, "source": "operator@example.invalid"}},
            {"metadata": {"label": "operator@example.invalid"}},
            {"metadata": []},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(DefinitionValidationError):
                    replace(goal(), **changes)

        mutable_content = {
            **goal().content,
            "provider_bindings": [
                {
                    "metric": "ga4.page-views",
                    "role": "canonical",
                    "source": "google",
                }
            ],
            "site_ids": ["example"],
        }
        mutable_metadata = {"label": "Qualified page view"}
        definition = replace(
            goal(),
            content=mutable_content,
            metadata=mutable_metadata,
        )
        mutable_content["source"] = "operator@example.invalid"
        mutable_content["provider_bindings"][0]["source"] = (
            "operator@example.invalid"
        )
        mutable_metadata["label"] = "operator@example.invalid"
        for surface in (
            repr(definition).encode(),
            repr(asdict(definition)).encode(),
            pickle.dumps(definition),
            pickle.dumps(copy.copy(definition)),
            pickle.dumps(copy.deepcopy(definition)),
            repr(definition.content).encode(),
            repr(definition.metadata).encode(),
        ):
            self.assertNotIn(b"operator@example.invalid", surface)
        with self.assertRaisesRegex(TypeError, "immutable"):
            definition.content["source"] = "operator@example.invalid"
        with self.assertRaisesRegex(TypeError, "immutable"):
            definition.content["provider_bindings"][0]["source"] = (
                "operator@example.invalid"
            )
        with self.assertRaisesRegex(TypeError, "immutable"):
            definition.metadata["label"] = "operator@example.invalid"

    def test_goal_and_alert_conditional_field_matrix_fails_closed(self) -> None:
        validate_analytics_definition(ratio_goal())
        ratio_without_denominator = replace(
            ratio_goal(),
            content={
                key: value
                for key, value in ratio_goal().content.items()
                if key != "denominator"
            },
        )
        sum_with_denominator = replace(
            goal(),
            content={
                **goal().content,
                "denominator": ratio_goal().content["denominator"],
            },
        )
        for invalid in (ratio_without_denominator, sum_with_denominator):
            before = self.snapshot()
            with self.assertRaises(DefinitionValidationError):
                validate_analytics_definition(invalid)
            with self.assertRaises(DefinitionValidationError):
                self.store.apply_definition_package(
                    [invalid],
                    transaction_time=BASE_TIME,
                )
            self.assertEqual(self.snapshot(), before)

        conditional_matrix = {
            "sync_failure": {
                "required": {"source": "google", "threshold": 1},
                "forbidden": {"comparison": 1},
            },
            "stale_data": {
                "required": {"source": "google", "threshold": 60},
                "forbidden": {"comparison": 1},
            },
            "missing_binding": {
                "required": {"source": "google", "threshold": 1},
                "forbidden": {"comparison": 1},
            },
            "coverage_drop": {
                "required": {"threshold": 0.8},
                "forbidden": {"comparison": 1},
            },
            "absolute_threshold": {
                "required": {"threshold": 1},
                "forbidden": {"comparison": 1},
            },
            "relative_change": {
                "required": {"comparison": 0.2},
                "forbidden": {"threshold": 1},
            },
            "zero_after_nonzero": {
                "required": {"comparison": 1},
                "forbidden": {"threshold": 1},
            },
            "cross_provider_divergence": {
                "required": {"threshold": 0.5},
                "forbidden": {"source": "google"},
            },
            "goal_change": {
                "required": {
                    "comparison": 0.2,
                    "goal_version_id": "a" * 64,
                },
                "forbidden": {"source": "google"},
            },
        }
        conditional_fields = {
            "comparison",
            "goal_version_id",
            "source",
            "threshold",
        }
        base = {
            key: value
            for key, value in alert_rule().content.items()
            if key not in conditional_fields
        }
        for index, (rule_type, matrix) in enumerate(
            conditional_matrix.items()
        ):
            valid_content = {
                **base,
                "rule_type": rule_type,
                **matrix["required"],
            }
            valid = replace(
                alert_rule(),
                definition_key=f"conditional-{index}",
                content=valid_content,
            )
            validate_analytics_definition(valid)
            for required_field in matrix["required"]:
                missing = replace(
                    valid,
                    content={
                        key: value
                        for key, value in valid_content.items()
                        if key != required_field
                    },
                )
                with self.subTest(
                    rule_type=rule_type,
                    missing=required_field,
                ):
                    with self.assertRaisesRegex(
                        DefinitionValidationError, "requires fields"
                    ):
                        validate_analytics_definition(missing)
            forbidden = replace(
                valid,
                content={**valid_content, **matrix["forbidden"]},
            )
            with self.subTest(rule_type=rule_type, forbidden=True):
                with self.assertRaisesRegex(
                    DefinitionValidationError, "forbids fields"
                ):
                    validate_analytics_definition(forbidden)

    def test_goal_active_date_bounds_are_canonical_and_ordered(self) -> None:
        for content_updates in (
            {"active_start": "2026-07-01"},
            {"active_end": "2026-07-31"},
            {"active_start": "2026-07-01", "active_end": "2026-07-01"},
            {"active_start": "2026-07-01", "active_end": "2026-07-31"},
        ):
            with self.subTest(content_updates=content_updates):
                validate_analytics_definition(
                    replace(goal(), content={**goal().content, **content_updates})
                )
        for content_updates in (
            {"active_start": "2026-7-01"},
            {"active_start": "2026-02-30"},
            {"active_end": "not-a-date"},
            {"active_start": "2026-07-02", "active_end": "2026-07-01"},
        ):
            with self.subTest(content_updates=content_updates):
                with self.assertRaises(DefinitionValidationError):
                    validate_analytics_definition(
                        replace(goal(), content={**goal().content, **content_updates})
                    )

    def test_quiet_period_clock_boundaries_fail_closed(self) -> None:
        for index, value in enumerate(("00:00", "06:30", "23:59")):
            with self.subTest(value=value):
                definition = replace(
                    alert_rule(),
                    definition_key=f"valid-time-{index}",
                    content={
                        **alert_rule().content,
                        "quiet_periods": [{"start": value, "end": value}],
                    },
                )
                validate_analytics_definition(definition)
                self.store.apply_definition_package(
                    [definition],
                    transaction_time=BASE_TIME,
                )
        invalid_times = (
            "24:00",
            "29:00",
            "23:60",
            "9:00",
            "09:0",
            "00:00:00",
            "",
        )
        for field in ("start", "end"):
            for index, value in enumerate(invalid_times):
                with self.subTest(field=field, value=value):
                    period = {"start": "00:00", "end": "06:00"}
                    period[field] = value
                    invalid = replace(
                        alert_rule(),
                        definition_key=f"invalid-{field}-time-{index}",
                        content={
                            **alert_rule().content,
                            "quiet_periods": [period],
                        },
                    )
                    before = self.snapshot()
                    with self.assertRaisesRegex(
                        DefinitionValidationError, "quiet-period time"
                    ):
                        validate_analytics_definition(invalid)
                    with self.assertRaisesRegex(
                        DefinitionValidationError, "quiet-period time"
                    ):
                        self.store.apply_definition_package(
                            [invalid],
                            transaction_time=BASE_TIME + timedelta(minutes=1),
                        )
                    self.assertEqual(self.snapshot(), before)

    def test_route_list_predicates_validate_every_internal_pathname(self) -> None:
        for dimension in ("route", "landing_route"):
            for operator in ("in", "not_in"):
                with self.subTest(dimension=dimension, operator=operator, valid=True):
                    validate_analytics_definition(
                        replace(
                            segment(),
                            content={
                                **segment().content,
                                "expression": {
                                    "dimension": dimension,
                                    "operator": operator,
                                    "value": ["/", "/guides/", "/guides/two"],
                                },
                            },
                        )
                    )
                for values in (
                    ["not-an-internal-route"],
                    ["/valid", "not-an-internal-route"],
                    ["not-an-internal-route", "/valid"],
                    ["/valid?preview=1"],
                    ["/valid#fragment"],
                    [r"/\evil.example/path"],
                    ["/valid\nbad"],
                    ["/valid\x7fbad"],
                ):
                    with self.subTest(
                        dimension=dimension,
                        operator=operator,
                        values=values,
                    ):
                        before = self.snapshot()
                        with self.assertRaises(DefinitionValidationError):
                            invalid = replace(
                                segment(),
                                definition_key=f"invalid-{dimension}-{operator}",
                                content={
                                    **segment().content,
                                    "expression": {
                                        "dimension": dimension,
                                        "operator": operator,
                                        "value": values,
                                    },
                                },
                            )
                            validate_analytics_definition(invalid)
                            self.store.apply_definition_package(
                                [invalid], transaction_time=BASE_TIME
                            )
                        self.assertEqual(self.snapshot(), before)

    def test_route_scalar_predicates_require_internal_pathnames(self) -> None:
        for dimension in ("route", "landing_route"):
            for operator in (
                "equals",
                "not_equals",
                "starts_with",
                "ends_with",
                "contains",
            ):
                with self.subTest(dimension=dimension, operator=operator, valid=True):
                    validate_analytics_definition(
                        replace(
                            segment(),
                            content={
                                **segment().content,
                                "expression": {
                                    "dimension": dimension,
                                    "operator": operator,
                                    "value": "/guides/",
                                },
                            },
                        )
                    )
                for value in (
                    "not-an-internal-route",
                    "/guides/?preview=1",
                    "/guides/#fragment",
                    r"/\evil.example/path",
                    "/guides/\nbad",
                    "/guides/\x7fbad",
                ):
                    with self.subTest(
                        dimension=dimension,
                        operator=operator,
                        value=value,
                    ):
                        with self.assertRaises(DefinitionValidationError):
                            validate_analytics_definition(
                                replace(
                                    segment(),
                                    content={
                                        **segment().content,
                                        "expression": {
                                            "dimension": dimension,
                                            "operator": operator,
                                            "value": value,
                                        },
                                    },
                                )
                            )

    def test_route_patterns_reject_backslashes_and_control_characters(
        self,
    ) -> None:
        for value in (
            r"/guides/\d+",
            "/guides/\n.+",
            "/guides/\x7f.+",
        ):
            with self.subTest(value=value):
                with self.assertRaises(DefinitionValidationError):
                    validate_analytics_definition(
                        replace(
                            segment(),
                            content={
                                **segment().content,
                                "expression": {
                                    "dimension": "route",
                                    "operator": "matches_safe_pattern",
                                    "value": value,
                                },
                            },
                        )
                    )

    def test_new_semantic_regressions_fail_on_all_stored_use_paths(self) -> None:
        ratio_without_denominator = json.loads(
            validate_analytics_definition(ratio_goal()).content_json
        )
        ratio_without_denominator.pop("denominator")
        threshold_without_threshold = json.loads(
            validate_analytics_definition(
                absolute_threshold_alert()
            ).content_json
        )
        threshold_without_threshold.pop("threshold")
        invalid_cases = (
            (
                alert_rule(),
                {
                    **alert_rule().content,
                    "quiet_periods": [{"start": "29:00", "end": "06:00"}],
                },
            ),
            (
                segment(),
                {
                    **segment().content,
                    "expression": {
                        "dimension": "route",
                        "operator": "in",
                        "value": ["not-an-internal-route"],
                    },
                },
            ),
            (
                ratio_goal(),
                ratio_without_denominator,
            ),
            (
                absolute_threshold_alert(),
                threshold_without_threshold,
            ),
        )
        for index, (valid_definition, invalid_content) in enumerate(invalid_cases):
            with self.subTest(definition_type=valid_definition.definition_type):
                path = Path(self.temporary.name) / f"semantic-regression-{index}.db"
                store = SQLiteMetricStore(path)
                store.initialize()
                change = store.apply_definition_package(
                    [valid_definition],
                    transaction_time=BASE_TIME,
                ).changes[0]
                invalid_json = json.dumps(
                    invalid_content,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                with store.connect() as db:
                    trigger_sql = db.execute(
                        """SELECT sql FROM sqlite_master
                            WHERE type='trigger'
                              AND name='analytics_definition_versions_no_update'"""
                    ).fetchone()[0]
                    db.execute("DROP TRIGGER analytics_definition_versions_no_update")
                    db.execute(
                        "UPDATE analytics_definition_versions SET content_json=? WHERE id=?",
                        (invalid_json, change.version.id),
                    )
                    db.execute(trigger_sql)
                identity = DefinitionIdentity(
                    valid_definition.scope_key,
                    valid_definition.definition_type,
                    valid_definition.definition_key,
                )
                with self.assertRaisesRegex(
                    DefinitionIntegrityError, "semantic validation failed"
                ):
                    store.get_current_definition(identity)
                with store.connect() as db:
                    db.execute(
                        "DROP TRIGGER analytics_definition_versions_no_update"
                    )
                    db.execute(
                        "UPDATE analytics_definition_versions SET content_json=? WHERE id=?",
                        (change.version.content_json, change.version.id),
                    )
                    db.execute(trigger_sql)
                store.retire_definition(
                    identity,
                    transaction_time=BASE_TIME + timedelta(minutes=1),
                )
                with store.connect() as db:
                    db.execute(
                        "DROP TRIGGER analytics_definition_versions_no_update"
                    )
                    db.execute(
                        "UPDATE analytics_definition_versions SET content_json=? WHERE id=?",
                        (invalid_json, change.version.id),
                    )
                    db.execute(trigger_sql)
                for operation in (
                    lambda: store.apply_definition_package(
                        [valid_definition],
                        transaction_time=BASE_TIME + timedelta(minutes=2),
                    ),
                    lambda: store.activate_definition_version(
                        change.version.id,
                        transaction_time=BASE_TIME + timedelta(minutes=2),
                    ),
                    store.verify_definition_integrity,
                ):
                    with self.subTest(operation=operation):
                        with self.assertRaisesRegex(
                            DefinitionIntegrityError, "semantic validation failed"
                        ):
                            operation()
                with self.assertRaisesRegex(ValueError, "definition integrity"):
                    store.backup(
                        Path(self.temporary.name)
                        / f"semantic-regression-{index}-backup.db"
                    )

    def test_subscription_derives_keyed_digest_only_from_recipient_inputs(
        self,
    ) -> None:
        definition = subscription()
        validated = validate_analytics_definition(
            definition,
            recipient_set=("operator@example.invalid",),
            recipient_digest_key=FIXTURE_RECIPIENT_DIGEST_KEY,
        )
        self.assertNotIn("operator@example.invalid", validated.content_json)
        self.assertIn('"recipient_set_id":"', validated.content_json)
        self.assertNotIn("operator@example.invalid", repr(definition))
        self.assertFalse(hasattr(definition, "recipient_set"))
        self.assertFalse(hasattr(definition, "recipient_digest_key"))
        for material in (
            repr(asdict(definition)).encode(),
            pickle.dumps(definition),
            pickle.dumps(copy.copy(definition)),
            pickle.dumps(copy.deepcopy(definition)),
        ):
            self.assertNotIn(b"operator@example.invalid", material)
            self.assertNotIn(FIXTURE_RECIPIENT_DIGEST_KEY, material)
        self.assertFalse(hasattr(analytics_models, "VerifiedRecipientSetIdentifier"))
        self.assertFalse(
            hasattr(analytics_models, "derive_recipient_set_identifier")
        )
        self.assertIsNone(validate_analytics_definition.__closure__)
        chosen_digest = "a" * 64
        self.assertNotIn(
            f'"recipient_set_id":"{chosen_digest}"',
            validated.content_json,
        )
        unverified = replace(
            subscription(),
            content={
                **subscription().content,
                "recipient_set_id": chosen_digest,
            },
        )
        with self.assertRaisesRegex(
            DefinitionValidationError, "must be derived during validation"
        ):
            validate_analytics_definition(unverified)
        with self.assertRaisesRegex(
            DefinitionValidationError, "requires private recipient inputs"
        ):
            validate_analytics_definition(subscription())
        with self.assertRaisesRegex(
            DefinitionValidationError, "digest key"
        ):
            validate_analytics_definition(
                subscription(),
                recipient_set=("operator@example.invalid",),
                recipient_digest_key=b"too-short",
            )
        with self.assertRaisesRegex(
            DefinitionValidationError, "recipient address is invalid"
        ):
            validate_analytics_definition(
                subscription(),
                recipient_set=("not-an-address",),
                recipient_digest_key=FIXTURE_RECIPIENT_DIGEST_KEY,
            )
        for malformed_address in (
            ".operator@example.invalid",
            "operator.@example.invalid",
            "operator..two@example.invalid",
            "operator@example..invalid",
            "operator@example-.invalid",
            "operator@-example.invalid",
            f"operator@{'a' * 64}.invalid",
            "uſer@example.invalid",
            "operator@Kexample.invalid",
        ):
            with self.subTest(malformed_address=malformed_address):
                with self.assertRaisesRegex(
                    DefinitionValidationError, "recipient address is invalid"
                ):
                    validate_analytics_definition(
                        subscription(),
                        recipient_set=(malformed_address,),
                        recipient_digest_key=FIXTURE_RECIPIENT_DIGEST_KEY,
                    )
        validate_analytics_definition(
            subscription(),
            recipient_set=(
                "operator.one+tag@example.invalid",
                f"{'a' * 64}@{'b' * 63}.invalid",
            ),
            recipient_digest_key=FIXTURE_RECIPIENT_DIGEST_KEY,
        )
        with self.assertRaisesRegex(
            DefinitionValidationError, "must not contain duplicates"
        ):
            validate_analytics_definition(
                subscription(),
                recipient_set=(
                    "Operator@example.invalid",
                    "operator@example.invalid",
                ),
                recipient_digest_key=FIXTURE_RECIPIENT_DIGEST_KEY,
            )
        with self.assertRaisesRegex(
            DefinitionValidationError, "recipient address is invalid"
        ):
            validate_analytics_definition(
                subscription(),
                recipient_set=("οperator@example.invalid",),
                recipient_digest_key=FIXTURE_RECIPIENT_DIGEST_KEY,
            )

        class LyingRecipientList(list):
            def __len__(self) -> int:
                return 1

            def __iter__(self):
                for index in range(101):
                    yield f"operator-{index}@example.invalid"

        with self.assertRaisesRegex(
            DefinitionValidationError, "bounded non-empty sequence"
        ):
            validate_analytics_definition(
                subscription(),
                recipient_set=LyingRecipientList(["operator@example.invalid"]),
                recipient_digest_key=FIXTURE_RECIPIENT_DIGEST_KEY,
            )
        with self.assertRaisesRegex(
            DefinitionValidationError,
            "valid only for report_subscription",
        ):
            validate_analytics_definition(
                goal(),
                recipient_set=("operator@example.invalid",),
                recipient_digest_key=FIXTURE_RECIPIENT_DIGEST_KEY,
            )
        stored = self.store.apply_definition_package(
            [subscription()],
            recipient_inputs=subscription_recipient_inputs(),
            transaction_time=BASE_TIME,
        ).changes[0]
        self.assertNotIn("operator@example.invalid", stored.version.content_json)
        self.assertNotIn(
            FIXTURE_RECIPIENT_DIGEST_KEY.hex(),
            stored.version.content_json,
        )
        with self.assertRaisesRegex(ValueError, "absent from the package"):
            self.store.apply_definition_package(
                [goal()],
                recipient_inputs=subscription_recipient_inputs(),
            )

    def test_tampered_current_version_is_validated_before_every_mutation(
        self,
    ) -> None:
        retained = self.store.apply_definition_package(
            [goal()], transaction_time=BASE_TIME
        ).changes[0]
        current = self.store.apply_definition_package(
            [goal(metric="ga4.current-page-views")],
            transaction_time=BASE_TIME + timedelta(minutes=1),
        ).changes[0]
        identity = DefinitionIdentity(
            "site:example",
            DefinitionType.GOAL,
            "qualified-pageview",
        )
        with self.store.connect() as db:
            trigger_sql = db.execute(
                """SELECT sql FROM sqlite_master
                    WHERE type='trigger'
                      AND name='analytics_definition_versions_no_update'"""
            ).fetchone()[0]
            db.execute("DROP TRIGGER analytics_definition_versions_no_update")
            db.execute(
                """UPDATE analytics_definition_versions
                      SET metadata_json='{"label":"tampered current"}'
                    WHERE id=?""",
                (current.version.id,),
            )
            db.execute(trigger_sql)
        before = self.snapshot()
        for operation in (
            lambda: self.store.retire_definition(
                identity,
                transaction_time=BASE_TIME + timedelta(minutes=2),
            ),
            lambda: self.store.apply_definition_package(
                [goal(metric="ga4.replacement-page-views")],
                transaction_time=BASE_TIME + timedelta(minutes=2),
            ),
            lambda: self.store.activate_definition_version(
                retained.version.id,
                transaction_time=BASE_TIME + timedelta(minutes=2),
            ),
        ):
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(
                    DefinitionIntegrityError, "immutable integrity failed"
                ):
                    operation()
                self.assertEqual(self.snapshot(), before)

    def test_activation_history_is_monotonic_and_non_overlapping(self) -> None:
        change = self.store.apply_definition_package(
            [goal()], transaction_time=BASE_TIME
        ).changes[0]
        identity = DefinitionIdentity(
            "site:example",
            DefinitionType.GOAL,
            "qualified-pageview",
        )
        self.store.retire_definition(
            identity,
            transaction_time=BASE_TIME + timedelta(minutes=10),
        )
        before = self.snapshot()
        with self.assertRaisesRegex(
            DefinitionIntegrityError, "overlaps or is non-monotonic"
        ):
            self.store.activate_definition_version(
                change.version.id,
                transaction_time=BASE_TIME + timedelta(minutes=5),
            )
        self.assertEqual(self.snapshot(), before)

        with patch.object(
            SQLiteMetricStore,
            "_validate_activation_chronology",
            return_value=None,
        ):
            self.store.activate_definition_version(
                change.version.id,
                transaction_time=BASE_TIME + timedelta(minutes=5),
            )
        for operation in (
            lambda: self.store.get_current_definition(identity),
            lambda: self.store.apply_definition_package(
                [goal()],
                transaction_time=BASE_TIME + timedelta(minutes=11),
            ),
            lambda: self.store.activate_definition_version(
                change.version.id,
                transaction_time=BASE_TIME + timedelta(minutes=11),
            ),
            lambda: self.store.retire_definition(
                identity,
                transaction_time=BASE_TIME + timedelta(minutes=11),
            ),
            self.store.verify_definition_integrity,
        ):
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(
                    DefinitionIntegrityError,
                    "overlaps or is non-monotonic",
                ):
                    operation()
        with self.assertRaisesRegex(ValueError, "definition integrity"):
            self.store.backup(
                Path(self.temporary.name) / "overlapping-history-backup.db"
            )

    def test_embedded_references_are_authority_invariants(self) -> None:
        goal_change = self.store.apply_definition_package(
            [goal()], transaction_time=BASE_TIME
        ).changes[0]
        referenced_alert = replace(
            alert_rule(),
            content={
                **alert_rule().content,
                "goal_version_id": goal_change.version.id,
            },
        )
        alert_change = self.store.apply_definition_package(
            [referenced_alert],
            transaction_time=BASE_TIME + timedelta(minutes=1),
        ).changes[0]
        with closing(sqlite3.connect(self.store.path)) as db:
            db.execute("PRAGMA foreign_keys=OFF")
            trigger_rows = db.execute(
                """SELECT name,sql FROM sqlite_master
                    WHERE type='trigger'
                      AND name IN (
                        'analytics_definition_activations_no_delete',
                        'analytics_definition_versions_no_delete'
                      )
                    ORDER BY name"""
            ).fetchall()
            for name, _ in trigger_rows:
                db.execute(f"DROP TRIGGER {name}")
            db.execute(
                """DELETE FROM analytics_definition_activations
                    WHERE definition_version_id=?""",
                (goal_change.version.id,),
            )
            db.execute(
                "DELETE FROM analytics_definition_versions WHERE id=?",
                (goal_change.version.id,),
            )
            for _, trigger_sql in trigger_rows:
                db.execute(trigger_sql)
            db.commit()
        identity = DefinitionIdentity(
            referenced_alert.scope_key,
            referenced_alert.definition_type,
            referenced_alert.definition_key,
        )
        for operation in (
            lambda: self.store.get_current_definition(identity),
            lambda: self.store.retire_definition(
                identity,
                transaction_time=BASE_TIME + timedelta(minutes=2),
            ),
            lambda: self.store.activate_definition_version(
                alert_change.version.id,
                transaction_time=BASE_TIME + timedelta(minutes=2),
            ),
            self.store.verify_definition_integrity,
        ):
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(
                    DefinitionIntegrityError, "referenced goal version"
                ):
                    operation()
        with self.assertRaisesRegex(
            DefinitionNotFoundError, "referenced goal version"
        ):
            self.store.apply_definition_package(
                [referenced_alert],
                transaction_time=BASE_TIME + timedelta(minutes=2),
            )
        with self.assertRaisesRegex(ValueError, "definition integrity"):
            self.store.backup(
                Path(self.temporary.name) / "dangling-reference-backup.db"
            )

    def test_definition_integrity_pins_every_schema_enforcement_object(
        self,
    ) -> None:
        self.store.apply_definition_package(
            [goal()], transaction_time=BASE_TIME
        )
        missing = (
            "analytics_definition_activations_one_current",
            "analytics_definition_retirements_no_update",
            "analytics_definition_versions_no_update",
        )
        with self.store.connect() as db:
            for trigger_name in missing:
                db.execute(f"DROP TRIGGER {trigger_name}")
        with self.assertRaisesRegex(
            DefinitionIntegrityError, "schema enforcement"
        ):
            self.store.verify_definition_integrity()
        with self.assertRaisesRegex(ValueError, "definition integrity"):
            self.store.backup(
                Path(self.temporary.name) / "missing-enforcement-backup.db"
            )

    def test_restore_copies_the_single_snapshot_that_was_validated(self) -> None:
        source_store = SQLiteMetricStore(
            Path(self.temporary.name) / "snapshot-source-live.db"
        )
        source_store.initialize()
        source_store.apply_definition_package(
            [goal(metadata={"label": "Validated source"})],
            transaction_time=BASE_TIME,
        )
        source_store.verify_definition_integrity()
        source = source_store.backup(
            Path(self.temporary.name) / "snapshot-source.db"
        )
        replacement = source_store.backup(
            Path(self.temporary.name) / "snapshot-replacement.db"
        )
        with closing(sqlite3.connect(replacement)) as db:
            trigger_sql = db.execute(
                """SELECT sql FROM sqlite_master
                    WHERE type='trigger'
                      AND name='analytics_definition_versions_no_update'"""
            ).fetchone()[0]
            db.execute("DROP TRIGGER analytics_definition_versions_no_update")
            db.execute(
                """UPDATE analytics_definition_versions
                      SET metadata_json='{"label":"replacement after validation"}'"""
            )
            db.execute(trigger_sql)
            db.commit()

        target = SQLiteMetricStore(
            Path(self.temporary.name) / "snapshot-restore-target.db"
        )
        real_connect = sqlite3.connect
        source_uri = f"file:{source.as_posix()}?mode=ro"
        source_connections = 0
        replaced = False

        def replacing_connect(database, *args, **kwargs):
            nonlocal replaced, source_connections
            database_text = os.fspath(database)
            if database_text == source_uri:
                source_connections += 1
            elif (
                not replaced
                and isinstance(database, Path)
                and database.name.startswith(
                    f".{target.path.name}.restore-"
                )
            ):
                os.replace(replacement, source)
                replaced = True
            return real_connect(database, *args, **kwargs)

        with patch(
            "boho_analytics_platform.storage.sqlite3.connect",
            side_effect=replacing_connect,
        ):
            target.restore(source, confirmed=True)
        self.assertTrue(replaced)
        self.assertEqual(source_connections, 1)
        self.assertEqual(
            list(
                target.path.parent.glob(
                    f".{target.path.name}.restore-*"
                )
            ),
            [],
        )
        self.assertEqual(
            target.verify_definition_integrity(),
            {"versions": 1, "activations": 1, "retirements": 0},
        )
        current = target.get_current_definition(
            DefinitionIdentity(
                "site:example",
                DefinitionType.GOAL,
                "qualified-pageview",
            )
        )
        self.assertIsNotNone(current)
        self.assertEqual(
            json.loads(current[0].metadata_json)["label"],
            "Validated source",
        )
        with self.assertRaisesRegex(
            DefinitionIntegrityError, "immutable integrity failed"
        ):
            SQLiteMetricStore(source).verify_definition_integrity()

    def test_restore_rejects_a_schema_newer_than_the_runtime(self) -> None:
        source = self.store.backup(
            Path(self.temporary.name) / "future-schema-source.db"
        )
        with closing(sqlite3.connect(source)) as db:
            db.execute("UPDATE schema_meta SET version=?", (SCHEMA_VERSION + 1,))
            db.commit()
        target = SQLiteMetricStore(
            Path(self.temporary.name) / "future-schema-target.db"
        )
        with self.assertRaisesRegex(ValueError, "outside supported range"):
            target.restore(source, confirmed=True)
        self.assertFalse(target.path.exists())

    def test_activation_and_restore_revalidate_definition_semantics(self) -> None:
        semantic_change, metadata_change = self.store.apply_definition_package(
            [goal(), goal(key="metadata-integrity")],
            transaction_time=BASE_TIME,
        ).changes
        with self.store.connect() as db:
            db.execute("DROP TRIGGER analytics_definition_versions_no_update")
            db.execute(
                "UPDATE analytics_definition_versions SET content_json=? WHERE id=?",
                ('{"metric":"only"}', semantic_change.version.id),
            )
            db.execute(
                "UPDATE analytics_definition_versions SET metadata_json=? WHERE id=?",
                ('{"label":"tampered"}', metadata_change.version.id),
            )
        with self.assertRaisesRegex(
            DefinitionIntegrityError, "semantic validation failed"
        ):
            self.store.activate_definition_version(
                semantic_change.version.id,
                transaction_time=BASE_TIME + timedelta(minutes=1),
            )
        with self.assertRaisesRegex(
            DefinitionIntegrityError, "immutable integrity failed"
        ):
            self.store.activate_definition_version(
                metadata_change.version.id,
                transaction_time=BASE_TIME + timedelta(minutes=1),
            )
        with self.assertRaisesRegex(
            DefinitionIntegrityError, "semantic validation failed"
        ):
            self.store.apply_definition_package(
                [goal()], transaction_time=BASE_TIME + timedelta(minutes=2)
            )
        with self.assertRaisesRegex(
            DefinitionIntegrityError, "semantic validation failed"
        ):
            self.store.get_current_definition(
                DefinitionIdentity(
                    "site:example",
                    DefinitionType.GOAL,
                    "qualified-pageview",
                )
            )
        with self.assertRaisesRegex(
            DefinitionIntegrityError, "immutable integrity failed"
        ):
            self.store.apply_definition_package(
                [goal(key="metadata-integrity")],
                transaction_time=BASE_TIME + timedelta(minutes=2),
            )
        with self.assertRaisesRegex(
            DefinitionIntegrityError, "immutable integrity failed"
        ):
            self.store.get_current_definition(
                DefinitionIdentity(
                    "site:example",
                    DefinitionType.GOAL,
                    "metadata-integrity",
                )
            )
        referenced = replace(
            alert_rule(),
            content={
                **alert_rule().content,
                "goal_version_id": metadata_change.version.id,
            },
        )
        with self.assertRaisesRegex(
            DefinitionIntegrityError, "immutable integrity failed"
        ):
            self.store.apply_definition_package(
                [referenced],
                transaction_time=BASE_TIME + timedelta(minutes=2),
            )

        source_store = SQLiteMetricStore(
            Path(self.temporary.name) / "valid-source.db"
        )
        source_store.initialize()
        source_change = source_store.apply_definition_package(
            [goal()], transaction_time=BASE_TIME
        ).changes[0]
        semantic_backup = source_store.backup(
            Path(self.temporary.name) / "semantic-backup.db"
        )
        with closing(sqlite3.connect(semantic_backup)) as db:
            db.execute("DROP TRIGGER analytics_definition_versions_no_update")
            db.execute(
                "UPDATE analytics_definition_versions SET content_json=? WHERE id=?",
                ('{"metric":"only"}', source_change.version.id),
            )
            db.commit()
        restore_target = SQLiteMetricStore(
            Path(self.temporary.name) / "restore-target.db"
        )
        with self.assertRaisesRegex(ValueError, "definition integrity"):
            restore_target.restore(semantic_backup, confirmed=True)
        self.assertFalse(restore_target.path.exists())

        foreign_key_backup = source_store.backup(
            Path(self.temporary.name) / "foreign-key-backup.db"
        )
        with closing(sqlite3.connect(foreign_key_backup)) as db:
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute("DROP TRIGGER analytics_definition_activations_no_update")
            db.execute(
                """UPDATE analytics_definition_activations
                      SET definition_version_id=?
                    WHERE definition_version_id=?""",
                ("f" * 64, source_change.version.id),
            )
            db.commit()
        with self.assertRaisesRegex(ValueError, "foreign-key"):
            restore_target.restore(foreign_key_backup, confirmed=True)
        self.assertFalse(restore_target.path.exists())

    def test_current_activation_use_revalidates_immutable_record_hash(self) -> None:
        change = self.store.apply_definition_package(
            [goal()], transaction_time=BASE_TIME
        ).changes[0]
        identity = DefinitionIdentity(
            "site:example", DefinitionType.GOAL, "qualified-pageview"
        )
        with self.store.connect() as db:
            db.execute("DROP TRIGGER analytics_definition_activations_no_update")
            db.execute(
                """UPDATE analytics_definition_activations
                      SET record_hash=?
                    WHERE id=?""",
                ("0" * 64, change.activation.id),
            )
        for operation in (
            lambda: self.store.get_current_definition(identity),
            lambda: self.store.apply_definition_package(
                [goal()], transaction_time=BASE_TIME + timedelta(minutes=1)
            ),
            lambda: self.store.activate_definition_version(
                change.version.id,
                transaction_time=BASE_TIME + timedelta(minutes=1),
            ),
            lambda: self.store.retire_definition(
                identity, transaction_time=BASE_TIME + timedelta(minutes=1)
            ),
        ):
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(
                    DefinitionIntegrityError, "activation integrity failed"
                ):
                    operation()

    def test_retirement_history_is_append_only_and_revalidated(self) -> None:
        change = self.store.apply_definition_package(
            [goal()], transaction_time=BASE_TIME
        ).changes[0]
        identity = DefinitionIdentity(
            "site:example", DefinitionType.GOAL, "qualified-pageview"
        )
        self.store.retire_definition(
            identity, transaction_time=BASE_TIME + timedelta(minutes=1)
        )
        with self.store.connect() as db:
            trigger_sql = db.execute(
                """SELECT sql FROM sqlite_master
                    WHERE type='trigger'
                      AND name='analytics_definition_retirements_no_update'"""
            ).fetchone()[0]
            db.execute("DROP TRIGGER analytics_definition_retirements_no_update")
            db.execute(
                """UPDATE analytics_definition_retirements
                      SET record_hash=?
                    WHERE activation_id=?""",
                ("0" * 64, change.activation.id),
            )
            db.execute(trigger_sql)
        for operation in (
            lambda: self.store.get_current_definition(identity),
            lambda: self.store.apply_definition_package(
                [goal()], transaction_time=BASE_TIME + timedelta(minutes=2)
            ),
            lambda: self.store.activate_definition_version(
                change.version.id,
                transaction_time=BASE_TIME + timedelta(minutes=2),
            ),
            self.store.verify_definition_integrity,
        ):
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(
                    DefinitionIntegrityError, "retirement integrity failed"
                ):
                    operation()
        with self.assertRaisesRegex(ValueError, "definition integrity"):
            self.store.backup(
                Path(self.temporary.name) / "retirement-tampered-backup.db"
            )

    def test_duplicate_scoped_keys_and_conflicting_retirement_are_rejected(self) -> None:
        identity = DefinitionIdentity(
            "site:example", DefinitionType.GOAL, "qualified-pageview"
        )
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "duplicate scoped"):
            self.store.apply_definition_package([goal(), goal()])
        with self.assertRaisesRegex(ValueError, "activate and explicitly retire"):
            self.store.apply_definition_package([goal()], retirements=[identity])
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
