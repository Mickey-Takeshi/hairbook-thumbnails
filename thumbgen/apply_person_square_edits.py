"""Apply dashboard per-banner edit requests to a reviewed square manifest.

The dashboard stores each request in the append-only review checklist.  This
command turns those structured requests into a new manifest without touching
Google Sheets or ``thumbnail_override``.  Render and QA the new manifest, then
register it for another human review.

The output must live beside the source manifest so its relative source-image
paths remain immutable and auditable.
"""
from __future__ import annotations

import argparse
import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import person_square
from person_v3 import ManifestError


class EditError(RuntimeError):
    """A dashboard edit cannot be applied safely."""


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EditError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise EditError(f"{path}: root must be an object")
    return value


def _requested_edits(
    status: dict[str, Any],
    manifest_sha256: str,
) -> dict[str, dict[str, Any]]:
    edits: dict[str, dict[str, Any]] = {}
    for asset in status.get("assets") or []:
        review = asset.get("latest_review") or {}
        if review.get("manifest_sha256") != manifest_sha256:
            continue
        if review.get("decision") != "changes_requested":
            continue
        request = (review.get("checklist") or {}).get("edit_request")
        if isinstance(request, dict) and request:
            edits[str(asset.get("asset_id") or "")] = request
    return edits


def _select_source_option(
    asset: dict[str, Any],
    request: dict[str, Any],
) -> dict[str, Any] | None:
    option_id = str(request.get("source_option_id") or "")
    image_url = str(request.get("source_image_url") or "")
    if not option_id and not image_url:
        return None
    for option in asset.get("source_options") or []:
        source = option.get("source") or {}
        if (
            option_id
            and str(option.get("option_id") or "") == option_id
        ) or (
            image_url
            and str(source.get("image_url") or "") == image_url
        ):
            return option
    raise EditError(
        f"{asset.get('asset_id')}: requested source is not an approved option"
    )


def _apply_request(
    asset: dict[str, Any],
    request: dict[str, Any],
) -> list[str]:
    changed: list[str] = []
    option = _select_source_option(asset, request)
    if option:
        preserved_source = {
            key: asset["source"].get(key)
            for key in ("subject_scope", "selection_scope")
            if asset["source"].get(key) is not None
        }
        asset["source"] = {
            **copy.deepcopy(option["source"]),
            **preserved_source,
        }
        asset["image"] = copy.deepcopy(option["image"])
        asset["layout"] = str(option["layout"])
        asset["layout_reason"] = str(option["layout_reason"])
        asset["theme"] = str(option["theme"])
        changed.append("source")

    copy_payload = asset["copy"]
    for field in ("industry", "area", "salon_name", "access", "headline"):
        if field in request and request[field] != copy_payload.get(field):
            copy_payload[field] = copy.deepcopy(request[field])
            changed.append(field)
    copy_payload["cta"] = ""

    if request.get("layout") and request["layout"] != asset.get("layout"):
        asset["layout"] = str(request["layout"])
        asset["layout_reason"] = "管理画面の個別調整で指定"
        changed.append("layout")

    if changed:
        asset["edit_audit"] = {
            "applied_at": datetime.now(timezone.utc).isoformat(),
            "fields": changed,
            "issue_types": list(request.get("issue_types") or []),
            "note": str(request.get("note") or ""),
        }
    return changed


def apply_edits(
    manifest_path: Path,
    status_path: Path,
    output_path: Path,
    only: set[str] | None = None,
) -> dict[str, Any]:
    manifest_path = manifest_path.resolve()
    output_path = output_path.resolve()
    if output_path.parent != manifest_path.parent:
        raise EditError("output manifest must be beside the source manifest")
    manifest = person_square.load_manifest(manifest_path)
    status = _load_object(status_path.resolve())
    manifest_sha256 = person_square.sha256_file(manifest_path)
    requests = _requested_edits(status, manifest_sha256)
    if only:
        requests = {
            asset_id: request
            for asset_id, request in requests.items()
            if asset_id in only
        }
    if not requests:
        raise EditError("no current structured changes_requested reviews found")

    updated = copy.deepcopy(manifest)
    by_id = {
        str(asset["asset_id"]): asset
        for asset in updated["assets"]
    }
    applied: list[dict[str, Any]] = []
    manual: list[dict[str, Any]] = []
    for asset_id, request in requests.items():
        asset = by_id.get(asset_id)
        if not asset:
            raise EditError(f"{asset_id}: asset is not in the manifest")
        fields = _apply_request(asset, request)
        if fields:
            applied.append({"asset_id": asset_id, "fields": fields})
        else:
            manual.append(
                {
                    "asset_id": asset_id,
                    "issue_types": request.get("issue_types") or [],
                    "reason": "具体的な素材・文言・業種・レイアウト変更が未指定",
                }
            )
    if not applied:
        raise EditError("requests contain issue flags only; no renderable edit")

    updated["generated_at"] = datetime.now(timezone.utc).isoformat()
    updated["derived_from_manifest_sha256"] = manifest_sha256
    updated["dashboard_edits"] = applied
    output_path.write_text(
        json.dumps(updated, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        person_square.load_manifest(output_path)
    except ManifestError as exc:
        output_path.unlink(missing_ok=True)
        raise EditError(f"edited manifest is invalid: {exc}") from exc
    return {
        "schema_version": "hairbook.person_square_edit_receipt.v1",
        "source_manifest": str(manifest_path),
        "source_manifest_sha256": manifest_sha256,
        "output_manifest": str(output_path),
        "output_manifest_sha256": person_square.sha256_file(output_path),
        "applied": applied,
        "manual_intervention_required": manual,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--asset-id", action="append", default=[])
    args = parser.parse_args()
    receipt = apply_edits(
        args.manifest,
        args.status,
        args.output,
        set(args.asset_id) or None,
    )
    print(json.dumps(receipt, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
