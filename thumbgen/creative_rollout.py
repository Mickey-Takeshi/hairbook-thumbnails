"""QA, review, preflight, and rollback planning for person-first V3 assets.

This module is deliberately non-publishing. It produces immutable evidence and
dry-run manifests, but never writes Google Sheets or changes thumbnail_override.

Commands:
    qa           Automated file/source/copy checks.
    init-review  Human-review JSON template and side-by-side HTML.
    preflight    Gate A/B/C validation plus publish/rollback manifests.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image

from improve import assess
import person_square
import person_v3
from person_v3 import ManifestError, _canonical_sha, sha256_file

QA_SCHEMA = "hairbook.person_v3_qa.v1"
REVIEW_SCHEMA = "hairbook.person_v3_review.v1"
SQUARE_QA_SCHEMA = "hairbook.person_square_qa.v1"
SQUARE_REVIEW_SCHEMA = "hairbook.person_square_review.v1"
OVERRIDE_SNAPSHOT_SCHEMA = "hairbook.thumbnail_override_snapshot.v1"
PREFLIGHT_SCHEMA = "hairbook.person_v3_preflight.v1"
PUBLISH_SCHEMA = "hairbook.person_v3_publish_manifest.v1"
ROLLBACK_SCHEMA = "hairbook.person_v3_rollback_manifest.v1"
CHECKLIST_VERSION = "person_v3_pre_publish_v1"
SQUARE_CHECKLIST_VERSION = "person_square_pre_publish_v1"

MANUAL_CHECKS = [
    ("source_match", "対象サロン／スタッフの人物画像である"),
    ("not_treatment_scene", "商品・器具・店内・手元だけ／施術中の画像ではない"),
    ("person_unchanged", "顔・髪型・髪色・髪の長さ・体型が不自然に変わっていない"),
    ("safe_crop", "顔・髪が文字、CTA、Meta上の見切れで損なわれない"),
    ("copy_matches_landing", "店舗名・エリア・アクセス・訴求が着地先と一致する"),
    ("no_misleading_claim", "人物を施術実績だと過度に断定する表現がない"),
    ("mobile_readable", "360px幅の実表示で店名・アクセス・主訴求・CTAを判読できる"),
    ("no_unwanted_text", "求人・価格・他店舗情報・第三者ロゴが残っていない"),
    ("rollback_identified", "現在画像と直前overrideの復元先が確認できる"),
]

SQUARE_MANUAL_CHECKS = [
    ("source_match", "対象サロンの商品・人物画像である"),
    ("not_treatment_scene", "施術中、器具・薬剤、店内・看板だけの画像ではない"),
    ("person_unchanged", "人物・顔・髪型・髪色が不自然に変わっていない"),
    ("safe_crop", "人物またはネイルの主役が文字や見切れで損なわれない"),
    ("copy_matches_landing", "業種・店舗名・エリア・アクセス・訴求が着地先と一致する"),
    ("gender_copy_match", "人物の見え方に対して訴求が性別を限定しすぎていない"),
    ("area_name_glanceable", "一覧表示でもエリアと店舗名を一目で判読できる"),
    ("no_unwanted_text", "元写真の文字・求人・価格・ロゴが残っていない"),
    ("mask_or_service_visible", "マスクで顔が隠れず、ネイル商材では爪が確認できる"),
    ("cta_removed", "CTAボタンが表示されていない"),
    ("rollback_identified", "現在画像と直前overrideの復元先が確認できる"),
]


class GateError(RuntimeError):
    """One or more mandatory rollout gates failed."""


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GateError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise GateError(f"{path}: JSON root must be an object")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _load_manifest(path: Path) -> dict[str, Any]:
    probe = _load_json(path)
    design_version = str(probe.get("design_version") or "")
    if design_version == person_square.DESIGN_VERSION:
        return person_square.load_manifest(path)
    if design_version == person_v3.DESIGN_VERSION:
        return person_v3.load_manifest(path)
    raise GateError(f"unsupported design_version: {design_version!r}")


def _creative_spec(manifest: dict[str, Any]) -> dict[str, Any]:
    if manifest.get("design_version") == person_square.DESIGN_VERSION:
        return {
            "design_version": person_square.DESIGN_VERSION,
            "render_schema": person_square.RENDER_SCHEMA_VERSION,
            "qa_schema": SQUARE_QA_SCHEMA,
            "review_schema": SQUARE_REVIEW_SCHEMA,
            "checklist_version": SQUARE_CHECKLIST_VERSION,
            "output_size": (person_square.W, person_square.H),
            "preview_size": (
                person_square.MOBILE_W,
                person_square.MOBILE_H,
            ),
            "review_title": "人物優先・正方形版",
            "manual_checks": SQUARE_MANUAL_CHECKS,
        }
    return {
        "design_version": person_v3.DESIGN_VERSION,
        "render_schema": "hairbook.person_v3_render_index.v1",
        "qa_schema": QA_SCHEMA,
        "review_schema": REVIEW_SCHEMA,
        "checklist_version": CHECKLIST_VERSION,
        "output_size": (person_v3.W, person_v3.H),
        "preview_size": (person_v3.MOBILE_W, person_v3.MOBILE_H),
        "review_title": "人物画像版V3",
        "manual_checks": MANUAL_CHECKS,
    }


def _check(
    checks: list[dict[str, Any]],
    name: str,
    passed: bool,
    detail: str,
    severity: str = "fail",
) -> None:
    checks.append(
        {
            "name": name,
            "status": "pass" if passed else severity,
            "detail": detail,
        }
    )


def run_qa(
    manifest_path: Path,
    render_index_path: Path,
    output_path: Path,
    max_bytes: int = 8 * 1024 * 1024,
    min_source_short_side: int = 720,
) -> dict[str, Any]:
    manifest_path = manifest_path.resolve()
    render_index_path = render_index_path.resolve()
    manifest = _load_manifest(manifest_path)
    spec = _creative_spec(manifest)
    index = _load_json(render_index_path)
    if index.get("schema_version") != spec["render_schema"]:
        raise GateError("unsupported render index schema")

    manifest_hash = sha256_file(manifest_path)
    manifest_assets = {
        str(asset["asset_id"]): asset for asset in manifest["assets"]
    }
    index_assets = {
        str(asset.get("asset_id")): asset for asset in index.get("assets") or []
    }
    qa_assets: list[dict[str, Any]] = []
    all_output_hashes: dict[str, str] = {}

    for asset_id, asset in manifest_assets.items():
        checks: list[dict[str, Any]] = []
        record = index_assets.get(asset_id)
        _check(
            checks,
            "render_record",
            record is not None,
            "render record exists" if record else "render record is missing",
        )
        if record is None:
            qa_assets.append(
                {
                    "asset_id": asset_id,
                    "status": "fail",
                    "checks": checks,
                    "manual_checks_required": [
                        key for key, _ in spec["manual_checks"]
                    ],
                }
            )
            continue

        _check(
            checks,
            "design_version",
            record.get("design_version") == spec["design_version"],
            (
                f"render={record.get('design_version')!r}, "
                f"expected={spec['design_version']!r}"
            ),
        )
        _check(
            checks,
            "render_mode",
            record.get("render_mode") == "complete_banner",
            f"render_mode={record.get('render_mode')!r}",
        )
        _check(
            checks,
            "manifest_hash",
            record.get("manifest_sha256") == manifest_hash
            and index.get("manifest_sha256") == manifest_hash,
            "render index and asset record match the current manifest",
        )

        expected_source_hash = str(
            (asset.get("source") or {}).get("sha256") or ""
        ).lower()
        source_path = Path(str(record.get("source_path") or ""))
        source_exists = source_path.is_file()
        _check(
            checks,
            "source_exists",
            source_exists,
            str(source_path),
        )
        if source_exists:
            actual_source_hash = sha256_file(source_path)
            _check(
                checks,
                "source_hash",
                actual_source_hash == expected_source_hash
                and record.get("source_sha256") == expected_source_hash,
                f"expected={expected_source_hash}, actual={actual_source_hash}",
            )
            try:
                with Image.open(source_path) as source_image:
                    source_width, source_height = source_image.size
            except OSError as exc:
                source_width = source_height = 0
                _check(checks, "source_decodable", False, repr(exc))
            else:
                _check(checks, "source_decodable", True, "source image opens")
                _check(
                    checks,
                    "source_resolution",
                    min(source_width, source_height) >= min_source_short_side,
                    f"{source_width}x{source_height}, minimum short side={min_source_short_side}",
                )

        expected_copy_hash = _canonical_sha(asset["copy"])
        _check(
            checks,
            "copy_hash",
            record.get("copy_sha256") == expected_copy_hash,
            f"expected={expected_copy_hash}, render={record.get('copy_sha256')}",
        )

        output_path_value = Path(str(record.get("output_path") or ""))
        output_exists = output_path_value.is_file()
        _check(checks, "output_exists", output_exists, str(output_path_value))
        if output_exists:
            output_hash = sha256_file(output_path_value)
            _check(
                checks,
                "output_hash",
                output_hash == record.get("output_sha256"),
                f"record={record.get('output_sha256')}, actual={output_hash}",
            )
            prior = all_output_hashes.get(output_hash)
            _check(
                checks,
                "output_not_duplicated",
                prior is None or prior == asset_id,
                "unique output"
                if prior is None
                else f"same output bytes as {prior}",
            )
            all_output_hashes[output_hash] = asset_id
            _check(
                checks,
                "file_size",
                output_path_value.stat().st_size <= max_bytes,
                f"{output_path_value.stat().st_size} bytes, maximum={max_bytes}",
            )
            try:
                with Image.open(output_path_value) as output_image:
                    output_image.load()
                    output_size = output_image.size
                    output_format = output_image.format
                    exif_count = len(output_image.getexif())
                    icc = output_image.info.get("icc_profile")
                    image_assessment = assess(output_image)
            except OSError as exc:
                _check(checks, "output_decodable", False, repr(exc))
            else:
                _check(checks, "output_decodable", True, "output image opens")
                _check(
                    checks,
                    "dimensions",
                    output_size == spec["output_size"],
                    f"{output_size[0]}x{output_size[1]}",
                )
                _check(
                    checks,
                    "jpeg",
                    output_format == "JPEG",
                    f"format={output_format}",
                )
                _check(
                    checks,
                    "exif_removed",
                    exif_count == 0,
                    f"Exif tags={exif_count}",
                )
                _check(
                    checks,
                    "color_space",
                    output_image.mode == "RGB",
                    f"mode={output_image.mode}; ICC={'embedded' if icc else 'implicit sRGB'}",
                )
                _check(
                    checks,
                    "image_information",
                    image_assessment.get("verdict") != "broken",
                    json.dumps(image_assessment, ensure_ascii=False),
                )

        preview_path = Path(str(record.get("preview_path") or ""))
        preview_exists = preview_path.is_file()
        _check(checks, "mobile_preview_exists", preview_exists, str(preview_path))
        if preview_exists:
            try:
                with Image.open(preview_path) as preview_image:
                    preview_size = preview_image.size
            except OSError as exc:
                _check(checks, "mobile_preview_decodable", False, repr(exc))
            else:
                _check(
                    checks,
                    "mobile_preview_dimensions",
                    preview_size == spec["preview_size"],
                    f"{preview_size[0]}x{preview_size[1]}",
                )

        if spec["design_version"] == person_square.DESIGN_VERSION:
            source_vision = (asset.get("source") or {}).get("vision") or {}
            copy_payload = asset.get("copy") or {}
            is_nail_service = (
                copy_payload.get("industry") in {"nail", "eye_nail"}
                and source_vision.get("nail_service_approved") is True
            )
            _check(
                checks,
                "source_person_score",
                is_nail_service
                or float(source_vision.get("person_score") or 0) >= 0.30,
                (
                    "nail service-result exception"
                    if is_nail_service
                    else f"person_score={source_vision.get('person_score')}"
                ),
            )
            _check(
                checks,
                "source_not_treatment_scene",
                float(source_vision.get("treatment_risk") or 0) <= 0.72
                and not source_vision.get("treatment_flags"),
                (
                    f"treatment_risk={source_vision.get('treatment_risk')}, "
                    f"flags={source_vision.get('treatment_flags') or []}"
                ),
            )
            copy_text = json.dumps(copy_payload, ensure_ascii=False)
            _check(
                checks,
                "copy_has_no_html",
                not bool(person_square.HTML_RE.search(copy_text)),
                "copy is plain text",
            )
            access_lines = copy_payload.get("access") or []
            _check(
                checks,
                "access_present",
                bool(access_lines)
                and not any(
                    "アクセスを確認" in str(line)
                    for line in access_lines
                ),
                " / ".join(str(line) for line in access_lines),
            )
            _check(
                checks,
                "access_no_zero_minutes",
                not any(
                    re.search(
                        r"徒歩\s*0\s*(?:分|秒)",
                        str(line),
                    )
                    for line in access_lines
                ),
                " / ".join(str(line) for line in access_lines),
            )
            _check(
                checks,
                "source_no_baked_text",
                (
                    source_vision.get("text_after_crop_clear") is True
                    if is_nail_service
                    else not source_vision.get("text_flags")
                ),
                (
                    "nail crop is clear"
                    if is_nail_service
                    else (
                        "flags="
                        f"{source_vision.get('text_flags') or []}; "
                        "recognized="
                        f"{source_vision.get('recognized_text') or []}"
                    )
                ),
            )
            _check(
                checks,
                "cta_removed",
                not str(copy_payload.get("cta") or "").strip(),
                "CTA is omitted",
            )
            headline_text = " ".join(
                str(line)
                for line in copy_payload.get("headline") or []
            )
            gendered_phrases = (
                "美髪",
                "肌まできれい",
                "今の私",
                "大人女性",
                "女性らし",
            )
            _check(
                checks,
                "general_hair_gender_neutral",
                copy_payload.get("industry") != "hair"
                or not any(
                    phrase in headline_text
                    for phrase in gendered_phrases
                ),
                headline_text,
            )
            header_metrics = record.get("header_metrics") or {}
            _check(
                checks,
                "area_and_salon_legible",
                int(header_metrics.get("area_font_size") or 0) >= 24
                and int(
                    header_metrics.get("salon_name_font_size") or 0
                )
                >= 32
                and int(header_metrics.get("salon_name_lines") or 0)
                in {1, 2},
                json.dumps(header_metrics, ensure_ascii=False),
            )

        status = (
            "fail"
            if any(item["status"] == "fail" for item in checks)
            else "warn"
            if any(item["status"] == "warn" for item in checks)
            else "pass"
        )
        qa_assets.append(
            {
                "asset_id": asset_id,
                "salon_id": str(asset.get("salon_id") or ""),
                "status": status,
                "checks": checks,
                "manual_checks_required": [
                    key for key, _ in spec["manual_checks"]
                ],
            }
        )

    missing_from_manifest = sorted(set(index_assets) - set(manifest_assets))
    overall_status = (
        "fail"
        if missing_from_manifest
        or any(asset["status"] == "fail" for asset in qa_assets)
        else "warn"
        if any(asset["status"] == "warn" for asset in qa_assets)
        else "pass"
    )
    payload = {
        "schema_version": spec["qa_schema"],
        "checklist_version": spec["checklist_version"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_hash,
        "render_index_path": str(render_index_path),
        "render_index_sha256": sha256_file(render_index_path),
        "overall_status": overall_status,
        "unexpected_render_records": missing_from_manifest,
        "assets": qa_assets,
        "summary": {
            "total": len(qa_assets),
            "pass": sum(asset["status"] == "pass" for asset in qa_assets),
            "warn": sum(asset["status"] == "warn" for asset in qa_assets),
            "fail": sum(asset["status"] == "fail" for asset in qa_assets),
        },
    }
    _write_json(output_path, payload)
    return payload


def _review_html(
    manifest: dict[str, Any],
    render_index: dict[str, Any],
    qa: dict[str, Any],
    review: dict[str, Any],
) -> str:
    spec = _creative_spec(manifest)
    manifest_map = {
        str(asset["asset_id"]): asset for asset in manifest["assets"]
    }
    render_map = {
        str(asset["asset_id"]): asset
        for asset in render_index.get("assets") or []
    }
    qa_map = {
        str(asset["asset_id"]): asset for asset in qa.get("assets") or []
    }
    cards = []
    for item in review["assets"]:
        asset_id = str(item["asset_id"])
        asset = manifest_map[asset_id]
        record = render_map[asset_id]
        qa_item = qa_map[asset_id]
        source_uri = Path(record["source_path"]).resolve().as_uri()
        output_uri = Path(record["output_path"]).resolve().as_uri()
        preview_uri = Path(record["preview_path"]).resolve().as_uri()
        output_label = (
            f"{record.get('width', spec['output_size'][0])}×"
            f"{record.get('height', spec['output_size'][1])}"
        )
        preview_label = (
            f"{record.get('preview_width', spec['preview_size'][0])}×"
            f"{record.get('preview_height', spec['preview_size'][1])}"
        )
        checklist = "".join(
            f"""
            <label class="check">
              <input type="checkbox" data-key="{html.escape(key)}">
              <span>{html.escape(label)}</span>
            </label>
            """
            for key, label in spec["manual_checks"]
        )
        copy = asset["copy"]
        cards.append(
            f"""
            <article class="card" data-asset-id="{html.escape(asset_id)}">
              <header>
                <div>
                  <p class="eyebrow">{html.escape(str(copy["area"]))}</p>
                  <h2>{html.escape(str(copy["salon_name"]))}</h2>
                </div>
                <span class="status {html.escape(qa_item["status"])}">AUTO QA {html.escape(qa_item["status"].upper())}</span>
              </header>
              <div class="images">
                <figure><img src="{html.escape(source_uri)}"><figcaption>元source</figcaption></figure>
                <figure><img src="{html.escape(output_uri)}"><figcaption>完成 {html.escape(output_label)}</figcaption></figure>
                <figure class="mobile"><img src="{html.escape(preview_uri)}"><figcaption>{html.escape(preview_label)}</figcaption></figure>
              </div>
              <div class="copy">
                <b>アクセス</b> {html.escape(" / ".join(copy["access"]))}<br>
                <b>訴求</b> {html.escape(" / ".join(copy["headline"]))}<br>
                <b>CTA</b> {html.escape(str(copy["cta"]))}
              </div>
              <div class="checks">{checklist}</div>
              <div class="decision">
                <label>判定
                  <select>
                    <option value="review_pending">未判定</option>
                    <option value="approved_for_dry_run">dry-run承認</option>
                    <option value="approved">本番承認</option>
                    <option value="rejected">却下</option>
                  </select>
                </label>
                <label>コメント<input class="notes" type="text"></label>
              </div>
            </article>
            """
        )

    initial = json.dumps(review, ensure_ascii=False).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(spec["review_title"])} 事前チェック</title>
<style>
:root{{--ink:#221c15;--paper:#f5f1e8;--gold:#b08d57;--line:#d7cec0}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--paper);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans",sans-serif}}
.top{{position:sticky;top:0;z-index:5;display:flex;gap:16px;align-items:center;padding:16px 28px;background:rgba(245,241,232,.96);border-bottom:1px solid var(--line);backdrop-filter:blur(10px)}}
.top h1{{font-size:20px;margin:0 auto 0 0}} .top input{{height:40px;padding:0 12px}} button{{height:42px;border:0;border-radius:10px;padding:0 18px;background:var(--ink);color:white;font-weight:700;cursor:pointer}}
main{{max-width:1500px;margin:28px auto;padding:0 24px 80px;display:grid;gap:24px}}
.card{{background:white;border:1px solid var(--line);border-radius:20px;padding:22px;box-shadow:0 8px 28px rgba(34,28,21,.06)}}
.card header{{display:flex;justify-content:space-between;align-items:start;margin-bottom:18px}} h2{{margin:4px 0 0;font-family:"Hiragino Mincho ProN",serif;font-size:30px}} .eyebrow{{margin:0;color:var(--gold);font-weight:700}}
.status{{border-radius:999px;padding:8px 12px;font-size:12px;font-weight:800}} .status.pass{{background:#e7f3e5;color:#276128}} .status.fail{{background:#fde8e7;color:#9c2320}} .status.warn{{background:#fff2d4;color:#76520b}}
.images{{display:grid;grid-template-columns:1fr 1fr 360px;gap:16px;align-items:start}} figure{{margin:0}} img{{display:block;width:100%;max-height:620px;object-fit:contain;background:#eee;border-radius:12px}} .mobile img{{width:360px;aspect-ratio:{spec["preview_size"][0]}/{spec["preview_size"][1]};object-fit:contain}} figcaption{{font-size:13px;color:#756a5c;margin-top:7px}}
.copy{{margin:18px 0;padding:14px 16px;background:#faf8f3;border-radius:12px;line-height:1.8}} .checks{{display:grid;grid-template-columns:1fr 1fr;gap:8px 18px}} .check{{display:flex;gap:9px;align-items:start;padding:9px;border-radius:9px}} .check:has(input:checked){{background:#edf6eb}} .check input{{width:18px;height:18px}}
.decision{{display:grid;grid-template-columns:260px 1fr;gap:16px;margin-top:18px;padding-top:18px;border-top:1px solid var(--line)}} select,.notes{{width:100%;height:42px;margin-top:6px;border:1px solid var(--line);border-radius:9px;padding:0 10px}}
@media(max-width:1000px){{.images{{grid-template-columns:1fr 1fr}}.mobile{{grid-column:1/-1}}.checks{{grid-template-columns:1fr}}}}
</style>
</head>
<body>
<div class="top">
  <h1>{html.escape(spec["review_title"])}｜差し替え前チェック</h1>
  <label>レビュアー <input id="reviewer" placeholder="氏名"></label>
  <button id="export">レビューJSONを書き出す</button>
</div>
<main>{"".join(cards)}</main>
<script>
const review = {initial};
const byId = Object.fromEntries(review.assets.map(x => [x.asset_id, x]));
document.querySelectorAll('.card').forEach(card => {{
  const row = byId[card.dataset.assetId];
  card.querySelectorAll('input[type=checkbox]').forEach(input => {{
    input.checked = row.checklist[input.dataset.key] === true;
    input.addEventListener('change', () => row.checklist[input.dataset.key] = input.checked);
  }});
  const select = card.querySelector('select');
  select.value = row.decision;
  select.addEventListener('change', () => row.decision = select.value);
  const notes = card.querySelector('.notes');
  notes.value = row.notes || '';
  notes.addEventListener('input', () => row.notes = notes.value);
}});
document.getElementById('export').addEventListener('click', () => {{
  const reviewer = document.getElementById('reviewer').value.trim();
  if (!reviewer) {{ alert('レビュアー名を入力してください'); return; }}
  const now = new Date().toISOString();
  review.assets.forEach(row => {{ row.reviewer = reviewer; row.reviewed_at = now; }});
  const blob = new Blob([JSON.stringify(review, null, 2) + '\\n'], {{type:'application/json'}});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = '{html.escape(spec["design_version"])}_approvals.json';
  a.click();
  URL.revokeObjectURL(a.href);
}});
</script>
</body>
</html>
"""


def init_review(
    manifest_path: Path,
    render_index_path: Path,
    qa_path: Path,
    output_path: Path,
    html_path: Path | None = None,
) -> dict[str, Any]:
    manifest = _load_manifest(manifest_path)
    spec = _creative_spec(manifest)
    render_index = _load_json(render_index_path)
    qa = _load_json(qa_path)
    if qa.get("schema_version") != spec["qa_schema"]:
        raise GateError("unsupported QA schema")
    qa_map = {
        str(asset["asset_id"]): asset for asset in qa.get("assets") or []
    }
    review_assets = []
    for asset in manifest["assets"]:
        asset_id = str(asset["asset_id"])
        qa_item = qa_map.get(asset_id)
        if not qa_item:
            raise GateError(f"{asset_id}: QA record missing")
        review_assets.append(
            {
                "asset_id": asset_id,
                "salon_id": str(asset.get("salon_id") or ""),
                "qa_status": qa_item["status"],
                "decision": "review_pending",
                "reviewer": "",
                "reviewed_at": "",
                "checklist_version": spec["checklist_version"],
                "checklist": {
                    key: None for key, _ in spec["manual_checks"]
                },
                "notes": "",
            }
        )
    payload = {
        "schema_version": spec["review_schema"],
        "environment": str(manifest.get("environment") or "production"),
        "manifest_sha256": sha256_file(manifest_path),
        "qa_report_sha256": sha256_file(qa_path),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "assets": review_assets,
    }
    _write_json(output_path, payload)
    if html_path:
        html_path.parent.mkdir(parents=True, exist_ok=True)
        html_path.write_text(
            _review_html(manifest, render_index, qa, payload),
            encoding="utf-8",
        )
    return payload


def _url_check(url: str, expected_sha256: str) -> tuple[bool, str]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "HairbookPersonV3Preflight/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read()
            status = getattr(response, "status", 200)
            content_type = response.headers.get("Content-Type", "")
    except Exception as exc:  # noqa: BLE001 - report the remote failure as evidence
        return False, repr(exc)
    actual_sha256 = __import__("hashlib").sha256(data).hexdigest()
    passed = (
        status == 200
        and content_type.lower().startswith("image/jpeg")
        and actual_sha256 == expected_sha256
    )
    return (
        passed,
        f"status={status}, content_type={content_type}, sha256={actual_sha256}",
    )


def run_preflight(
    manifest_path: Path,
    render_index_path: Path,
    qa_path: Path,
    approvals_path: Path,
    override_snapshot_path: Path,
    output_dir: Path,
    dry_run: bool,
    check_urls: bool,
) -> dict[str, Any]:
    manifest = _load_manifest(manifest_path)
    spec = _creative_spec(manifest)
    render_index = _load_json(render_index_path)
    qa = _load_json(qa_path)
    approvals = _load_json(approvals_path)
    snapshot = _load_json(override_snapshot_path)

    checks: list[dict[str, Any]] = []
    _check(
        checks,
        "environment",
        dry_run or manifest.get("environment", "production") == "production",
        f"manifest environment={manifest.get('environment')!r}, dry_run={dry_run}",
    )
    _check(
        checks,
        "qa_schema",
        qa.get("schema_version") == spec["qa_schema"],
        str(qa.get("schema_version")),
    )
    _check(
        checks,
        "qa_overall",
        qa.get("overall_status") == "pass",
        f"overall_status={qa.get('overall_status')!r}",
    )
    _check(
        checks,
        "review_schema",
        approvals.get("schema_version") == spec["review_schema"],
        str(approvals.get("schema_version")),
    )
    _check(
        checks,
        "override_snapshot_schema",
        snapshot.get("schema_version") == OVERRIDE_SNAPSHOT_SCHEMA,
        str(snapshot.get("schema_version")),
    )
    manifest_hash = sha256_file(manifest_path)
    _check(
        checks,
        "manifest_hashes",
        render_index.get("manifest_sha256") == manifest_hash
        and qa.get("manifest_sha256") == manifest_hash
        and approvals.get("manifest_sha256") == manifest_hash,
        f"current={manifest_hash}",
    )

    manifest_map = {
        str(asset["asset_id"]): asset for asset in manifest["assets"]
    }
    render_map = {
        str(asset["asset_id"]): asset
        for asset in render_index.get("assets") or []
    }
    qa_map = {
        str(asset["asset_id"]): asset for asset in qa.get("assets") or []
    }
    approval_map = {
        str(asset["asset_id"]): asset
        for asset in approvals.get("assets") or []
    }
    snapshot_map = {
        str(row.get("id") or ""): row for row in snapshot.get("rows") or []
    }

    accepted_decisions = (
        {"approved", "approved_for_dry_run"} if dry_run else {"approved"}
    )
    product_owners: dict[str, str] = {}
    entries = []
    per_asset: list[dict[str, Any]] = []
    for asset_id, asset in manifest_map.items():
        item_checks: list[dict[str, Any]] = []
        record = render_map.get(asset_id)
        qa_item = qa_map.get(asset_id)
        approval = approval_map.get(asset_id)
        _check(item_checks, "render_record", record is not None, "present")
        _check(
            item_checks,
            "qa_passed",
            qa_item is not None and qa_item.get("status") == "pass",
            f"status={qa_item.get('status') if qa_item else None}",
        )
        decision = approval.get("decision") if approval else None
        _check(
            item_checks,
            "human_decision",
            decision in accepted_decisions,
            f"decision={decision!r}",
        )
        checklist = approval.get("checklist") if approval else {}
        checklist_ok = bool(checklist) and all(
            checklist.get(key) is True
            for key, _ in spec["manual_checks"]
        )
        _check(
            item_checks,
            "human_checklist",
            checklist_ok,
            "all manual checks are true" if checklist_ok else "one or more checks are incomplete",
        )
        _check(
            item_checks,
            "reviewer",
            bool(str((approval or {}).get("reviewer") or "").strip())
            and bool(str((approval or {}).get("reviewed_at") or "").strip()),
            f"reviewer={(approval or {}).get('reviewer')!r}",
        )

        product_ids = [str(value) for value in asset.get("product_ids") or []]
        _check(
            item_checks,
            "product_mapping",
            bool(product_ids) or dry_run,
            f"{len(product_ids)} product IDs",
        )
        for product_id in product_ids:
            prior = product_owners.get(product_id)
            _check(
                item_checks,
                f"unique_product_id:{product_id}",
                prior is None or prior == asset_id,
                "unique" if prior is None else f"already owned by {prior}",
            )
            product_owners[product_id] = asset_id

        if record:
            output_path = Path(str(record.get("output_path") or ""))
            output_ok = (
                output_path.is_file()
                and sha256_file(output_path) == record.get("output_sha256")
            )
            _check(
                item_checks,
                "frozen_output",
                output_ok,
                str(output_path),
            )
            public_url = str(record.get("public_url") or asset.get("public_url") or "")
            _check(
                item_checks,
                "public_url",
                (public_url.startswith("https://") if not dry_run else True),
                public_url or "not required for dry-run",
            )
            if check_urls and public_url:
                passed, detail = _url_check(
                    public_url,
                    str(record.get("output_sha256") or ""),
                )
                _check(item_checks, "public_url_fetch", passed, detail)

            if product_ids:
                for product_id in product_ids:
                    previous = snapshot_map.get(product_id, {})
                    entries.append(
                        {
                            "product_id": product_id,
                            "salon_id": str(asset.get("salon_id") or ""),
                            "asset_id": asset_id,
                            "design_version": spec["design_version"],
                            "render_mode": "complete_banner",
                            "new_image_url": public_url,
                            "new_image_sha256": str(record.get("output_sha256") or ""),
                            "previous_override": previous,
                        }
                    )
            elif dry_run:
                entries.append(
                    {
                        "product_id": None,
                        "salon_id": str(asset.get("salon_id") or ""),
                        "asset_id": asset_id,
                        "design_version": spec["design_version"],
                        "render_mode": "complete_banner",
                        "new_image_url": public_url,
                        "new_image_sha256": str(record.get("output_sha256") or ""),
                        "previous_override": {},
                        "sample_only": True,
                    }
                )

        item_status = (
            "fail"
            if any(check["status"] == "fail" for check in item_checks)
            else "pass"
        )
        per_asset.append(
            {
                "asset_id": asset_id,
                "status": item_status,
                "checks": item_checks,
            }
        )

    overall_status = (
        "fail"
        if any(check["status"] == "fail" for check in checks)
        or any(asset["status"] == "fail" for asset in per_asset)
        else "dry_run_ready"
        if dry_run
        else "ready_to_publish"
    )
    generated_at = datetime.now(timezone.utc).isoformat()
    snapshot_hash = sha256_file(override_snapshot_path)
    rollout_context = {
        "account_scope": manifest.get("account_scope") or "",
        "experiment": manifest.get("experiment") or {},
    }
    publish_manifest = {
        "schema_version": PUBLISH_SCHEMA,
        "mode": "dry_run" if dry_run else "production",
        "status": overall_status,
        "generated_at": generated_at,
        "manifest_sha256": manifest_hash,
        "qa_report_sha256": sha256_file(qa_path),
        "approvals_sha256": sha256_file(approvals_path),
        "override_snapshot_sha256": snapshot_hash,
        **rollout_context,
        "entries": entries,
    }
    rollback_manifest = {
        "schema_version": ROLLBACK_SCHEMA,
        "mode": "dry_run" if dry_run else "production",
        "status": "planned" if overall_status != "fail" else "blocked",
        "generated_at": generated_at,
        "source_publish_manifest_sha256": _canonical_sha(publish_manifest),
        **rollout_context,
        "entries": [
            {
                "product_id": entry["product_id"],
                "salon_id": entry["salon_id"],
                "asset_id": entry["asset_id"],
                "restore_override": entry["previous_override"],
            }
            for entry in entries
        ],
    }
    report = {
        "schema_version": PREFLIGHT_SCHEMA,
        "mode": "dry_run" if dry_run else "production",
        "status": overall_status,
        "generated_at": generated_at,
        **rollout_context,
        "checks": checks,
        "assets": per_asset,
        "summary": {
            "asset_count": len(per_asset),
            "entry_count": len(entries),
            "failed_assets": sum(asset["status"] == "fail" for asset in per_asset),
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "publish_manifest.json", publish_manifest)
    _write_json(output_dir / "rollback_manifest.json", rollback_manifest)
    _write_json(output_dir / "preflight_report.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Person-first V3 automated QA and pre-publish gates."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    qa_parser = sub.add_parser("qa", help="Run automated QA")
    qa_parser.add_argument("--manifest", type=Path, required=True)
    qa_parser.add_argument("--render-index", type=Path, required=True)
    qa_parser.add_argument("--output", type=Path, required=True)
    qa_parser.add_argument("--max-bytes", type=int, default=8 * 1024 * 1024)
    qa_parser.add_argument("--min-source-short-side", type=int, default=720)

    review_parser = sub.add_parser(
        "init-review",
        help="Create a human-review template and optional HTML",
    )
    review_parser.add_argument("--manifest", type=Path, required=True)
    review_parser.add_argument("--render-index", type=Path, required=True)
    review_parser.add_argument("--qa-report", type=Path, required=True)
    review_parser.add_argument("--output", type=Path, required=True)
    review_parser.add_argument("--html", type=Path)

    preflight_parser = sub.add_parser(
        "preflight",
        help="Validate approval and generate dry-run publish/rollback manifests",
    )
    preflight_parser.add_argument("--manifest", type=Path, required=True)
    preflight_parser.add_argument("--render-index", type=Path, required=True)
    preflight_parser.add_argument("--qa-report", type=Path, required=True)
    preflight_parser.add_argument("--approvals", type=Path, required=True)
    preflight_parser.add_argument("--override-snapshot", type=Path, required=True)
    preflight_parser.add_argument("--output-dir", type=Path, required=True)
    preflight_parser.add_argument("--dry-run", action="store_true")
    preflight_parser.add_argument("--check-urls", action="store_true")

    args = parser.parse_args()
    try:
        if args.command == "qa":
            payload = run_qa(
                args.manifest,
                args.render_index,
                args.output,
                max_bytes=args.max_bytes,
                min_source_short_side=args.min_source_short_side,
            )
            summary = {
                "status": payload["overall_status"],
                **payload["summary"],
                "output": str(args.output.resolve()),
            }
        elif args.command == "init-review":
            payload = init_review(
                args.manifest,
                args.render_index,
                args.qa_report,
                args.output,
                args.html,
            )
            summary = {
                "status": "review_pending",
                "asset_count": len(payload["assets"]),
                "output": str(args.output.resolve()),
                "html": str(args.html.resolve()) if args.html else None,
            }
        else:
            payload = run_preflight(
                args.manifest,
                args.render_index,
                args.qa_report,
                args.approvals,
                args.override_snapshot,
                args.output_dir,
                dry_run=args.dry_run,
                check_urls=args.check_urls,
            )
            summary = {
                "status": payload["status"],
                **payload["summary"],
                "output_dir": str(args.output_dir.resolve()),
            }
        print(json.dumps(summary, ensure_ascii=False))
        if summary["status"] == "fail":
            raise SystemExit(2)
    except (GateError, ManifestError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
