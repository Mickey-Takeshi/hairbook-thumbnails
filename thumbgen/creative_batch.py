"""Apply and roll back approved Person V3 publish manifests.

This module is deliberately separate from ``enrich.py``.  It only changes
product IDs explicitly frozen in an approved publish manifest and never scans
an output directory for images.

Safety properties:

* production/ready_to_publish manifests only;
* explicit SHA-256 confirmation token;
* optimistic locking against every previous override row;
* full override snapshot before the first write;
* one batched worksheet update, followed by a read-back verification;
* automatic best-effort restore when verification or event logging fails;
* append-only publish/rollback events for later batch rollback.

``plan`` and ``verify`` never write Google Sheets.  ``apply`` and ``rollback``
require ``GOOGLE_SHEETS_KEY_JSON``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from creative_inventory import (
    FEED_ID,
    INVENTORY_SCHEMA,
    OVERRIDE_SCHEMA,
    OVERRIDE_TAB,
)

PUBLISH_SCHEMA = "hairbook.person_v3_publish_manifest.v1"
EXECUTION_PLAN_SCHEMA = "hairbook.person_v3_execution_plan.v1"
RUN_RECORD_SCHEMA = "hairbook.person_v3_publish_run.v1"
EVENT_TAB = "creative_publish_events"
EVENT_HEADERS = [
    "event_id",
    "batch_id",
    "event_type",
    "product_id",
    "salon_id",
    "asset_id",
    "design_version",
    "rollout_scope",
    "old_override_json",
    "new_image_url",
    "new_image_sha256",
    "manifest_sha256",
    "account_scope",
    "occurred_at",
    "actor",
    "reason",
]
OVERRIDE_HEADERS = ["id", "image_link", "hash", "title", "srow", "note"]
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SUPPORTED_DESIGN_VERSIONS = {
    "person_v3_layered_v1",
    "person_square_editorial_v1",
}


class BatchError(RuntimeError):
    """A batch cannot safely proceed."""


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BatchError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise BatchError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BatchError(f"{path} must contain a JSON object")
    return value


def normalize_override_row(row: dict[str, Any] | None) -> dict[str, str]:
    if not row or not str(row.get("id") or "").strip():
        return {}
    return {
        key: str(row.get(key) or "").strip()
        for key in OVERRIDE_HEADERS
    }


def values_to_override_rows(values: list[list[Any]]) -> list[dict[str, str]]:
    if not values:
        raise BatchError("thumbnail_override is empty")
    headers = [str(value).strip() for value in values[0]]
    if headers[:2] != ["id", "image_link"]:
        raise BatchError(
            "thumbnail_override header must start with id,image_link"
        )
    rows: list[dict[str, str]] = []
    for raw in values[1:]:
        padded = list(raw) + [""] * max(0, len(headers) - len(raw))
        row = {
            header: str(padded[index] if index < len(padded) else "").strip()
            for index, header in enumerate(headers)
            if header
        }
        if row.get("id"):
            rows.append(normalize_override_row(row))
    return rows


def validate_publish_manifest(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    if manifest.get("schema_version") != PUBLISH_SCHEMA:
        raise BatchError(
            f"unsupported publish manifest schema: {manifest.get('schema_version')!r}"
        )
    if manifest.get("mode") != "production":
        raise BatchError("publish manifest mode must be production")
    if manifest.get("status") != "ready_to_publish":
        raise BatchError("publish manifest status must be ready_to_publish")
    if manifest.get("account_scope") != "HB_02":
        raise BatchError("account_scope must be HB_02")
    rollout_scope = str(
        manifest.get("rollout_scope")
        or (manifest.get("experiment") or {}).get("rollout_batch")
        or ""
    )
    if rollout_scope not in {"pilot", "wave", "full"}:
        raise BatchError(
            "rollout_scope (or experiment.rollout_batch) must be "
            "pilot, wave, or full"
        )

    entries = manifest.get("entries")
    if not isinstance(entries, list) or not entries:
        raise BatchError("publish manifest entries must be a non-empty list")
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise BatchError(f"entries[{index}] must be an object")
        product_id = str(entry.get("product_id") or "").strip()
        if not product_id:
            raise BatchError(f"entries[{index}] has no product_id")
        if product_id in seen:
            raise BatchError(f"duplicate product_id in manifest: {product_id}")
        seen.add(product_id)
        if entry.get("render_mode") != "complete_banner":
            raise BatchError(
                f"{product_id}: render_mode must be complete_banner"
            )
        if entry.get("design_version") not in SUPPORTED_DESIGN_VERSIONS:
            raise BatchError(
                f"{product_id}: unsupported design_version "
                f"{entry.get('design_version')!r}"
            )
        image_url = str(entry.get("new_image_url") or "")
        if not image_url.startswith("https://"):
            raise BatchError(f"{product_id}: new_image_url must use HTTPS")
        image_sha = str(entry.get("new_image_sha256") or "")
        if not SHA256_RE.fullmatch(image_sha):
            raise BatchError(f"{product_id}: invalid new_image_sha256")
        previous = entry.get("previous_override")
        if not isinstance(previous, dict):
            raise BatchError(f"{product_id}: previous_override must be an object")
    return entries


def validate_full_coverage(
    manifest: dict[str, Any],
    inventory: dict[str, Any],
) -> dict[str, int]:
    """Require every active product to be published or explicitly held."""
    if inventory.get("schema_version") != INVENTORY_SCHEMA:
        raise BatchError("inventory schema is not supported")
    active_ids = {
        str(row.get("product_id") or "")
        for row in inventory.get("rows") or []
        if row.get("eligibility_status") != "excluded_out_of_stock"
        and str(row.get("product_id") or "")
    }
    publish_ids = {
        str(entry.get("product_id") or "")
        for entry in manifest.get("entries") or []
    }
    exclusions = manifest.get("excluded_products") or []
    if not isinstance(exclusions, list):
        raise BatchError("excluded_products must be a list")
    excluded_ids: set[str] = set()
    for item in exclusions:
        product_id = str((item or {}).get("product_id") or "")
        reason = str((item or {}).get("reason") or "").strip()
        if not product_id or not reason:
            raise BatchError(
                "every excluded_products item needs product_id and reason"
            )
        excluded_ids.add(product_id)

    overlap = publish_ids & excluded_ids
    if overlap:
        raise BatchError(
            f"products cannot be both published and excluded: {sorted(overlap)[:5]}"
        )
    missing = active_ids - publish_ids - excluded_ids
    unknown = (publish_ids | excluded_ids) - active_ids
    if missing or unknown:
        raise BatchError(
            "full coverage mismatch: "
            f"missing={len(missing)}, unknown={len(unknown)}"
        )
    return {
        "active_products": len(active_ids),
        "publish_products": len(publish_ids),
        "held_products": len(excluded_ids),
    }


@dataclass(frozen=True)
class PlannedMutation:
    product_id: str
    row_number: int
    previous_override: dict[str, str]
    replacement: dict[str, str]
    entry: dict[str, Any]


def build_mutation_plan(
    manifest: dict[str, Any],
    live_rows: list[dict[str, Any]],
) -> list[PlannedMutation]:
    entries = validate_publish_manifest(manifest)
    normalized = [normalize_override_row(row) for row in live_rows]
    live_map = {row["id"]: row for row in normalized if row}
    row_numbers = {
        row["id"]: index + 2
        for index, row in enumerate(normalized)
        if row
    }
    next_row = len(normalized) + 2
    mutations: list[PlannedMutation] = []
    batch_label = str(
        manifest.get("batch_id")
        or (manifest.get("experiment") or {}).get("experiment_id")
        or "person-v3"
    )

    for entry in entries:
        product_id = str(entry["product_id"])
        expected = normalize_override_row(entry.get("previous_override"))
        actual = live_map.get(product_id, {})
        if actual != expected:
            raise BatchError(
                f"{product_id}: current override changed after preflight "
                f"(expected={expected!r}, actual={actual!r})"
            )
        row_number = row_numbers.get(product_id)
        if row_number is None:
            row_number = next_row
            next_row += 1
        replacement = {
            "id": product_id,
            "image_link": str(entry["new_image_url"]),
            "hash": str(entry["new_image_sha256"]),
            "title": actual.get("title", ""),
            "srow": actual.get("srow", ""),
            "note": f"person-v3 {batch_label}"[:500],
        }
        mutations.append(
            PlannedMutation(
                product_id=product_id,
                row_number=row_number,
                previous_override=actual,
                replacement=replacement,
                entry=entry,
            )
        )
    return mutations


def snapshot_payload(
    rows: list[dict[str, Any]],
    source: str,
    snapshot_at: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": OVERRIDE_SCHEMA,
        "snapshot_at": snapshot_at or datetime.now(timezone.utc).isoformat(),
        "source": source,
        "rows": [
            row
            for row in (normalize_override_row(value) for value in rows)
            if row
        ],
    }


def make_execution_plan(
    manifest: dict[str, Any],
    snapshot: dict[str, Any],
    inventory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if snapshot.get("schema_version") != OVERRIDE_SCHEMA:
        raise BatchError("override snapshot schema is not supported")
    mutations = build_mutation_plan(manifest, snapshot.get("rows") or [])
    coverage = (
        validate_full_coverage(manifest, inventory) if inventory else None
    )
    manifest_sha = canonical_sha256(manifest)
    return {
        "schema_version": EXECUTION_PLAN_SCHEMA,
        "status": "ready",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "account_scope": manifest.get("account_scope"),
        "batch_id": manifest.get("batch_id")
        or (manifest.get("experiment") or {}).get("experiment_id")
        or f"person-v3-{manifest_sha[:12]}",
        "manifest_sha256": manifest_sha,
        "confirmation_sha256": manifest_sha,
        "override_snapshot_sha256": canonical_sha256(snapshot),
        "asset_count": len({m.entry.get("asset_id") for m in mutations}),
        "product_count": len(mutations),
        "salon_count": len(
            {str(m.entry.get("salon_id") or "") for m in mutations}
        ),
        "coverage": coverage,
        "changes": [
            {
                "product_id": mutation.product_id,
                "salon_id": str(mutation.entry.get("salon_id") or ""),
                "asset_id": str(mutation.entry.get("asset_id") or ""),
                "row_number": mutation.row_number,
                "old_image_url": mutation.previous_override.get(
                    "image_link", ""
                ),
                "new_image_url": mutation.replacement["image_link"],
            }
            for mutation in mutations
        ],
    }


def _gspread_client():
    raw = os.environ.get("GOOGLE_SHEETS_KEY_JSON")
    if not raw:
        raise BatchError("GOOGLE_SHEETS_KEY_JSON is required")
    try:
        service_account_info = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BatchError("GOOGLE_SHEETS_KEY_JSON is invalid JSON") from exc
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError as exc:
        raise BatchError("gspread and google-auth are required") from exc
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    credentials = Credentials.from_service_account_info(
        service_account_info,
        scopes=scopes,
    )
    return gspread.authorize(credentials)


def _row_values(row: dict[str, str]) -> list[str]:
    return [row.get(header, "") for header in OVERRIDE_HEADERS]


def _batch_update_rows(
    worksheet: Any,
    rows: Iterable[tuple[int, dict[str, str]]],
) -> None:
    updates = [
        {
            "range": f"A{row_number}:F{row_number}",
            "values": [_row_values(row)],
        }
        for row_number, row in rows
    ]
    if not updates:
        return
    max_row = max(
        int(update["range"].split(":")[0][1:]) for update in updates
    )
    current_rows = int(getattr(worksheet, "row_count", max_row))
    if max_row > current_rows:
        worksheet.add_rows(max_row - current_rows)
    worksheet.batch_update(updates, value_input_option="RAW")


def _ensure_event_worksheet(spreadsheet: Any) -> Any:
    try:
        worksheet = spreadsheet.worksheet(EVENT_TAB)
    except Exception:
        worksheet = spreadsheet.add_worksheet(
            title=EVENT_TAB,
            rows=2000,
            cols=len(EVENT_HEADERS),
        )
        worksheet.update([EVENT_HEADERS], "A1", value_input_option="RAW")
        return worksheet
    values = worksheet.get_all_values()
    if not values:
        worksheet.update([EVENT_HEADERS], "A1", value_input_option="RAW")
    elif values[0] != EVENT_HEADERS:
        raise BatchError(
            f"{EVENT_TAB} header does not match the required schema"
        )
    return worksheet


def _existing_events(event_worksheet: Any) -> list[dict[str, str]]:
    values = event_worksheet.get_all_values()
    if not values:
        return []
    headers = [str(value) for value in values[0]]
    return [
        {
            header: str(row[index] if index < len(row) else "")
            for index, header in enumerate(headers)
        }
        for row in values[1:]
        if row and row[0]
    ]


def _verify_replacements(
    worksheet: Any,
    mutations: list[PlannedMutation],
) -> None:
    actual_rows = values_to_override_rows(worksheet.get_all_values())
    actual_map = {row["id"]: row for row in actual_rows}
    mismatches = [
        mutation.product_id
        for mutation in mutations
        if actual_map.get(mutation.product_id) != mutation.replacement
    ]
    if mismatches:
        raise BatchError(
            f"read-back verification failed for {len(mismatches)} products: "
            f"{mismatches[:5]}"
        )


def _restore_mutations(
    worksheet: Any,
    mutations: list[PlannedMutation],
) -> None:
    rows = []
    for mutation in mutations:
        previous = mutation.previous_override
        rows.append(
            (
                mutation.row_number,
                previous
                if previous
                else {header: "" for header in OVERRIDE_HEADERS},
            )
        )
    _batch_update_rows(worksheet, rows)


def apply_manifest(
    manifest: dict[str, Any],
    confirm_sha256: str,
    actor: str,
    output_dir: Path,
    client: Any | None = None,
) -> dict[str, Any]:
    validate_publish_manifest(manifest)
    manifest_sha = canonical_sha256(manifest)
    if confirm_sha256 != manifest_sha:
        raise BatchError(
            "confirmation SHA-256 does not match the frozen manifest"
        )
    if not actor.strip():
        raise BatchError("actor is required")
    batch_id = str(
        manifest.get("batch_id")
        or (manifest.get("experiment") or {}).get("experiment_id")
        or f"person-v3-{manifest_sha[:12]}"
    )
    client = client or _gspread_client()
    spreadsheet = client.open_by_key(FEED_ID)
    override_worksheet = spreadsheet.worksheet(OVERRIDE_TAB)
    live_rows = values_to_override_rows(override_worksheet.get_all_values())
    mutations = build_mutation_plan(manifest, live_rows)
    snapshot = snapshot_payload(
        live_rows,
        f"google-sheet:{FEED_ID}/{OVERRIDE_TAB}",
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "thumbnail_override_before.json", snapshot)

    event_worksheet = _ensure_event_worksheet(spreadsheet)
    existing = _existing_events(event_worksheet)
    prior_publish = [
        row
        for row in existing
        if row.get("batch_id") == batch_id
        and row.get("event_type") == "publish"
    ]
    if prior_publish:
        raise BatchError(f"batch_id has already been published: {batch_id}")

    occurred_at = datetime.now(timezone.utc).isoformat()
    event_rows: list[list[str]] = []
    for index, mutation in enumerate(mutations, start=1):
        event = {
            "event_id": f"{batch_id}:publish:{index}",
            "batch_id": batch_id,
            "event_type": "publish",
            "product_id": mutation.product_id,
            "salon_id": str(mutation.entry.get("salon_id") or ""),
            "asset_id": str(mutation.entry.get("asset_id") or ""),
            "design_version": str(
                mutation.entry.get("design_version") or ""
            ),
            "rollout_scope": str(
                manifest.get("rollout_scope")
                or (manifest.get("experiment") or {}).get("rollout_batch")
                or ""
            ),
            "old_override_json": json.dumps(
                mutation.previous_override,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "new_image_url": mutation.replacement["image_link"],
            "new_image_sha256": str(
                mutation.entry.get("new_image_sha256") or ""
            ),
            "manifest_sha256": manifest_sha,
            "account_scope": str(manifest.get("account_scope") or ""),
            "occurred_at": occurred_at,
            "actor": actor.strip(),
            "reason": "",
        }
        event_rows.append([event[header] for header in EVENT_HEADERS])

    applied = False
    try:
        _batch_update_rows(
            override_worksheet,
            [
                (mutation.row_number, mutation.replacement)
                for mutation in mutations
            ],
        )
        applied = True
        _verify_replacements(override_worksheet, mutations)
        event_worksheet.append_rows(
            event_rows,
            value_input_option="RAW",
        )
    except Exception as exc:
        if applied:
            try:
                _restore_mutations(override_worksheet, mutations)
            except Exception as restore_exc:
                raise BatchError(
                    "publish failed and automatic restore also failed: "
                    f"publish={exc!r}, restore={restore_exc!r}"
                ) from restore_exc
        raise BatchError(
            f"publish failed; previous override was restored: {exc!r}"
        ) from exc

    after_rows = values_to_override_rows(override_worksheet.get_all_values())
    after_snapshot = snapshot_payload(
        after_rows,
        f"google-sheet:{FEED_ID}/{OVERRIDE_TAB}",
    )
    write_json(output_dir / "thumbnail_override_after.json", after_snapshot)
    record = {
        "schema_version": RUN_RECORD_SCHEMA,
        "status": "published",
        "batch_id": batch_id,
        "account_scope": manifest.get("account_scope"),
        "manifest_sha256": manifest_sha,
        "published_at": occurred_at,
        "actor": actor.strip(),
        "product_count": len(mutations),
        "salon_count": len(
            {str(item.entry.get("salon_id") or "") for item in mutations}
        ),
        "before_snapshot_sha256": canonical_sha256(snapshot),
        "after_snapshot_sha256": canonical_sha256(after_snapshot),
    }
    write_json(output_dir / "publish_run.json", record)
    return record


def rollback_batch(
    batch_id: str,
    confirm_batch_id: str,
    actor: str,
    reason: str,
    output_dir: Path,
    client: Any | None = None,
) -> dict[str, Any]:
    if not batch_id or confirm_batch_id != batch_id:
        raise BatchError("confirm_batch_id must exactly match batch_id")
    if not actor.strip() or not reason.strip():
        raise BatchError("actor and rollback reason are required")
    client = client or _gspread_client()
    spreadsheet = client.open_by_key(FEED_ID)
    override_worksheet = spreadsheet.worksheet(OVERRIDE_TAB)
    event_worksheet = _ensure_event_worksheet(spreadsheet)
    events = _existing_events(event_worksheet)
    publishes = [
        row
        for row in events
        if row.get("batch_id") == batch_id
        and row.get("event_type") == "publish"
    ]
    if not publishes:
        raise BatchError(f"no publish events found for batch {batch_id}")
    if any(
        row.get("batch_id") == batch_id
        and row.get("event_type") == "rollback"
        for row in events
    ):
        raise BatchError(f"batch has already been rolled back: {batch_id}")

    live_rows = values_to_override_rows(override_worksheet.get_all_values())
    live_map = {row["id"]: row for row in live_rows}
    row_numbers = {
        row["id"]: index + 2 for index, row in enumerate(live_rows)
    }
    restore_rows: list[tuple[int, dict[str, str]]] = []
    for event in publishes:
        product_id = event["product_id"]
        current = live_map.get(product_id, {})
        if current.get("image_link") != event.get("new_image_url"):
            raise BatchError(
                f"{product_id}: current override no longer matches this batch"
            )
        try:
            previous = normalize_override_row(
                json.loads(event.get("old_override_json") or "{}")
            )
        except json.JSONDecodeError as exc:
            raise BatchError(
                f"{product_id}: invalid old_override_json in event log"
            ) from exc
        row_number = row_numbers.get(product_id)
        if row_number is None:
            raise BatchError(f"{product_id}: current override row is missing")
        restore_rows.append(
            (
                row_number,
                previous
                if previous
                else {header: "" for header in OVERRIDE_HEADERS},
            )
        )

    before = snapshot_payload(
        live_rows,
        f"google-sheet:{FEED_ID}/{OVERRIDE_TAB}",
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "thumbnail_override_before_rollback.json", before)
    _batch_update_rows(override_worksheet, restore_rows)

    actual_rows = values_to_override_rows(override_worksheet.get_all_values())
    actual_map = {row["id"]: row for row in actual_rows}
    for event in publishes:
        product_id = event["product_id"]
        previous = normalize_override_row(
            json.loads(event.get("old_override_json") or "{}")
        )
        if actual_map.get(product_id, {}) != previous:
            raise BatchError(
                f"rollback read-back verification failed for {product_id}"
            )

    occurred_at = datetime.now(timezone.utc).isoformat()
    rollback_rows = []
    for index, event in enumerate(publishes, start=1):
        value = {
            "event_id": f"{batch_id}:rollback:{index}",
            "batch_id": batch_id,
            "event_type": "rollback",
            "product_id": event["product_id"],
            "salon_id": event.get("salon_id", ""),
            "asset_id": event.get("asset_id", ""),
            "design_version": event.get("design_version", ""),
            "rollout_scope": event.get("rollout_scope", ""),
            "old_override_json": event.get("old_override_json", "{}"),
            "new_image_url": event.get("new_image_url", ""),
            "new_image_sha256": event.get("new_image_sha256", ""),
            "manifest_sha256": event.get("manifest_sha256", ""),
            "account_scope": event.get("account_scope", ""),
            "occurred_at": occurred_at,
            "actor": actor.strip(),
            "reason": reason.strip(),
        }
        rollback_rows.append([value[header] for header in EVENT_HEADERS])
    event_worksheet.append_rows(rollback_rows, value_input_option="RAW")
    after = snapshot_payload(
        actual_rows,
        f"google-sheet:{FEED_ID}/{OVERRIDE_TAB}",
    )
    write_json(output_dir / "thumbnail_override_after_rollback.json", after)
    record = {
        "schema_version": RUN_RECORD_SCHEMA,
        "status": "rolled_back",
        "batch_id": batch_id,
        "rolled_back_at": occurred_at,
        "actor": actor.strip(),
        "reason": reason.strip(),
        "product_count": len(publishes),
        "before_snapshot_sha256": canonical_sha256(before),
        "after_snapshot_sha256": canonical_sha256(after),
    }
    write_json(output_dir / "rollback_run.json", record)
    return record


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Safely plan, apply, verify, or roll back Person V3 batches."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser(
        "plan",
        help="Build a non-writing execution plan and confirmation SHA-256",
    )
    plan_parser.add_argument("--publish-manifest", type=Path, required=True)
    plan_parser.add_argument("--override-snapshot", type=Path, required=True)
    plan_parser.add_argument("--inventory", type=Path)
    plan_parser.add_argument("--output", type=Path, required=True)

    apply_parser = subparsers.add_parser(
        "apply",
        help="Apply an approved manifest to thumbnail_override",
    )
    apply_parser.add_argument("--publish-manifest", type=Path, required=True)
    apply_parser.add_argument("--confirm-sha256", required=True)
    apply_parser.add_argument("--actor", required=True)
    apply_parser.add_argument("--output-dir", type=Path, required=True)

    rollback_parser = subparsers.add_parser(
        "rollback",
        help="Restore every product in a previously published batch",
    )
    rollback_parser.add_argument("--batch-id", required=True)
    rollback_parser.add_argument("--confirm-batch-id", required=True)
    rollback_parser.add_argument("--actor", required=True)
    rollback_parser.add_argument("--reason", required=True)
    rollback_parser.add_argument("--output-dir", type=Path, required=True)

    args = parser.parse_args()
    try:
        if args.command == "plan":
            manifest = load_json(args.publish_manifest)
            snapshot = load_json(args.override_snapshot)
            inventory = load_json(args.inventory) if args.inventory else None
            plan = make_execution_plan(manifest, snapshot, inventory)
            write_json(args.output, plan)
            print(json.dumps(plan, ensure_ascii=False))
        elif args.command == "apply":
            record = apply_manifest(
                load_json(args.publish_manifest),
                args.confirm_sha256,
                args.actor,
                args.output_dir,
            )
            print(json.dumps(record, ensure_ascii=False))
        else:
            record = rollback_batch(
                args.batch_id,
                args.confirm_batch_id,
                args.actor,
                args.reason,
                args.output_dir,
            )
            print(json.dumps(record, ensure_ascii=False))
    except BatchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
