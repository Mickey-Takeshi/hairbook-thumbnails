"""Build the reviewed 1:1, person-first Hairbook catalog manifest.

This builder is intentionally read-only with respect to Hairbook, Google
Sheets, and ``thumbnail_override``.  It downloads candidate photographs from
the Hairbook landing pages and current feed, scores every candidate with
Apple Vision, prefers clear non-treatment portraits, allows finished nail
close-ups for nail-only products, cleans all copy, and records the chosen
editorial layout for the square renderer.

Example:
    python3 thumbgen/build_person_square_catalog.py \
      --feed-csv /tmp/hairbook-feed.csv \
      --output-dir dashboard/private_snapshots/person-square-full-20260727
"""
from __future__ import annotations

import argparse
import colorsys
import csv
import hashlib
import html
import json
import math
import platform
import re
import shutil
import subprocess
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

import numpy as np
from PIL import Image, ImageFilter, ImageOps

from build_person_v3_catalog import (
    CatalogBuildError,
    PageInfo,
    SourceCandidate,
    _active_rows,
    _clean_salon_name,
    _download_candidate,
    _fetch_feed,
    _fetch_page_info,
    _feed_location_prefix,
    _landing_key,
    _manifest_source_path,
    _read_feed_csv,
    _sha256,
    _staff_styles_source_url,
)
from creative_inventory import parse_product_id


SCHEMA_VERSION = "hairbook.person_square_manifest.v1"
DESIGN_VERSION = "person_square_editorial_v1"
CLASSIFICATION_SCHEMA = "hairbook.person_square_classification.v1"
DISCOVERY_SCHEMA = "hairbook.person_square_page_discovery.v1"
SUMMARY_SCHEMA = "hairbook.person_square_full_build_summary.v1"
VISION_SCHEMA = "hairbook.source_vision_report.v1"
MAX_PATTERNS_PER_LANDING = 2
MIN_PERSON_SCORE = 0.30
MAX_TREATMENT_RISK = 0.72

ROOT = Path(__file__).resolve().parent
VISION_SOURCE = ROOT / "vision_source_analyzer.swift"
BACKGROUND_CLEANER_SOURCE = ROOT / "clean_person_background.swift"

HTML_TAG_RE = re.compile(r"<\s*/?\s*[a-zA-Z][^>]*>")
HTML_ENTITY_RE = re.compile(r"&(?:[a-zA-Z]{2,10}|#[0-9]{2,6}|#x[0-9a-fA-F]{2,6});")
FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")

TREATMENT_LABEL_WEIGHTS = {
    "bathroom": 1.00,
    "bath": 1.00,
    "washbasin": 1.15,
    "sink": 0.85,
    "shower": 0.95,
    "bathrobe": 0.75,
    "towel": 0.65,
    "glove": 0.70,
    "glove_other": 0.70,
    "medicine": 0.85,
    "syringe": 1.20,
    "cosmetic_tool": 0.95,
    "tool": 0.42,
    "hospital": 0.55,
    "headgear": 0.32,
    "machine": 0.24,
    "container": 0.14,
}

NON_PERSON_LABEL_WEIGHTS = {
    "document": 1.15,
    "screenshot": 1.30,
    "printed_page": 1.25,
    "sign": 1.00,
    "banner": 0.90,
    "handwriting": 0.90,
    "chart": 1.00,
    "diagram": 1.00,
    "receipt": 1.25,
    "product": 0.95,
    "bottle": 0.85,
    "syringe": 1.20,
    "medicine": 1.00,
    "cosmetic_tool": 0.90,
    "tool": 0.60,
    "plant": 0.62,
    "food": 0.65,
    "building": 0.85,
    "street": 0.75,
    "interior_room": 0.72,
}

INDUSTRY_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "nail": [
        re.compile(pattern, re.I)
        for pattern in (
            r"ネイル",
            r"\bnail\b",
            r"ジェル",
            r"深爪",
            r"美爪",
            r"フットネイル",
        )
    ],
    "eye": [
        re.compile(pattern, re.I)
        for pattern in (
            r"まつ毛",
            r"まつげ",
            r"眉毛",
            r"アイラッシュ",
            r"アイブロウ",
            r"アイサロン",
            r"\beyelash\b",
            r"\beyebrow\b",
            r"\beye\s*salon\b",
            r"\beyesalon\b",
            r"\beyelashsalon\b",
            r"\beyebrowsalon\b",
        )
    ],
    "head_spa": [
        re.compile(pattern, re.I)
        for pattern in (
            r"ドライヘッドスパ",
            r"ヘッドスパ専門",
            r"頭浸浴",
            r"頭ほぐし",
        )
    ],
    "esthetic": [
        re.compile(pattern, re.I)
        for pattern in (
            r"エステサロン",
            r"エステ専門",
            r"脱毛",
            r"フェイシャル",
            r"痩身",
            r"ボディケア",
            r"コルギ",
            r"毛穴",
            r"肌質改善",
            r"リラク",
            r"フェムケア",
            r"更年期",
            r"骨髄由来",
        )
    ],
    "hair_mens": [
        re.compile(pattern, re.I)
        for pattern in (
            r"メンズ",
            r"\bmen'?s\b",
            r"男性",
            r"理容",
            r"\bbarber(?:shop)?\b",
        )
    ],
    "hair": [
        re.compile(pattern, re.I)
        for pattern in (
            r"美容室",
            r"美容師",
            r"ヘア",
            r"\bhair\b",
            r"カット",
            r"カラー",
            r"髪",
            r"ショート",
            r"ボブ",
            r"縮毛",
            r"ヘッドスパ",
        )
    ],
}

HEADLINES: dict[str, list[tuple[re.Pattern[str], list[str]]]] = {
    "hair": [
        (
            re.compile(r"縮毛|髪質改善|ストレート|うねり", re.I),
            ["朝のスタイリングを楽に", "まとまりのある髪へ"],
        ),
        (
            re.compile(r"カラー|ブリーチ|ハイライト|インナー", re.I),
            ["印象になじむカラーを", "似合わせで提案"],
        ),
        (
            re.compile(r"ショート|ボブ", re.I),
            ["骨格になじむシルエット", "扱いやすいスタイルへ"],
        ),
        (
            re.compile(r"大人|上質|30代|40代|50代", re.I),
            ["今の自分に、ちょうどいい", "品よく整うヘアデザイン"],
        ),
    ],
    "hair_mens": [
        (
            re.compile(r"パーマ|ツイスト|スパイラル|波巻き", re.I),
            ["清潔感も、動きも。", "似合うメンズパーマ"],
        ),
    ],
    "nail": [],
    "eye": [],
    "eye_nail": [],
    "head_spa": [],
    "esthetic": [],
}

DEFAULT_HEADLINES = {
    "hair": ["自分らしさが見つかる", "似合わせヘア"],
    "hair_mens": ["清潔感を、デザインする", "扱いやすいメンズヘア"],
    "nail": ["指先を見るたび、気分が上がる", "似合わせネイル"],
    "eye": ["目元に、さりげない自信を", "似合わせアイデザイン"],
    "eye_nail": ["目元も、指先も。", "私らしく整える"],
    "head_spa": ["頭から、軽やかに。", "深く休まるヘッドスパ"],
    "esthetic": ["素肌から、もっと自分らしく", "丁寧に寄り添うケア"],
}


def _clean_text(value: Any, *, preserve_lines: bool = False) -> str:
    text = str(value or "")
    text = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", text)
    text = re.sub(r"(?i)</\s*(?:p|div|li|h[1-6])\s*>", "\n", text)
    text = re.sub(r"<[^>]*>", " ", text)
    text = html.unescape(text)
    text = (
        text.replace("\u200b", "")
        .replace("\u200c", "")
        .replace("\u200d", "")
        .replace("\ufeff", "")
        .replace("\xa0", " ")
    )
    if preserve_lines:
        lines = [
            re.sub(r"[ \t\u3000]+", " ", line).strip()
            for line in text.replace("\r", "\n").split("\n")
        ]
        return "\n".join(line for line in lines if line)
    return re.sub(r"\s+", " ", text).strip()


def _candidate_identity(candidate: SourceCandidate) -> str:
    return f"{candidate.source_type}|{candidate.record_id}|{candidate.image_url}"


def _feed_candidate(row: dict[str, str]) -> SourceCandidate | None:
    image_url = _clean_text(row.get("image_link"))
    if not image_url.startswith("https://hairbook.jp/"):
        return None
    path_parts = [part for part in urlsplit(image_url).path.split("/") if part]
    record_id = path_parts[-1] if path_parts else ""
    if not record_id:
        record_id = hashlib.sha256(image_url.encode("utf-8")).hexdigest()[:20]
    return SourceCandidate(
        source_type="hairbook_feed_post_photo",
        record_id=record_id,
        page_url=_landing_key(row["link"]),
        image_url=image_url,
        metadata_text="\n".join(
            value
            for value in (
                _clean_text(row.get("title")),
                _clean_text(row.get("description"), preserve_lines=True),
            )
            if value
        ),
    )


def _dedupe_candidates(
    candidates: Iterable[SourceCandidate],
) -> list[SourceCandidate]:
    output: list[SourceCandidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = candidate.image_url
        if key in seen:
            continue
        seen.add(key)
        output.append(candidate)
    return output


def _page_source_candidates(
    *,
    landing_url: str,
    rows: list[dict[str, str]],
    info: PageInfo | None,
    override_candidates: dict[str, list[SourceCandidate]],
) -> list[SourceCandidate]:
    """Apply the source boundary for one catalog landing page."""
    staff_styles_url = _staff_styles_source_url(landing_url)
    candidates = list(info.candidates) if info else []
    if staff_styles_url:
        # A staff landing must use a finished style from that staff's gallery.
        # Profile, feed, official override and same-salon images are not valid
        # substitutes because they do not represent the destination context.
        return _dedupe_candidates(
            candidate
            for candidate in candidates
            if candidate.source_type == "hairbook_stylist_style_photo"
        )
    if info:
        candidates.extend(
            override_candidates.get(info.salon_id, [])
        )
    candidates.extend(
        candidate
        for candidate in (_feed_candidate(row) for row in rows)
        if candidate
    )
    return _dedupe_candidates(candidates)


def _download_all_candidates(
    candidates: list[SourceCandidate],
    source_dir: Path,
    workers: int,
    *,
    reuse_sources: bool = False,
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    def prepare(candidate: SourceCandidate) -> dict[str, Any]:
        if candidate.local_path:
            original_path = Path(candidate.local_path).resolve()
            suffix = original_path.suffix.lower() or ".png"
            path = (
                source_dir
                / f"person-{candidate.record_id}{suffix}"
            ).resolve()
            if (
                not path.is_file()
                or _sha256(path) != _sha256(original_path)
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(original_path, path)
            try:
                with Image.open(path) as opened:
                    oriented = ImageOps.exif_transpose(opened)
                    oriented.load()
                    width, height = oriented.size
                    image_format = str(opened.format or "").upper()
            except OSError as exc:
                raise CatalogBuildError(
                    f"could not read local source {original_path}: {exc}"
                ) from exc
            return {
                "candidate": candidate,
                "path": path,
                "sha256": _sha256(path),
                "width": width,
                "height": height,
                "format": image_format,
                "final_url": candidate.image_url,
            }
        if reuse_sources:
            prefix = (
                "style"
                if "style_photo" in candidate.source_type
                else "person"
            )
            matches = sorted(
                path
                for path in source_dir.glob(
                    f"{prefix}-{candidate.record_id}.*"
                )
                if path.is_file() and not path.name.startswith(".")
            )
            for path in matches:
                try:
                    with Image.open(path) as opened:
                        oriented = ImageOps.exif_transpose(opened)
                        oriented.load()
                        width, height = oriented.size
                        image_format = str(opened.format or "").upper()
                except OSError:
                    continue
                return {
                    "candidate": candidate,
                    "path": path,
                    "sha256": _sha256(path),
                    "width": width,
                    "height": height,
                    "format": image_format,
                    "final_url": candidate.image_url,
                }
        return _download_candidate(candidate, source_dir)

    prepared: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(prepare, candidate):
            candidate
            for candidate in candidates
        }
        for future in as_completed(futures):
            candidate = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # keep the full audit progressing
                errors[candidate.image_url] = str(exc)
            else:
                prepared[candidate.image_url] = result
    return prepared, errors


def _load_source_overrides(
    path: Path | None,
) -> dict[str, list[SourceCandidate]]:
    if not path:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogBuildError(
            f"cannot read source overrides {path}: {exc}"
        ) from exc
    if payload.get("schema_version") != (
        "hairbook.person_square_source_overrides.v1"
    ):
        raise CatalogBuildError("unsupported source override schema")
    output: dict[str, list[SourceCandidate]] = {}
    for index, item in enumerate(payload.get("overrides") or []):
        salon_id = _clean_text(item.get("salon_id"))
        record_id = _clean_text(item.get("record_id"))
        page_url = _clean_text(item.get("page_url"))
        image_url = _clean_text(item.get("image_url"))
        local_path = _clean_text(item.get("local_path"))
        resolved_local_path = ""
        if local_path:
            local_candidate = Path(local_path)
            if not local_candidate.is_absolute():
                local_candidate = path.parent / local_candidate
            resolved_local_path = str(local_candidate.resolve())
        if (
            not salon_id
            or not record_id
            or not page_url.startswith("https://")
            or not image_url.startswith("https://")
            or (
                resolved_local_path
                and not Path(resolved_local_path).is_file()
            )
        ):
            raise CatalogBuildError(
                f"source override {index} is incomplete"
            )
        output.setdefault(salon_id, []).append(
            SourceCandidate(
                source_type="official_salon_person_photo",
                record_id=record_id,
                page_url=page_url,
                image_url=image_url,
                metadata_text=_clean_text(item.get("metadata_text")),
                local_path=resolved_local_path,
            )
        )
    return output


def _load_source_rejections(
    path: Path | None,
) -> dict[tuple[str, str], str]:
    if not path:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogBuildError(
            f"cannot read source rejections {path}: {exc}"
        ) from exc
    if payload.get("schema_version") != (
        "hairbook.person_square_source_rejections.v1"
    ):
        raise CatalogBuildError("unsupported source rejection schema")
    output: dict[tuple[str, str], str] = {}
    for index, item in enumerate(payload.get("rejections") or []):
        source_type = _clean_text(item.get("source_type"))
        record_id = _clean_text(item.get("record_id"))
        reason = _clean_text(item.get("reason"))
        if not source_type or not record_id or not reason:
            raise CatalogBuildError(
                f"source rejection {index} is incomplete"
            )
        output[(source_type, record_id)] = reason
    return output


def _compile_vision_tool(output_dir: Path) -> Path:
    if platform.system() != "Darwin":
        raise CatalogBuildError(
            "Apple Vision source scoring requires macOS or --vision-report"
        )
    if not VISION_SOURCE.is_file():
        raise CatalogBuildError(f"Vision source is missing: {VISION_SOURCE}")
    binary = output_dir / ".vision-source-analyzer"
    command = ["xcrun", "swiftc", str(VISION_SOURCE), "-O", "-o", str(binary)]
    try:
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        stderr = getattr(exc, "stderr", "")
        raise CatalogBuildError(
            f"could not compile Apple Vision analyzer: {exc}: {stderr}"
        ) from exc
    return binary


def _compile_background_cleaner(output_dir: Path) -> Path:
    if platform.system() != "Darwin":
        raise CatalogBuildError(
            "person background cleaning requires macOS"
        )
    if not BACKGROUND_CLEANER_SOURCE.is_file():
        raise CatalogBuildError(
            f"background cleaner is missing: {BACKGROUND_CLEANER_SOURCE}"
        )
    binary = output_dir / ".person-background-cleaner"
    if (
        binary.is_file()
        and binary.stat().st_mtime
        >= BACKGROUND_CLEANER_SOURCE.stat().st_mtime
    ):
        return binary
    command = [
        "xcrun",
        "swiftc",
        str(BACKGROUND_CLEANER_SOURCE),
        "-O",
        "-o",
        str(binary),
    ]
    try:
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        stderr = getattr(exc, "stderr", "")
        raise CatalogBuildError(
            f"could not compile background cleaner: {exc}: {stderr}"
        ) from exc
    return binary


def _run_vision(
    source_dir: Path,
    output_dir: Path,
    *,
    vision_tool: Path | None = None,
    vision_report: Path | None = None,
) -> tuple[dict[str, dict[str, Any]], Path]:
    report_path = output_dir / "source_vision.jsonl"
    if vision_report:
        report_path = vision_report.resolve()
    else:
        tool = vision_tool.resolve() if vision_tool else _compile_vision_tool(
            output_dir
        )
        try:
            subprocess.run(
                [
                    str(tool),
                    "--output",
                    str(report_path),
                    "--directory",
                    str(source_dir),
                ],
                check=True,
                timeout=1200,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise CatalogBuildError(
                f"Apple Vision source analysis failed: {exc}"
            ) from exc

    def load_report(path: Path) -> dict[str, dict[str, Any]]:
        loaded: dict[str, dict[str, Any]] = {}
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, 1):
                    if not line.strip():
                        continue
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise CatalogBuildError(
                            f"{path}:{line_number}: invalid JSON: {exc}"
                        ) from exc
                    raw_path = Path(str(item.get("path") or ""))
                    loaded[str(raw_path.resolve())] = item
        except OSError as exc:
            raise CatalogBuildError(
                f"could not read Vision report {path}: {exc}"
            ) from exc
        return loaded

    analyses = load_report(report_path)
    if vision_report:
        supported = {".jpg", ".jpeg", ".png", ".webp", ".avif"}
        missing = sorted(
            path
            for path in source_dir.iterdir()
            if path.is_file()
            and path.suffix.lower() in supported
            and str(path.resolve()) not in analyses
        )
        if missing:
            tool = (
                vision_tool.resolve()
                if vision_tool
                else _compile_vision_tool(output_dir)
            )
            incremental = output_dir / "source_vision_incremental.jsonl"
            try:
                subprocess.run(
                    [
                        str(tool),
                        "--output",
                        str(incremental),
                        *[str(path) for path in missing],
                    ],
                    check=True,
                    timeout=1200,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise CatalogBuildError(
                    f"incremental Apple Vision analysis failed: {exc}"
                ) from exc
            incremental_text = incremental.read_text(encoding="utf-8")
            with report_path.open("a", encoding="utf-8") as handle:
                handle.write(incremental_text)
            analyses.update(load_report(incremental))
    return analyses, report_path


def _prepare_staff_background_clean_variants(
    page_candidates: dict[str, list[SourceCandidate]],
    prepared_by_url: dict[str, dict[str, Any]],
    vision_by_path: dict[str, dict[str, Any]],
    source_dir: Path,
    output_dir: Path,
    *,
    workers: int,
) -> int:
    """Create identity-preserving derivatives for logo/text-heavy portraits."""
    originals: dict[str, tuple[SourceCandidate, dict[str, Any]]] = {}
    for candidates in page_candidates.values():
        for candidate in candidates:
            if candidate.source_type != "hairbook_staff_profile_photo":
                continue
            prepared = prepared_by_url.get(candidate.image_url)
            if not prepared:
                continue
            analysis = vision_by_path.get(
                str(Path(prepared["path"]).resolve())
            )
            vision = _score_source(prepared, analysis)
            if (
                vision["person_score"] < 0.50
                or vision["face_score"] < 0.45
                or vision["treatment_flags"]
                or vision["treatment_risk"] > 0.25
                or (
                    not vision.get("text_flags")
                    and vision["non_person_risk"] <= 0.55
                )
            ):
                continue
            originals[candidate.image_url] = (candidate, prepared)
    if not originals:
        return 0

    cleaner = _compile_background_cleaner(output_dir)

    def clean(
        value: tuple[SourceCandidate, dict[str, Any]],
    ) -> tuple[str, SourceCandidate, dict[str, Any]] | None:
        candidate, prepared = value
        suffix = prepared["sha256"][:8]
        record_id = (
            f"{candidate.record_id}-background-clean-{suffix}"
        )
        output = source_dir / f"person-{record_id}.png"
        if not output.is_file():
            try:
                subprocess.run(
                    [str(cleaner), str(prepared["path"]), str(output)],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=90,
                )
            except (OSError, subprocess.SubprocessError):
                return None
        try:
            with Image.open(output) as opened:
                oriented = ImageOps.exif_transpose(opened)
                oriented.load()
                width, height = oriented.size
                image_format = str(opened.format or "").upper()
        except OSError:
            return None
        separator = "&" if "?" in candidate.image_url else "?"
        image_url = (
            f"{candidate.image_url}{separator}"
            f"background_clean=auto-v1&source_sha={suffix}"
        )
        derived = SourceCandidate(
            source_type="hairbook_staff_profile_background_clean",
            record_id=record_id,
            page_url=candidate.page_url,
            image_url=image_url,
            metadata_text=candidate.metadata_text,
            local_path=str(output.resolve()),
        )
        return (
            candidate.image_url,
            derived,
            {
                "candidate": derived,
                "path": output.resolve(),
                "sha256": _sha256(output),
                "width": width,
                "height": height,
                "format": image_format,
                "final_url": image_url,
            },
        )

    variants: dict[str, SourceCandidate] = {}
    with ThreadPoolExecutor(
        max_workers=max(1, min(4, workers))
    ) as executor:
        futures = [
            executor.submit(clean, value)
            for value in originals.values()
        ]
        for future in as_completed(futures):
            result = future.result()
            if not result:
                continue
            original_url, candidate, prepared = result
            variants[original_url] = candidate
            prepared_by_url[candidate.image_url] = prepared

    if not variants:
        return 0
    for key, candidates in page_candidates.items():
        additions = [
            variants[candidate.image_url]
            for candidate in list(candidates)
            if candidate.image_url in variants
        ]
        page_candidates[key] = _dedupe_candidates(
            [*candidates, *additions]
        )
    return len(variants)


def _label_map(analysis: dict[str, Any]) -> dict[str, float]:
    return {
        str(item.get("name") or ""): float(item.get("confidence") or 0)
        for item in analysis.get("labels") or []
        if str(item.get("name") or "")
    }


def _source_text_analysis(
    analysis: dict[str, Any],
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for raw in analysis.get("texts") or []:
        value = _clean_text(raw.get("text"))
        signal = re.sub(r"[^0-9A-Za-z一-龯ぁ-んァ-ヶー]+", "", value)
        confidence = float(raw.get("confidence") or 0)
        width = max(0.0, min(1.0, float(raw.get("width") or 0)))
        height = max(0.0, min(1.0, float(raw.get("height") or 0)))
        if len(signal) < 2 or confidence < 0.20 or width * height < 0.00025:
            continue
        items.append(
            {
                "text": value[:80],
                "x": round(float(raw.get("x") or 0), 5),
                "y": round(float(raw.get("y") or 0), 5),
                "width": round(width, 5),
                "height": round(height, 5),
                "confidence": round(confidence, 5),
                "signal_length": len(signal),
            }
        )
    area_ratio = min(
        1.0,
        sum(item["width"] * item["height"] for item in items),
    )
    longest = max(
        (int(item["signal_length"]) for item in items),
        default=0,
    )
    risk = min(
        1.5,
        area_ratio * 9.0 + len(items) * 0.055 + longest * 0.012,
    )
    flags: list[str] = []
    if (
        area_ratio >= 0.045
        or (len(items) >= 3 and area_ratio >= 0.006)
        or (longest >= 6 and area_ratio >= 0.004)
    ):
        flags.append("baked_text_detected")
    return {
        "text_count": len(items),
        "text_area_ratio": round(area_ratio, 5),
        "text_risk": round(risk, 5),
        "text_flags": flags,
        "text_boxes": items[:20],
        "recognized_text": [item["text"] for item in items[:12]],
    }


def _weighted_label_risk(
    labels: dict[str, float],
    weights: dict[str, float],
) -> float:
    return min(
        1.5,
        sum(labels.get(name, 0.0) * weight for name, weight in weights.items()),
    )


def _treatment_flags(labels: dict[str, float]) -> list[str]:
    flags: list[str] = []
    if max(
        labels.get("mask", 0),
        labels.get("gas_mask", 0),
    ) >= 0.01:
        flags.append("face_mask_or_obstruction")
    if labels.get("hospital", 0) >= 0.28:
        flags.append("hospital_or_facial_treatment")
    if (
        labels.get("bathroom", 0) >= 0.14
        and labels.get("washbasin", 0) >= 0.14
        and labels.get("bathroom", 0)
        + labels.get("washbasin", 0)
        >= 0.34
    ):
        flags.append("shampoo_or_washbasin_scene")
    if (
        max(
            labels.get("glove", 0),
            labels.get("glove_other", 0),
        )
        >= 0.16
        and max(
            labels.get("tool", 0),
            labels.get("cosmetic_tool", 0),
            labels.get("medicine", 0),
        )
        >= 0.06
    ):
        flags.append("gloved_treatment_scene")
    if max(
        labels.get("bathrobe", 0),
        labels.get("towel", 0),
    ) >= 0.24:
        flags.append("towel_or_wrapped_scene")
    if (
        labels.get("headgear", 0) >= 0.38
        and max(
            labels.get("tool", 0),
            labels.get("cosmetic_tool", 0),
        )
        >= 0.06
    ):
        flags.append("rods_or_treatment_headgear")
    return flags


def _best_box(
    boxes: list[dict[str, Any]],
) -> dict[str, float] | None:
    if not boxes:
        return None
    box = max(
        boxes,
        key=lambda item: (
            float(item.get("confidence") or 0),
            float(item.get("width") or 0) * float(item.get("height") or 0),
        ),
    )
    return {
        "x": float(box.get("x") or 0),
        "y": float(box.get("y") or 0),
        "width": float(box.get("width") or 0),
        "height": float(box.get("height") or 0),
        "confidence": float(box.get("confidence") or 0),
    }


def _score_source(
    prepared: dict[str, Any],
    analysis: dict[str, Any] | None,
) -> dict[str, Any]:
    candidate: SourceCandidate = prepared["candidate"]
    if not analysis or analysis.get("error"):
        return {
            "status": "rejected",
            "reason": "画像認識結果がありません",
            "score": -999.0,
            "person_score": 0.0,
            "face_score": 0.0,
            "human_score": 0.0,
            "face_count": 0,
            "people_count": 0,
            "treatment_risk": 1.0,
            "non_person_risk": 1.0,
            "text_count": 0,
            "text_area_ratio": 0.0,
            "text_risk": 1.0,
            "text_flags": ["vision_analysis_missing"],
            "text_boxes": [],
            "recognized_text": [],
            "face_box": None,
            "person_box": None,
            "top_labels": [],
        }

    labels = _label_map(analysis)
    source_text = _source_text_analysis(analysis)
    person_score = max(
        (
            labels.get("people", 0.0),
            labels.get("adult", 0.0),
            labels.get("teen", 0.0),
            labels.get("child", 0.0),
            labels.get("baby", 0.0),
        )
    )
    face_box = _best_box(list(analysis.get("faces") or []))
    person_box = _best_box(list(analysis.get("people") or []))
    face_count = len(analysis.get("faces") or [])
    people_count = len(analysis.get("people") or [])
    face_score = float((face_box or {}).get("confidence") or 0)
    human_score = float((person_box or {}).get("confidence") or 0)
    treatment_risk = _weighted_label_risk(
        labels,
        TREATMENT_LABEL_WEIGHTS,
    )
    treatment_flags = _treatment_flags(labels)
    if (
        (
            "style_photo" in candidate.source_type
            or candidate.source_type == "hairbook_feed_post_photo"
        )
        and max(face_count, people_count) >= 2
    ):
        treatment_flags.append("multiple_people_service_scene")
        treatment_risk = max(treatment_risk, 0.86)
    non_person_risk = _weighted_label_risk(
        labels,
        NON_PERSON_LABEL_WEIGHTS,
    )
    resolution_bonus = min(
        0.45,
        math.log2(max(220, min(prepared["width"], prepared["height"])) / 220)
        * 0.16,
    )
    source_bonus = (
        0.36
        if "style_photo" in candidate.source_type
        else 0.22
        if candidate.source_type == "official_salon_person_photo"
        else 0.20
        if candidate.source_type
        == "hairbook_staff_profile_background_clean"
        else 0.08
        if candidate.source_type == "hairbook_staff_profile_photo"
        else 0.0
    )
    score = (
        person_score * 6.2
        + face_score * 1.8
        + human_score * 0.9
        + (0.55 if face_box else 0.0)
        + resolution_bonus
        + source_bonus
        - treatment_risk * 2.8
        - non_person_risk * (3.1 if person_score < 0.48 else 0.7)
        - float(source_text["text_risk"]) * 2.4
    )

    evidence = bool(face_box or person_box or person_score >= 0.55)
    eligible = (
        person_score >= MIN_PERSON_SCORE
        and evidence
        and treatment_risk <= MAX_TREATMENT_RISK
        and not treatment_flags
        and not source_text["text_flags"]
        and non_person_risk <= 0.90
        and not (
            person_score < 0.48
            and non_person_risk >= 0.72
        )
    )
    if not eligible:
        if person_score < MIN_PERSON_SCORE:
            reason = "人物信頼度が基準未満"
        elif not evidence:
            reason = "人物または顔の検出根拠が不足"
        elif treatment_flags or treatment_risk > MAX_TREATMENT_RISK:
            reason = "施術中カットの可能性が高い"
        elif source_text["text_flags"]:
            reason = "写真への文字焼き込みを検出"
        elif non_person_risk > 0.90:
            reason = "文字・ロゴ・商品・店内画像の可能性が高い"
        else:
            reason = "商品・器具・店内・文字画像の可能性が高い"
    else:
        reason = "人物優先基準を満たし、施術中リスクが許容範囲"
    return {
        "status": "approved" if eligible else "rejected",
        "reason": reason,
        "score": round(score, 5),
        "person_score": round(person_score, 5),
        "face_score": round(face_score, 5),
        "human_score": round(human_score, 5),
        "face_count": face_count,
        "people_count": people_count,
        "treatment_risk": round(treatment_risk, 5),
        "treatment_flags": treatment_flags,
        "non_person_risk": round(non_person_risk, 5),
        **source_text,
        "face_box": face_box,
        "person_box": person_box,
        "top_labels": [
            {
                "name": name,
                "confidence": round(confidence, 5),
            }
            for name, confidence in sorted(
                labels.items(),
                key=lambda item: item[1],
                reverse=True,
            )[:12]
        ],
    }


def _crop_mapping(
    source_size: tuple[int, int],
    focal_x: float,
    focal_y: float,
    box: dict[str, float] | None,
) -> dict[str, float] | None:
    if not box:
        return None
    source_width, source_height = source_size
    scale = max(360 / source_width, 360 / source_height)
    scaled_width = source_width * scale
    scaled_height = source_height * scale
    crop_left = (scaled_width - 360) * focal_x
    crop_top = (scaled_height - 360) * focal_y
    left = (box["x"] * source_width * scale - crop_left) / 360
    top_source = 1.0 - box["y"] - box["height"]
    top = (top_source * source_height * scale - crop_top) / 360
    width = box["width"] * source_width * scale / 360
    height = box["height"] * source_height * scale / 360
    return {
        "x": round(max(0.0, min(1.0, left)), 5),
        "y": round(max(0.0, min(1.0, top)), 5),
        "width": round(max(0.0, min(1.0, width)), 5),
        "height": round(max(0.0, min(1.0, height)), 5),
    }


def _box_overlap(
    box: dict[str, float] | None,
    zone: tuple[float, float, float, float],
) -> float:
    if not box:
        return 0.0
    bx0, by0 = box["x"], box["y"]
    bx1 = bx0 + box["width"]
    by1 = by0 + box["height"]
    zx0, zy0, zx1, zy1 = zone
    width = max(0.0, min(bx1, zx1) - max(bx0, zx0))
    height = max(0.0, min(by1, zy1) - max(by0, zy0))
    zone_area = max(0.001, (zx1 - zx0) * (zy1 - zy0))
    return width * height / zone_area


def _zone_complexity(
    image: Image.Image,
    zone: tuple[float, float, float, float],
) -> float:
    width, height = image.size
    x0, y0, x1, y1 = zone
    crop = image.crop(
        (
            round(x0 * width),
            round(y0 * height),
            round(x1 * width),
            round(y1 * height),
        )
    ).convert("L")
    values = np.asarray(crop, dtype=np.float32)
    if not values.size:
        return 9.0
    edges = np.asarray(
        crop.filter(ImageFilter.FIND_EDGES),
        dtype=np.float32,
    )
    return float(values.std() / 58.0 + edges.mean() / 42.0)


def _palette_name(image: Image.Image) -> str:
    values = np.asarray(image.resize((64, 64)), dtype=np.float32) / 255.0
    mean = values.reshape(-1, 3).mean(axis=0)
    hue, saturation, _ = colorsys.rgb_to_hsv(*mean.tolist())
    if saturation < 0.095:
        return "neutral_ink"
    if hue < 0.06 or hue >= 0.92:
        return "soft_rose"
    if hue < 0.20:
        return "warm_clay"
    if hue < 0.48:
        return "soft_sage"
    if hue < 0.72:
        return "deep_blue"
    return "soft_rose"


def _layout_analysis(
    source_path: Path,
    vision: dict[str, Any],
    *,
    prefer_wide_text: bool,
) -> dict[str, Any]:
    face_box = vision.get("face_box")
    person_box = vision.get("person_box")
    primary_box = face_box or person_box
    if primary_box:
        subject_x = primary_box["x"] + primary_box["width"] / 2
        subject_y = 1.0 - (
            primary_box["y"] + primary_box["height"] / 2
        )
    else:
        subject_x, subject_y = 0.5, 0.42
    focal_x = max(0.34, min(0.66, subject_x))
    focal_y = max(0.30, min(0.62, subject_y))

    with Image.open(source_path) as opened:
        source = ImageOps.exif_transpose(opened).convert("RGB")
        source_size = source.size
        if face_box:
            # The area/name plate occupies the top of every banner. For a
            # portrait whose face is already near the source top, the former
            # 0.30 minimum centering cropped upward and placed the face under
            # that plate. Reduce the top crop only as much as needed to keep
            # the face center below the protected header zone.
            source_width, source_height = source_size
            scale = max(360 / source_width, 360 / source_height)
            scaled_height = source_height * scale
            crop_travel = max(0.0, scaled_height - 360)
            if crop_travel > 0:
                face_center_px = subject_y * scaled_height
                safe_crop_top = max(
                    0.0,
                    face_center_px - 360 * 0.22,
                )
                safe_focal_y = min(
                    1.0,
                    safe_crop_top / crop_travel,
                )
                focal_y = min(focal_y, safe_focal_y)
        crop = ImageOps.fit(
            source,
            (360, 360),
            method=Image.Resampling.LANCZOS,
            centering=(focal_x, focal_y),
        )
    mapped_box = _crop_mapping(
        source_size,
        focal_x,
        focal_y,
        primary_box,
    )
    if mapped_box:
        mapped_x = mapped_box["x"] + mapped_box["width"] / 2
        mapped_y = mapped_box["y"] + mapped_box["height"] / 2
    else:
        mapped_x, mapped_y = subject_x, subject_y

    zones = {
        "copy_left": (0.00, 0.10, 0.57, 0.92),
        "copy_right": (0.43, 0.10, 1.00, 0.92),
        "bottom_editorial": (0.04, 0.55, 0.96, 1.00),
        "top_editorial": (0.04, 0.00, 0.96, 0.45),
    }
    scores: dict[str, float] = {}
    for name, zone in zones.items():
        score = _zone_complexity(crop, zone)
        score += _box_overlap(mapped_box, zone) * 5.8
        scores[name] = score

    if mapped_x >= 0.57:
        scores["copy_left"] -= 0.72
        scores["copy_right"] += 0.42
    elif mapped_x <= 0.43:
        scores["copy_right"] -= 0.72
        scores["copy_left"] += 0.42
    if mapped_y <= 0.48:
        scores["bottom_editorial"] -= 0.66
        scores["top_editorial"] += 0.40
    elif mapped_y >= 0.62:
        scores["top_editorial"] -= 0.66
        scores["bottom_editorial"] += 0.40
    if prefer_wide_text:
        scores["copy_left"] += 0.85
        scores["copy_right"] += 0.85
        scores["bottom_editorial"] -= 0.18
    layout = min(scores, key=scores.get)

    ordered = sorted(scores.items(), key=lambda item: item[1])
    reason = (
        f"人物中心=({mapped_x:.2f},{mapped_y:.2f})、"
        f"文字領域の複雑度と人物重なりを比較し"
        f"{layout}を選択（次点との差={ordered[1][1] - ordered[0][1]:.2f}）"
    )
    return {
        "layout": layout,
        "layout_reason": reason,
        "palette": _palette_name(crop),
        "focal_x": round(focal_x, 5),
        "focal_y": round(focal_y, 5),
        "subject_box_after_crop": mapped_box,
        "subject_center_after_crop": {
            "x": round(mapped_x, 5),
            "y": round(mapped_y, 5),
        },
        "zone_scores": {
            key: round(value, 5) for key, value in scores.items()
        },
    }


def _industry(
    row: dict[str, str],
    info: PageInfo,
) -> tuple[str, dict[str, float]]:
    def without_negative_mens_service(value: str) -> str:
        """Do not turn an unavailable men's menu into a men's business."""
        patterns = (
            r"(?:メンズ|男性|men'?s)[^。.!！?\n]{0,32}"
            r"(?:予約不可|受付不可|対応不可|非対応|"
            r"受け付けていません|受付していません)",
            r"(?:予約不可|受付不可|対応不可|非対応)[^。.!！?\n]{0,20}"
            r"(?:メンズ|男性|men'?s)",
        )
        output = value
        for pattern in patterns:
            output = re.sub(pattern, " ", output, flags=re.I)
        return _clean_text(output)

    sources = {
        "title": without_negative_mens_service(
            _clean_text(row.get("title"))
        ),
        "description": without_negative_mens_service(
            _clean_text(row.get("description"))
        ),
        "salon": without_negative_mens_service(
            _clean_text(info.salon_name)
        ),
        "page": without_negative_mens_service(
            _clean_text(info.source_text)
        ),
    }
    weights = {
        "title": 3.0,
        "description": 6.0,
        "salon": 2.0,
        "page": 0.50,
    }
    scores = {name: 0.0 for name in INDUSTRY_PATTERNS}
    for industry, patterns in INDUSTRY_PATTERNS.items():
        for source_name, text in sources.items():
            if not text:
                continue
            hits = sum(bool(pattern.search(text)) for pattern in patterns)
            scores[industry] += min(2, hits) * weights[source_name]

    # "ヘアエステ" describes a hair menu rather than an esthetic business.
    hair_esthe_hits = sum(
        text.count("ヘアエステ") for text in sources.values()
    )
    scores["esthetic"] = max(
        0.0,
        scores["esthetic"] - hair_esthe_hits * 3.0,
    )

    # A post/menu description describes the advertised item more precisely
    # than a multi-service salon name.  Resolve an explicit hair service here
    # before business-name rules such as "hair.nail.eye" are considered.
    description = sources["description"]
    description_has_hair = bool(
        re.search(
            r"縮毛|髪質改善|ストレート|カット|カラー|ブリーチ|"
            r"ハイライト|パーマ|ショート|ボブ|ヘアスタイル",
            description,
            re.I,
        )
    )
    description_has_other_service = bool(
        re.search(
            r"ネイル|ジェル|深爪|美爪|まつ毛|まつげ|眉毛|"
            r"アイラッシュ|アイブロウ|脱毛|フェイシャル|コルギ",
            description,
            re.I,
        )
    )
    primary = "\n".join([sources["title"], sources["salon"]])
    mens_specific_primary = bool(
        re.search(
            r"メンズサロン|メンズ専門|男性専門|"
            r"\bmen'?s\b|\bbarber(?:shop)?\b|理容",
            primary,
            re.I,
        )
    )
    if description_has_hair and not description_has_other_service:
        mens_specific_description = bool(
            re.search(
                r"メンズ(?:専門|専用|限定|向け|サロン)|"
                r"男性(?:専門|専用|限定|向け)|理容|"
                r"\bmen'?s\s*(?:only|salon)\b|\bbarber\b",
                description,
                re.I,
            )
        )
        return (
            "hair_mens"
            if mens_specific_primary or mens_specific_description
            else "hair"
        ), {
            key: round(value, 2) for key, value in scores.items()
        }

    # Explicit business names are more reliable than generic menu words in a
    # long description. Resolve them first so, for example, an eyesalon is
    # never given hair copy merely because its feed body also mentions hair.
    explicit_nail = bool(
        re.search(r"ネイルサロン|\bnail(?:\s*salon)?\b", primary, re.I)
    )
    explicit_eye = bool(
        re.search(
            r"まつ毛|まつげ|アイラッシュ|アイブロウ|アイサロン|"
            r"\beyelash\b|\beyebrow\b|\beye\s*salon\b|\beyesalon\b",
            primary,
            re.I,
        )
    )
    if explicit_nail and explicit_eye:
        scores["eye_nail"] = round(
            max(scores["nail"], scores["eye"]),
            2,
        )
        return "eye_nail", {
            key: round(value, 2) for key, value in scores.items()
        }
    if re.search(
        r"ドライヘッドスパ|ヘッドスパ専門|頭浸浴|頭ほぐし",
        primary,
        re.I,
    ):
        return "head_spa", {
            key: round(value, 2) for key, value in scores.items()
        }
    if explicit_eye:
        return "eye", {
            key: round(value, 2) for key, value in scores.items()
        }
    if explicit_nail:
        return "nail", {
            key: round(value, 2) for key, value in scores.items()
        }
    if re.search(
        r"エステサロン|エステ専門|脱毛サロン|コルギ|フェムケア",
        primary,
        re.I,
    ):
        return "esthetic", {
            key: round(value, 2) for key, value in scores.items()
        }
    if re.search(
        r"メンズサロン|\bmen'?s\b|\bbarber(?:shop)?\b|理容",
        primary,
        re.I,
    ):
        return "hair_mens", {
            key: round(value, 2) for key, value in scores.items()
        }
    if (
        scores["nail"] >= 6.0
        and scores["eye"] >= 6.0
    ):
        scores["eye_nail"] = round(
            (scores["nail"] + scores["eye"]) / 2,
            2,
        )
        return "eye_nail", {
            key: round(value, 2) for key, value in scores.items()
        }
    # A single "メンズカットも対応" mention in a general salon must not
    # switch every creative to men's copy. Dedicated men's businesses were
    # already resolved from the title/salon name above.
    winner = max(
        (name for name in scores if name != "hair_mens"),
        key=scores.get,
    )
    if winner != "hair" and scores[winner] < 3.0:
        winner = "hair"
    return winner, {key: round(value, 2) for key, value in scores.items()}


def _headline(
    industry: str,
    row: dict[str, str],
    info: PageInfo,
) -> list[str]:
    text = "\n".join(
        [
            _clean_text(row.get("title")),
            _clean_text(row.get("description")),
            _clean_text(info.source_text),
        ]
    )
    for pattern, lines in HEADLINES[industry]:
        if pattern.search(text):
            return list(lines)
    return list(DEFAULT_HEADLINES[industry])


def _prefecture_short(value: str) -> str:
    return re.sub(r"(都|道|府|県)$", "", _clean_text(value))


def _station_name(value: str) -> str:
    station = value.strip(" 　「」『』【】").removesuffix("駅")
    station = re.sub(
        (
            r"^(?:JR|地下鉄|東京メトロ|大阪メトロ|名鉄|近鉄|"
            r"阪急|阪神|京阪|東急|西武|東武|ゆいレール)+"
        ),
        "",
        station,
    )
    if "線" in station:
        after_line = station.rsplit("線", 1)[-1]
        if after_line:
            station = after_line
    return station[-18:].strip()


def _station_hits(value: str) -> list[tuple[str, str]]:
    text = _clean_text(value).translate(FULLWIDTH_DIGITS)
    hits: list[tuple[str, str]] = []
    token_pattern = re.compile(
        (
            r"[「『【](?P<quoted>[^」』】]{1,24})[」』】]\s*駅?"
            r"|(?P<plain>[A-Za-z0-9一-龯ぁ-んァ-ヶー・〈〉]{1,28})駅"
        )
    )
    separators = re.compile(r"[/／|,、]")
    for match in token_pattern.finditer(text):
        station = _station_name(
            match.group("quoted") or match.group("plain") or ""
        )
        tail = text[match.end():match.end() + 32]
        separator = separators.search(tail)
        if separator:
            tail = tail[:separator.start()]
        travel = re.search(
            (
                r"(?P<mode>徒歩|歩いて|車で|車|バスで|バス)"
                r"\s*(?:約)?\s*(?P<number>[0-9]{1,2})"
                r"\s*(?P<unit>分|秒)"
            ),
            tail,
        )
        if travel:
            raw_mode = travel.group("mode")
            if raw_mode.startswith("車"):
                mode = "車"
            elif raw_mode.startswith("バス"):
                mode = "バス"
            else:
                mode = "徒歩"
            if travel.group("number") == "0":
                detail = "すぐ"
            else:
                detail = (
                    f"{mode}{travel.group('number')}{travel.group('unit')}"
                )
        else:
            implicit = re.search(
                (
                    r"(?:より|から|\s)?\s*(?:約)?"
                    r"(?P<number>[0-9]{1,2})\s*(?P<unit>分|秒)"
                ),
                tail,
            )
            if implicit:
                detail = (
                    "すぐ"
                    if implicit.group("number") == "0"
                    else (
                        f"徒歩{implicit.group('number')}"
                        f"{implicit.group('unit')}"
                    )
                )
            elif re.search(r"すぐ|目の前|出口左上", tail):
                detail = "出口すぐ" if "出口" in tail else "すぐ"
            else:
                continue
        if station and (station, detail) not in hits:
            hits.append((station, detail))
    return hits[:2]


def _location_access_lines(value: str) -> list[str]:
    text = _clean_text(value).translate(FULLWIDTH_DIGITS)
    if not text or re.search(r"徒歩\s*分", text):
        return []
    segments = [
        segment.strip(" 　・")
        for segment in re.split(r"//+|[/／|]", text)
        if segment.strip(" 　・")
    ]
    output: list[str] = []
    for segment in segments:
        if not re.search(
            r"徒歩|歩いて|車で|バス停|[0-9]{1,2}\s*(?:分|秒)|すぐ|目の前|出口",
            segment,
        ):
            continue
        compact = (
            segment
            .replace("徒歩約", "徒歩")
            .replace("車で約", "車で")
            .replace("ご徒歩", "徒歩")
        )
        if len(compact) <= 30 and compact not in output:
            output.append(compact)
        if len(output) >= 2:
            break
    return output


def _access_copy(
    row: dict[str, str],
    info: PageInfo,
) -> list[str]:
    title = _clean_text(row.get("title"))
    feed_prefix = _feed_location_prefix(title, info.salon_name)
    location = _clean_text(info.location)
    combined = " / ".join(
        value for value in (feed_prefix, location, title) if value
    )
    hits = _station_hits(combined)
    if hits:
        return [
            (
                f"{station}駅すぐ"
                if detail == "すぐ"
                else f"{station}駅 {detail}"
            )
            for station, detail in hits
        ]

    location_lines = _location_access_lines(location)
    if location_lines:
        return location_lines

    stations = [
        _clean_text(value).removesuffix("駅")
        for value in info.nearest_stations
        if _clean_text(value)
    ]
    if stations:
        lines = [f"{stations[0]}駅からアクセス"]
        if len(stations) > 1:
            lines.append(f"{stations[1]}駅からも便利")
        return lines[:2]

    city = _clean_text(row.get("address.city") or info.city)
    street = _clean_text(row.get("address.street_address"))
    if city and street:
        normalized_street = street
        if normalized_street.startswith(city):
            normalized_street = normalized_street[len(city):]
        value = f"{city}{normalized_street}".strip()
        return [value[:28]]
    if city:
        return [f"{city}エリア"]
    region = _clean_text(row.get("address.region") or info.region)
    if region:
        return [f"{region}内"]
    raise CatalogBuildError(
        f"{row.get('id')}: access could not be resolved"
    )


def _area_copy(
    row: dict[str, str],
    info: PageInfo,
    access: list[str],
) -> str:
    prefix = _prefecture_short(row.get("address.region") or info.region)
    station_match = re.search(r"(.{1,14})駅", access[0])
    place = (
        station_match.group(1).strip()
        if station_match
        else _clean_text(row.get("address.city") or info.city)
    )
    values: list[str] = []
    for value in (prefix, place):
        if value and value not in values:
            values.append(value)
    return "・".join(values) or "Hairbook掲載エリア"


def _display_salon_name(value: str) -> str:
    """Keep the canonical brand/branch while dropping search-menu suffixes."""
    name = _clean_text(_clean_salon_name(value))
    service_prefix = re.match(
        r"^(?P<prefix>.{1,32}?サロン)\s+(?P<brand>.+)$",
        name,
        re.I,
    )
    if (
        service_prefix
        and re.search(
            r"眉毛|まつ毛|まつげ|アイブロウ|アイラッシュ|"
            r"eyebrow|eyelash|ネイル|nail|髪質|縮毛|カラー",
            service_prefix.group("prefix"),
            re.I,
        )
    ):
        name = service_prefix.group("brand").strip()
    name = re.sub(
        (
            r"^(?:大人の美髪)?(?:髪質改善|縮毛矯正|カラー|デザイン)"
            r"(?:&カラー)?(?:特化型)?サロン\s+"
        ),
        "",
        name,
        flags=re.I,
    ).strip()
    parts = [
        part.strip()
        for part in re.split(r"\s*/\s*", name)
        if part.strip()
    ]
    if not parts:
        return "Hairbook掲載サロン"
    output = parts[0]
    for suffix in parts[1:]:
        cleaned = re.sub(
            (
                r"^(?:大人の美髪)?(?:髪質改善|縮毛矯正|カラー|"
                r"デザイン)(?:&カラー)?(?:特化型)?サロン\s+"
            ),
            "",
            suffix,
            flags=re.I,
        ).strip()
        if re.search(
            r"まつ毛|まつげ|マツエク|眉毛|アイブロウ|"
            r"ネイル|縮毛|髪質|カラー|パーマ|カット",
            cleaned,
            re.I,
        ):
            break
        if cleaned and cleaned != output:
            candidate = f"{output} / {cleaned}"
            if len(candidate) <= 52:
                output = candidate
            break
    return output[:80].strip() or "Hairbook掲載サロン"


def _copy_payload(
    row: dict[str, str],
    info: PageInfo,
) -> tuple[dict[str, Any], dict[str, Any]]:
    industry, industry_scores = _industry(row, info)
    access = _access_copy(row, info)
    salon_name = _display_salon_name(info.salon_name)
    copy = {
        "industry": industry,
        "area": _area_copy(row, info, access),
        "salon_name": salon_name,
        "salon_name_full": _clean_text(info.salon_name),
        "access": access,
        "headline": _headline(industry, row, info),
        "cta": "",
    }
    flattened = json.dumps(copy, ensure_ascii=False)
    if HTML_TAG_RE.search(flattened) or HTML_ENTITY_RE.search(flattened):
        raise CatalogBuildError(
            f"{row.get('id')}: HTML remained after copy cleaning"
        )
    if not copy["access"] or any(
        "アクセスを確認" in line for line in copy["access"]
    ):
        raise CatalogBuildError(
            f"{row.get('id')}: access fallback is not acceptable"
        )
    explicit_service = {
        "hair_mens": r"メンズ",
        "nail": r"ネイル",
        "eye": r"目元|アイ",
        "eye_nail": r"目元.*指先|指先.*目元",
        "head_spa": r"頭|ヘッドスパ",
        "esthetic": r"素肌|ケア",
    }
    if industry in explicit_service and not re.search(
        explicit_service[industry],
        " ".join(copy["headline"]),
    ):
        raise CatalogBuildError(
            f"{row.get('id')}: headline does not match {industry}"
        )
    return copy, {
        "industry_scores": industry_scores,
        "feed_title_clean": _clean_text(row.get("title")),
        "feed_description_clean": _clean_text(
            row.get("description"),
            preserve_lines=True,
        ),
        "access_sources": {
            "page_location": _clean_text(info.location),
            "feed_title": _clean_text(row.get("title")),
            "feed_city": _clean_text(row.get("address.city")),
            "feed_street": _clean_text(row.get("address.street_address")),
        },
    }


def _asset_id(product_id: str) -> str:
    parsed = parse_product_id(product_id)
    suffix = hashlib.sha256(product_id.encode("utf-8")).hexdigest()[:14]
    return f"product-{parsed['salon_id']}-{suffix}-person-square-v1"


def _build_asset(
    row: dict[str, str],
    info: PageInfo,
    selected: dict[str, Any],
    source_options: list[dict[str, Any]],
    manifest_dir: Path,
    selection_scope: str,
) -> dict[str, Any]:
    product_id = str(row["id"])
    parsed = parse_product_id(product_id)
    prepared = selected["prepared"]
    candidate: SourceCandidate = prepared["candidate"]
    copy, copy_audit = _copy_payload(row, info)
    prefer_wide = (
        len(copy["salon_name"]) > 20
        or max(map(len, copy["headline"])) > 13
        or max(map(len, copy["access"])) > 18
    )
    layout = _layout_analysis(
        prepared["path"],
        selected["vision"],
        prefer_wide_text=prefer_wide,
    )
    option_payloads: list[dict[str, Any]] = []
    for option in source_options:
        option_prepared = option["prepared"]
        option_candidate: SourceCandidate = option["candidate"]
        option_layout = _layout_analysis(
            option_prepared["path"],
            option["vision"],
            prefer_wide_text=prefer_wide,
        )
        option_payloads.append(
            {
                "option_id": (
                    f"{option_candidate.source_type}:"
                    f"{option_candidate.record_id}"
                ),
                "source": {
                    "path": _manifest_source_path(
                        manifest_dir,
                        option_prepared["path"],
                    ),
                    "page_url": option_candidate.page_url,
                    "image_url": option_candidate.image_url,
                    "resolved_image_url": option_prepared["final_url"],
                    "sha256": option_prepared["sha256"],
                    "source_type": option_candidate.source_type,
                    "record_id": option_candidate.record_id,
                    "metadata_text": _clean_text(
                        option_candidate.metadata_text
                    ),
                    "width": option_prepared["width"],
                    "height": option_prepared["height"],
                    "vision": option["vision"],
                },
                "image": {
                    "focal_x": option_layout["focal_x"],
                    "focal_y": option_layout["focal_y"],
                    "crop_box": option.get("source_crop"),
                    "brightness": 1.0,
                    "saturation": 0.97,
                    "contrast": 1.03,
                },
                "layout": option_layout["layout"],
                "layout_reason": option_layout["layout_reason"],
                "theme": option_layout["palette"],
            }
        )
    return {
        "asset_id": _asset_id(product_id),
        "salon_id": parsed["salon_id"],
        **(
            {"stylist_id": parsed["stylist_id"]}
            if parsed["stylist_id"]
            else {}
        ),
        "product_ids": [product_id],
        "landing_url": _landing_key(row["link"]),
        "theme": layout["palette"],
        "layout": layout["layout"],
        "layout_reason": layout["layout_reason"],
        "source": {
            "path": _manifest_source_path(
                manifest_dir,
                prepared["path"],
            ),
            "page_url": candidate.page_url,
            "image_url": candidate.image_url,
            "resolved_image_url": prepared["final_url"],
            "sha256": prepared["sha256"],
            "source_type": candidate.source_type,
            "record_id": candidate.record_id,
            "metadata_text": _clean_text(candidate.metadata_text),
            "subject_scope": (
                "stylist" if parsed["stylist_id"] else "salon"
            ),
            "selection_scope": selection_scope,
            "width": prepared["width"],
            "height": prepared["height"],
            "vision": selected["vision"],
        },
        "image": {
            "focal_x": layout["focal_x"],
            "focal_y": layout["focal_y"],
            "crop_box": selected.get("source_crop"),
            "brightness": 1.0,
            "saturation": 0.97,
            "contrast": 1.03,
        },
        "visual_analysis": {
            "layout": layout["layout"],
            "layout_reason": layout["layout_reason"],
            "palette": layout["palette"],
            "subject_box_after_crop": layout[
                "subject_box_after_crop"
            ],
            "subject_center_after_crop": layout[
                "subject_center_after_crop"
            ],
            "zone_scores": layout["zone_scores"],
        },
        "source_options": option_payloads,
        "copy": copy,
        "copy_audit": copy_audit,
    }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _candidate_record(
    candidate: SourceCandidate,
    prepared_by_url: dict[str, dict[str, Any]],
    vision_by_path: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    prepared = prepared_by_url.get(candidate.image_url)
    if not prepared:
        return None
    analysis = vision_by_path.get(str(Path(prepared["path"]).resolve()))
    vision = _score_source(prepared, analysis)
    return {
        "candidate": candidate,
        "prepared": prepared,
        "vision": vision,
    }


def _nail_candidate_evidence(candidate: SourceCandidate) -> float:
    text = _clean_text(candidate.metadata_text)
    if not text:
        return 0.0
    nail_hits = len(
        re.findall(
            r"深爪|美爪|爪|ジェル|フットネイル|ネイル|nail",
            text,
            re.I,
        )
    )
    hair_hits = len(
        re.findall(
            r"縮毛|髪質|カット|カラー|ブリーチ|ハイライト|"
            r"パーマ|ショート|ボブ|ヘア|hair",
            text,
            re.I,
        )
    )
    if nail_hits == 0 or hair_hits > 0:
        return 0.0
    return min(1.0, 0.55 + nail_hits * 0.15)


def _ocr_crop_overlap(
    crop_box: tuple[float, float, float, float],
    text_boxes: list[dict[str, Any]],
) -> float:
    left, top, right, bottom = crop_box
    overlap = 0.0
    for box in text_boxes:
        box_left = float(box.get("x") or 0)
        box_top = (
            1.0
            - float(box.get("y") or 0)
            - float(box.get("height") or 0)
        )
        box_right = box_left + float(box.get("width") or 0)
        box_bottom = box_top + float(box.get("height") or 0)
        overlap += max(
            0.0,
            min(right, box_right) - max(left, box_left),
        ) * max(
            0.0,
            min(bottom, box_bottom) - max(top, box_top),
        )
    return overlap


def _nail_crop_box(
    prepared: dict[str, Any],
    vision: dict[str, Any],
) -> tuple[list[float], float]:
    width = int(prepared["width"])
    height = int(prepared["height"])
    text_boxes = list(vision.get("text_boxes") or [])
    after_boxes = [
        box
        for box in text_boxes
        if re.search(
            r"\bafter\b|アフター|施術後",
            str(box.get("text") or ""),
            re.I,
        )
    ]
    if after_boxes:
        # Before/after source images are allowed only after isolating the
        # completed result below the "after" label.  A non-square source crop
        # is intentional here; the renderer performs the final 1:1 fit.
        top = max(
            1.0 - float(box.get("y") or 0) + 0.018
            for box in after_boxes
        )
        top = max(0.0, min(0.68, top))
        crop = (0.0, top, 1.0, 1.0)
        overlap = _ocr_crop_overlap(crop, text_boxes)
        return (
            [round(value, 5) for value in crop],
            round(overlap, 5),
        )
    if height >= width:
        crop_height = min(1.0, width / max(1, height))
        maximum_top = max(0.0, 1.0 - crop_height)
        candidates = [
            maximum_top * index / 20
            for index in range(21)
        ]
        top = min(
            candidates,
            key=lambda value: (
                _ocr_crop_overlap(
                    (0.0, value, 1.0, value + crop_height),
                    text_boxes,
                ),
                -value,
            ),
        )
        crop = (0.0, top, 1.0, top + crop_height)
    else:
        crop_width = min(1.0, height / max(1, width))
        maximum_left = max(0.0, 1.0 - crop_width)
        candidates = [
            maximum_left * index / 20
            for index in range(21)
        ]
        left = min(
            candidates,
            key=lambda value: (
                _ocr_crop_overlap(
                    (value, 0.0, value + crop_width, 1.0),
                    text_boxes,
                ),
                abs(value - maximum_left / 2),
            ),
        )
        crop = (left, 0.0, left + crop_width, 1.0)
    overlap = _ocr_crop_overlap(crop, text_boxes)
    return [round(value, 5) for value in crop], round(overlap, 5)


def _approve_nail_candidate(
    item: dict[str, Any],
) -> dict[str, Any] | None:
    candidate: SourceCandidate = item["candidate"]
    evidence = _nail_candidate_evidence(candidate)
    vision = item["vision"]
    if (
        evidence <= 0
        or "style_photo" not in candidate.source_type
        or vision.get("treatment_flags")
        or float(vision.get("treatment_risk") or 0) > 0.35
        or float(vision.get("non_person_risk") or 0) > 0.90
    ):
        return None
    crop_box, text_overlap = _nail_crop_box(item["prepared"], vision)
    nail_vision = dict(vision)
    nail_vision.update(
        {
            "status": "approved",
            "reason": "ネイル完成写真として承認（文字領域をクロップ）",
            "nail_service_approved": True,
            "nail_service_score": round(evidence, 5),
            "text_after_crop_overlap": text_overlap,
            "text_after_crop_clear": text_overlap < 0.0015,
            "score": round(
                7.0
                + evidence
                + min(
                    item["prepared"]["width"],
                    item["prepared"]["height"],
                )
                / 1800
                - text_overlap * 30,
                5,
            ),
        }
    )
    if not nail_vision["text_after_crop_clear"]:
        return None
    return {
        **item,
        "vision": nail_vision,
        "source_crop": crop_box,
    }


def _selected_pool(
    candidates: list[dict[str, Any]],
    industry: str = "hair",
) -> list[dict[str, Any]]:
    def face_area(item: dict[str, Any]) -> float:
        box = item["vision"].get("face_box") or {}
        return float(box.get("width") or 0) * float(
            box.get("height") or 0
        )

    if industry == "nail":
        eligible = [
            approved
            for item in candidates
            if (approved := _approve_nail_candidate(item)) is not None
        ]
    else:
        eligible = [
            item
            for item in candidates
            if item["vision"]["status"] == "approved"
            and item["vision"]["treatment_risk"] <= 0.25
            and not item["vision"]["treatment_flags"]
            and not item["vision"].get("text_flags")
            and item["vision"]["non_person_risk"] <= 0.55
        ]
    if industry in {"hair", "hair_mens"}:
        clean_face_style_candidates = [
            item
            for item in eligible
            if "style_photo" in item["candidate"].source_type
            and item["vision"]["person_score"] >= 0.55
            and item["vision"]["face_score"] >= 0.58
            and face_area(item) >= 0.012
        ]
        portrait_candidates = [
            item
            for item in eligible
            if item["candidate"].source_type
            in {
                "hairbook_staff_profile_photo",
                "hairbook_staff_profile_background_clean",
                "official_salon_person_photo",
            }
            and item["vision"]["person_score"] >= 0.50
            and item["vision"]["face_score"] >= 0.50
            and face_area(item) >= 0.008
        ]
        clean_face_candidates = [
            item
            for item in eligible
            if item["vision"]["person_score"] >= 0.52
            and item["vision"]["face_score"] >= 0.52
            and face_area(item) >= 0.012
        ]
        finished_style_back_views = []
        for item in eligible:
            if (
                "style_photo" not in item["candidate"].source_type
                or item["vision"]["person_score"] < 0.58
            ):
                continue
            approved = dict(item)
            approved_vision = dict(item["vision"])
            approved_vision.update(
                {
                    "reason": (
                        "顔主役候補がないため、人物を含む完成ヘアの"
                        "後ろ姿として承認"
                    ),
                    "hair_finished_style_back_view_approved": True,
                }
            )
            approved["vision"] = approved_vision
            finished_style_back_views.append(approved)
        if clean_face_style_candidates:
            # Face-led finished styles communicate the service while avoiding
            # back-of-head-only and in-treatment photography.
            eligible = clean_face_style_candidates
        elif portrait_candidates:
            eligible = portrait_candidates
        elif clean_face_candidates:
            eligible = clean_face_candidates
        elif finished_style_back_views:
            # A completed hairstyle photographed from behind is still a
            # person/model service result. It is allowed only after clear
            # face-led candidates are exhausted; hand/product/interior-only
            # imagery remains excluded by the source eligibility checks.
            eligible = finished_style_back_views
        else:
            eligible = []
    elif industry != "nail":
        portrait_candidates = [
            item
            for item in eligible
            if item["candidate"].source_type
            in {
                "hairbook_staff_profile_photo",
                "hairbook_staff_profile_background_clean",
                "official_salon_person_photo",
            }
            and item["vision"]["person_score"] >= 0.50
            and item["vision"]["face_score"] >= 0.45
            and face_area(item) >= 0.008
        ]
        clean_face_models = [
            item
            for item in eligible
            if item["vision"]["person_score"] >= 0.50
            and item["vision"]["face_score"] >= 0.56
            and face_area(item) >= 0.012
            and item["vision"]["treatment_risk"] <= 0.30
        ]
        if portrait_candidates:
            eligible = portrait_candidates
        elif clean_face_models:
            # For eye, nail, and esthetic products, a clear upright face or
            # staff portrait is safer than a service close-up or treatment
            # demonstration.
            eligible = clean_face_models
        else:
            # A body-part/service close-up can pass coarse "person" labels.
            # Do not keep that broad fallback: allow the caller to try a
            # same-salon portrait pool or place the item on hold.
            eligible = []
        if industry == "eye_nail" and not eligible:
            # Mixed eye/nail salons may have no unobscured face portrait.
            # A verified finished nail result is still service evidence and
            # is preferable to a back-of-head or body-part-only photograph.
            eligible = [
                approved
                for item in candidates
                if (approved := _approve_nail_candidate(item)) is not None
            ]
    eligible.sort(
        key=lambda item: (
            item["vision"]["score"],
            item["vision"]["face_score"],
            item["vision"]["person_score"],
            min(
                item["prepared"]["width"],
                item["prepared"]["height"],
            ),
        ),
        reverse=True,
    )
    if not eligible:
        return []
    top_score = eligible[0]["vision"]["score"]
    selected: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()
    for item in eligible:
        source_hash = item["prepared"]["sha256"]
        if source_hash in seen_hashes:
            continue
        if selected and item["vision"]["score"] < top_score - 1.25:
            continue
        selected.append(item)
        seen_hashes.add(source_hash)
        if len(selected) >= MAX_PATTERNS_PER_LANDING:
            break
    return selected


def build_catalog(
    *,
    rows: list[dict[str, str]],
    output_dir: Path,
    workers: int,
    limit: int = 0,
    vision_tool: Path | None = None,
    vision_report: Path | None = None,
    source_overrides: Path | None = None,
    source_rejections: Path | None = None,
    reuse_sources: bool = False,
) -> dict[str, Any]:
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    source_dir = output_dir / "sources"
    active = _active_rows(rows)
    if limit:
        active = active[:limit]
    if not active:
        raise CatalogBuildError("feed contains no active products")

    grouped: dict[str, list[dict[str, str]]] = {}
    invalid_rows: dict[str, str] = {}
    for row in active:
        try:
            key = _landing_key(row["link"])
        except Exception as exc:
            key = f"invalid:{row.get('id', '')}"
            invalid_rows[str(row.get("id") or "")] = str(exc)
        grouped.setdefault(key, []).append(row)

    page_infos: dict[str, PageInfo] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_fetch_page_info, key, group[0]): key
            for key, group in grouped.items()
            if not key.startswith("invalid:")
        }
        for future in as_completed(futures):
            page_infos[futures[future]] = future.result()

    page_candidates: dict[str, list[SourceCandidate]] = {}
    override_candidates = _load_source_overrides(source_overrides)
    rejected_sources = _load_source_rejections(source_rejections)
    all_candidates: list[SourceCandidate] = []
    for key, group in grouped.items():
        info = page_infos.get(key)
        candidates = _page_source_candidates(
            landing_url=key,
            rows=group,
            info=info,
            override_candidates=override_candidates,
        )
        candidates = [
            candidate
            for candidate in candidates
            if (
                candidate.source_type,
                candidate.record_id,
            )
            not in rejected_sources
        ]
        page_candidates[key] = candidates
        all_candidates.extend(candidates)
    all_candidates = _dedupe_candidates(all_candidates)

    prepared_by_url, download_errors = _download_all_candidates(
        all_candidates,
        source_dir,
        workers,
        reuse_sources=reuse_sources,
    )
    vision_by_path, vision_report_path = _run_vision(
        source_dir,
        output_dir,
        vision_tool=vision_tool,
        vision_report=vision_report,
    )
    background_clean_variants = (
        _prepare_staff_background_clean_variants(
            page_candidates,
            prepared_by_url,
            vision_by_path,
            source_dir,
            output_dir,
            workers=workers,
        )
    )
    if background_clean_variants:
        # The second pass analyzes only newly created derivative files when
        # a reusable Vision report was supplied.
        vision_by_path, vision_report_path = _run_vision(
            source_dir,
            output_dir,
            vision_tool=vision_tool,
            vision_report=vision_report_path,
        )

    scored_by_page: dict[str, list[dict[str, Any]]] = {}
    scored_by_salon: dict[str, list[dict[str, Any]]] = {}
    source_audit: list[dict[str, Any]] = []
    seen_audit: set[str] = set()
    for key, candidates in page_candidates.items():
        info = page_infos.get(key)
        scored: list[dict[str, Any]] = []
        for candidate in candidates:
            item = _candidate_record(
                candidate,
                prepared_by_url,
                vision_by_path,
            )
            if not item:
                continue
            scored.append(item)
            if info:
                scored_by_salon.setdefault(info.salon_id, []).append(item)
            audit_key = item["prepared"]["sha256"]
            if audit_key not in seen_audit:
                source_audit.append(
                    {
                        "source_type": candidate.source_type,
                        "record_id": candidate.record_id,
                        "page_url": candidate.page_url,
                        "image_url": candidate.image_url,
                        "resolved_image_url": item["prepared"]["final_url"],
                        "path": str(item["prepared"]["path"]),
                        "sha256": item["prepared"]["sha256"],
                        "width": item["prepared"]["width"],
                        "height": item["prepared"]["height"],
                        "vision": item["vision"],
                    }
                )
                seen_audit.add(audit_key)
        scored_by_page[key] = scored

    salon_pools = {
        salon_id: _selected_pool(items)
        for salon_id, items in scored_by_salon.items()
    }
    page_pools = {
        key: _selected_pool(items)
        for key, items in scored_by_page.items()
    }

    assets: list[dict[str, Any]] = []
    assets_by_signature: dict[str, dict[str, Any]] = {}
    classifications: list[dict[str, Any]] = []
    next_review_on = (date.today() + timedelta(days=7)).isoformat()
    for key, group in grouped.items():
        info = page_infos.get(key)
        for index, row in enumerate(group):
            parsed = parse_product_id(row["id"])
            common = {
                "product_id": row["id"],
                "salon_id": parsed["salon_id"],
                "stylist_id": parsed["stylist_id"],
                "landing_url": row.get("link", ""),
                "current_image_url": row.get("image_link", ""),
                "feed_title_reference": _clean_text(row.get("title")),
            }
            row_industry = (
                _industry(row, info)[0] if info else "hair"
            )
            local_pool = _selected_pool(
                scored_by_page.get(key, []),
                row_industry,
            )
            staff_styles_only = bool(_staff_styles_source_url(key))
            salon_pool = (
                _selected_pool(
                    scored_by_salon.get(info.salon_id, []),
                    row_industry,
                )
                if info and not staff_styles_only
                else []
            )
            pool = local_pool or salon_pool
            selection_scope = (
                "landing_page"
                if local_pool
                else "same_salon_fallback"
                if salon_pool
                else ""
            )
            if info and not info.error and pool:
                selected = pool[index % len(pool)]
                item_selection_scope = selection_scope
                if (
                    selected["candidate"].source_type
                    == "official_salon_person_photo"
                ):
                    item_selection_scope = "official_salon_link_fallback"
                try:
                    asset = _build_asset(
                        row,
                        info,
                        selected,
                        pool,
                        output_dir,
                        item_selection_scope,
                    )
                except Exception as exc:
                    classifications.append(
                        {
                            **common,
                            "asset_id": "",
                            "eligibility_status": "copy_or_layout_hold",
                            "hold_reason": str(exc),
                            "next_review_on": next_review_on,
                            "source_page_url": "",
                            "source_image_url": "",
                        }
                    )
                    continue
                signature = json.dumps(
                    {
                        "salon_id": asset["salon_id"],
                        "stylist_id": asset.get("stylist_id", ""),
                        "landing_url": asset["landing_url"],
                        "source_sha256": asset["source"]["sha256"],
                        "theme": asset["theme"],
                        "layout": asset["layout"],
                        "image": asset["image"],
                        "copy": asset["copy"],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                existing = assets_by_signature.get(signature)
                if existing:
                    existing["product_ids"].append(row["id"])
                    asset = existing
                else:
                    assets_by_signature[signature] = asset
                    assets.append(asset)
                classifications.append(
                    {
                        **common,
                        "asset_id": asset["asset_id"],
                        "eligibility_status": "eligible",
                        "hold_reason": "",
                        "next_review_on": None,
                        "source_page_url": asset["source"]["page_url"],
                        "source_image_url": asset["source"]["image_url"],
                        "selection_scope": item_selection_scope,
                        "source_person_score": asset["source"]["vision"][
                            "person_score"
                        ],
                        "source_treatment_risk": asset["source"]["vision"][
                            "treatment_risk"
                        ],
                        "industry": asset["copy"]["industry"],
                        "layout": asset["layout"],
                    }
                )
            else:
                reasons = []
                if not info:
                    reasons.append(
                        invalid_rows.get(
                            str(row.get("id") or ""),
                            "着地URLを解決できません",
                        )
                    )
                elif info.error:
                    reasons.append(info.error)
                else:
                    reasons.append(
                        "人物・非施術中の基準を満たす既存画像がありません"
                    )
                candidates = scored_by_page.get(key, [])
                if candidates:
                    top = max(
                        candidates,
                        key=lambda item: item["vision"]["score"],
                    )
                    reasons.append(
                        f"最高候補: {top['vision']['reason']} "
                        f"(person={top['vision']['person_score']}, "
                        f"treatment={top['vision']['treatment_risk']})"
                    )
                elif page_candidates.get(key):
                    reasons.append("候補画像の取得または認識に失敗")
                classifications.append(
                    {
                        **common,
                        "asset_id": "",
                        "eligibility_status": "person_source_hold",
                        "hold_reason": " / ".join(reasons),
                        "next_review_on": next_review_on,
                        "source_page_url": "",
                        "source_image_url": "",
                    }
                )

    now = datetime.now(timezone.utc).isoformat()
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "design_version": DESIGN_VERSION,
        "environment": "review_draft",
        "account_scope": "HB_02",
        "generated_at": now,
        "output_format": {
            "mime_type": "image/jpeg",
            "width": 1080,
            "height": 1080,
            "review_width": 360,
            "review_height": 360,
        },
        "source_policy": {
            "priority": (
                "existing_hairbook_person_images_ranked_by_face_person_and_"
                "non_treatment_evidence"
            ),
            "max_patterns_per_landing": MAX_PATTERNS_PER_LANDING,
            "minimum_person_score": MIN_PERSON_SCORE,
            "maximum_treatment_risk": MAX_TREATMENT_RISK,
            "same_salon_fallback_allowed": True,
            "staff_landing_source": "matching_staff_styles_section_only",
            "staff_landing_same_salon_fallback_allowed": False,
            "staff_landing_profile_feed_override_fallback_allowed": False,
            "official_salon_link_fallback_allowed": bool(
                override_candidates
            ),
            "video_capture_used": False,
            "generated_person_used": False,
            "identity_edit_used": False,
            "background_replacement_used": (
                background_clean_variants > 0
            ),
            "background_replacement_method": (
                "apple_vision_person_segmentation_identity_preserving"
            ),
            "non_person_sources_allowed": (
                "finished_nail_closeup_only_for_nail_or_eye_nail_products"
            ),
            "treatment_scene_sources_allowed": False,
            "baked_text_sources_allowed": False,
            "manual_source_rejections": len(rejected_sources),
        },
        "copy_policy": {
            "html_removed": True,
            "industry_specific_headlines": True,
            "general_hair_gender_neutral": True,
            "cta_removed": True,
            "access_required": True,
            "zero_minute_access_rewritten_as_immediate": True,
            "access_placeholder_allowed": False,
        },
        "experiment": {
            "experiment_id": f"hb02_person_square_full_{date.today():%Y%m%d}",
            "rollout_batch": "full",
            "baseline": "28_complete_days_before_switch",
            "evaluation": "first_7_complete_days_then_14_complete_days",
            "primary_metrics": ["ctr", "cpm"],
            "secondary_metrics": ["meta_add_to_cart"],
            "aggregation": (
                "salon_id_x_creative_version_x_complete_day"
            ),
            "exclude": [
                "switch_day",
                "mixed_old_new_day",
                "incomplete_day",
            ],
        },
        "assets": assets,
    }

    eligible_products = sum(
        row["eligibility_status"] == "eligible"
        for row in classifications
    )
    summary = {
        "schema_version": SUMMARY_SCHEMA,
        "generated_at": now,
        "feed_active_products": len(active),
        "landing_pages": len(grouped),
        "salons": len(
            {
                parse_product_id(row["id"])["salon_id"]
                for row in active
                if parse_product_id(row["id"])["salon_id"]
            }
        ),
        "candidate_urls": len(all_candidates),
        "downloaded_candidates": len(prepared_by_url),
        "download_errors": len(download_errors),
        "vision_records": len(vision_by_path),
        "vision_approved_sources": sum(
            row["vision"]["status"] == "approved"
            for row in source_audit
        ),
        "vision_rejected_sources": sum(
            row["vision"]["status"] != "approved"
            for row in source_audit
        ),
        "assets": len(assets),
        "background_clean_variants": background_clean_variants,
        "eligible_products": eligible_products,
        "held_products": len(classifications) - eligible_products,
        "page_errors": sum(bool(info.error) for info in page_infos.values()),
        "unique_source_files": len(
            {asset["source"]["sha256"] for asset in assets}
        ),
        "same_salon_fallback_products": sum(
            row.get("selection_scope") == "same_salon_fallback"
            for row in classifications
        ),
        "official_link_fallback_products": sum(
            row.get("selection_scope") == "official_salon_link_fallback"
            for row in classifications
        ),
        "layouts": dict(
            Counter(asset["layout"] for asset in assets)
        ),
        "industries": dict(
            Counter(asset["copy"]["industry"] for asset in assets)
        ),
        "manifest": str((output_dir / "manifest.json").resolve()),
        "classification": str(
            (output_dir / "classification.json").resolve()
        ),
        "source_audit": str(
            (output_dir / "source_audit.json").resolve()
        ),
        "vision_report": str(vision_report_path.resolve()),
    }
    _write_json(output_dir / "manifest.json", manifest)
    _write_json(
        output_dir / "classification.json",
        {
            "schema_version": CLASSIFICATION_SCHEMA,
            "generated_at": now,
            "rows": classifications,
        },
    )
    _write_json(
        output_dir / "source_audit.json",
        {
            "schema_version": VISION_SCHEMA,
            "generated_at": now,
            "minimum_person_score": MIN_PERSON_SCORE,
            "maximum_treatment_risk": MAX_TREATMENT_RISK,
            "sources": source_audit,
            "download_errors": download_errors,
        },
    )
    _write_json(
        output_dir / "page_discovery.json",
        {
            "schema_version": DISCOVERY_SCHEMA,
            "generated_at": now,
            "pages": {
                key: {
                    "salon_id": info.salon_id if info else "",
                    "stylist_id": info.stylist_id if info else "",
                    "salon_name": info.salon_name if info else "",
                    "location": info.location if info else "",
                    "nearest_stations": (
                        info.nearest_stations if info else []
                    ),
                    "candidate_count": len(page_candidates.get(key, [])),
                    "approved_candidate_count": len(page_pools.get(key, [])),
                    "candidates": [
                        asdict(candidate)
                        for candidate in page_candidates.get(key, [])
                    ],
                    "error": info.error if info else "invalid landing URL",
                }
                for key, info in sorted(
                    (
                        (key, page_infos.get(key))
                        for key in grouped
                    ),
                    key=lambda item: item[0],
                )
            },
        },
    )
    _write_json(output_dir / "summary.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build a full 1:1 person-first catalog manifest with "
            "per-image editorial layouts."
        )
    )
    parser.add_argument("--feed-csv", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--vision-tool", type=Path)
    parser.add_argument("--vision-report", type=Path)
    parser.add_argument("--source-overrides", type=Path)
    parser.add_argument("--source-rejections", type=Path)
    parser.add_argument(
        "--reuse-sources",
        action="store_true",
        help="Reuse already downloaded candidate files in output-dir.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Testing only: process the first N active products.",
    )
    args = parser.parse_args()
    if args.workers < 1 or args.workers > 16:
        parser.error("--workers must be between 1 and 16")
    if args.limit < 0:
        parser.error("--limit must be non-negative")
    if args.vision_tool and args.vision_report:
        parser.error("--vision-tool and --vision-report are mutually exclusive")

    try:
        rows = (
            _read_feed_csv(args.feed_csv)
            if args.feed_csv
            else _fetch_feed()
        )
        summary = build_catalog(
            rows=rows,
            output_dir=args.output_dir,
            workers=args.workers,
            limit=args.limit,
            vision_tool=args.vision_tool,
            vision_report=args.vision_report,
            source_overrides=args.source_overrides,
            source_rejections=args.source_rejections,
            reuse_sources=args.reuse_sources,
        )
    except CatalogBuildError as exc:
        parser.exit(2, f"square catalog build failed: {exc}\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
