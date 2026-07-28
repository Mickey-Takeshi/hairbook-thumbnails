"""Render identity-preserving 1:1 editorial catalog banners.

The renderer never generates or alters a person.  It crops an approved
Hairbook photograph, applies restrained global tone adjustments, and places
exact Japanese copy using the per-image layout recorded in the manifest.
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

from person_v3 import (
    ManifestError,
    _canonical_sha,
    _fit_font,
    _fit_wrapped_text,
    _font,
    _hex_rgba,
    _resolve_source,
    _text_width,
    sha256_file,
)


W = 1080
H = 1080
MOBILE_W = 360
MOBILE_H = 360
DESIGN_VERSION = "person_square_editorial_v1"
SCHEMA_VERSION = "hairbook.person_square_manifest.v1"
RENDER_SCHEMA_VERSION = "hairbook.person_square_render_index.v1"

LAYOUTS = {
    "copy_left",
    "copy_right",
    "bottom_editorial",
    "top_editorial",
}

THEMES: dict[str, dict[str, str]] = {
    "neutral_ink": {
        "ink": "#171614",
        "accent": "#d8bd91",
        "body": "#fffaf1",
        "secondary": "#e3dbcf",
    },
    "warm_clay": {
        "ink": "#231612",
        "accent": "#e3aa82",
        "body": "#fff7ef",
        "secondary": "#ead9cb",
    },
    "soft_sage": {
        "ink": "#14211d",
        "accent": "#b8d1bd",
        "body": "#f8fff9",
        "secondary": "#d6e3d9",
    },
    "deep_blue": {
        "ink": "#121d2a",
        "accent": "#b7cce5",
        "body": "#f7fbff",
        "secondary": "#d6e0ec",
    },
    "soft_rose": {
        "ink": "#24161b",
        "accent": "#e4b6c2",
        "body": "#fff8fa",
        "secondary": "#ead9de",
    },
}

INDUSTRY_LABELS = {
    "hair": "HAIR DESIGN",
    "hair_mens": "MEN'S HAIR",
    "nail": "NAIL DESIGN",
    "eye": "EYE DESIGN",
    "eye_nail": "EYE & NAIL",
    "head_spa": "HEAD SPA",
    "esthetic": "BEAUTY CARE",
}

HTML_RE = re.compile(
    r"<\s*/?\s*[a-zA-Z][^>]*>|"
    r"&(?:[a-zA-Z]{2,10}|#[0-9]{2,6}|#x[0-9a-fA-F]{2,6});"
)


def _draw_multiline(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    lines: list[str],
    font: Any,
    fill: tuple[int, int, int, int],
    line_height: int,
) -> None:
    x, y = xy
    for index, line in enumerate(lines):
        draw.text(
            (x, y + index * line_height),
            str(line),
            font=font,
            fill=fill,
            anchor="lt",
        )


def _rgba(value: str, alpha: int = 255) -> tuple[int, int, int, int]:
    return _hex_rgba(value, alpha)


def _directional_scrim(
    layout: str,
    ink: tuple[int, int, int],
) -> Image.Image:
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    x = xx / max(1, W - 1)
    y = yy / max(1, H - 1)
    if layout == "copy_left":
        primary = np.clip(1.08 - x / 0.68, 0, 1) ** 0.78
        alpha = 0.86 * primary + 0.30 * np.clip(
            (y - 0.62) / 0.38,
            0,
            1,
        )
    elif layout == "copy_right":
        primary = np.clip(1.08 - (1 - x) / 0.68, 0, 1) ** 0.78
        alpha = 0.86 * primary + 0.30 * np.clip(
            (y - 0.62) / 0.38,
            0,
            1,
        )
    elif layout == "top_editorial":
        top = np.clip(1.02 - y / 0.58, 0, 1) ** 0.80
        bottom = np.clip((y - 0.73) / 0.27, 0, 1)
        alpha = 0.85 * top + 0.64 * bottom
    else:
        bottom = np.clip((y - 0.36) / 0.64, 0, 1) ** 0.72
        top = np.clip(1.0 - y / 0.22, 0, 1)
        alpha = 0.92 * bottom + 0.28 * top

    # A restrained vignette improves edge legibility without flattening the
    # center of the photograph.
    edge = np.maximum(
        np.clip((0.055 - x) / 0.055, 0, 1),
        np.clip((x - 0.945) / 0.055, 0, 1),
    )
    alpha = np.clip(alpha + edge * 0.13, 0, 0.94)
    array = np.zeros((H, W, 4), dtype=np.uint8)
    array[..., 0] = ink[0]
    array[..., 1] = ink[1]
    array[..., 2] = ink[2]
    array[..., 3] = np.round(alpha * 255).astype(np.uint8)
    return Image.fromarray(array)


def _subtle_grain(base: Image.Image, asset_id: str) -> None:
    seed = int.from_bytes(asset_id.encode("utf-8")[:8], "little")
    random = np.random.default_rng(seed)
    noise = random.normal(128, 13, size=(H // 2, W // 2)).clip(
        0,
        255,
    ).astype(np.uint8)
    layer = Image.fromarray(noise).resize(
        (W, H),
        Image.Resampling.BILINEAR,
    )
    alpha = Image.new("L", (W, H), 11)
    grain = Image.merge("RGBA", (layer, layer, layer, alpha))
    base.alpha_composite(grain)


def _rounded_shadow(
    base: Image.Image,
    box: tuple[int, int, int, int],
    radius: int,
    *,
    alpha: int = 72,
    offset: tuple[int, int] = (7, 8),
) -> None:
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer, "RGBA")
    x0, y0, x1, y1 = box
    dx, dy = offset
    draw.rounded_rectangle(
        (x0 + dx, y0 + dy, x1 + dx, y1 + dy),
        radius=radius,
        fill=(0, 0, 0, alpha),
    )
    layer = layer.filter(ImageFilter.GaussianBlur(7))
    base.alpha_composite(layer)


def _fit_name(
    measure: ImageDraw.ImageDraw,
    value: str,
    max_width: int,
    *,
    start: int = 42,
    minimum: int = 28,
) -> tuple[list[str], Any, int]:
    return _fit_wrapped_text(
        measure,
        value,
        "gothic_bold",
        start,
        minimum,
        max_width,
        max_lines=2,
    )


def _fit_headline(
    measure: ImageDraw.ImageDraw,
    lines: list[str],
    max_width: int,
    *,
    start: int = 72,
    minimum: int = 50,
) -> tuple[Any, int]:
    return _fit_font(
        measure,
        lines,
        "mincho",
        start,
        minimum,
        max_width,
    )


def _wrap_side_headline(
    draw: ImageDraw.ImageDraw,
    lines: list[str],
    max_width: int,
) -> list[str]:
    probe = _font("mincho", 44)
    output: list[str] = []
    for raw in lines:
        line = str(raw)
        if _text_width(draw, line, probe) <= max_width:
            output.append(line)
            continue
        candidates: list[int] = []
        for index, char in enumerate(line[:-1], 1):
            if char in "、。・／/ ":
                candidates.append(index)
        candidates.extend(range(2, len(line) - 1))
        midpoint = len(line) / 2
        valid = [
            index
            for index in candidates
            if _text_width(draw, line[:index], probe) <= max_width
            and _text_width(draw, line[index:], probe) <= max_width
        ]
        if not valid:
            output.append(line)
            continue
        split_at = min(valid, key=lambda index: abs(index - midpoint))
        output.extend(
            [
                line[:split_at].strip(),
                line[split_at:].strip(),
            ]
        )
    return [line for line in output if line]


def _draw_micro_label(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    label: str,
    accent: tuple[int, int, int, int],
    *,
    line_width: int = 64,
) -> None:
    x, y = xy
    draw.rounded_rectangle(
        (x, y + 11, x + line_width, y + 15),
        radius=2,
        fill=accent,
    )
    draw.text(
        (x + line_width + 16, y),
        label,
        font=_font("gothic_bold", 22),
        fill=accent,
        anchor="lt",
    )


def _draw_access(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    lines: list[str],
    theme: dict[str, str],
    max_width: int,
    *,
    start: int = 32,
) -> tuple[int, int]:
    x, y = xy
    accent = _rgba(theme["accent"])
    secondary = _rgba(theme["secondary"])
    draw.text(
        (x, y),
        "ACCESS",
        font=_font("gothic_bold", 19),
        fill=accent,
        anchor="lt",
    )
    font, size = _fit_font(
        draw,
        lines,
        "gothic_bold",
        start,
        24,
        max_width,
    )
    line_height = max(34, size + 8)
    _draw_multiline(
        draw,
        (x, y + 29),
        lines,
        font,
        secondary,
        line_height,
    )
    return size, line_height


def _header_spec(
    draw: ImageDraw.ImageDraw,
    copy: dict[str, Any],
    *,
    max_width: int,
    compact: bool,
) -> dict[str, Any]:
    area = str(copy["area"])
    name = str(copy["salon_name"])
    area_font, area_size = _fit_font(
        draw,
        [area],
        "gothic_bold",
        31 if not compact else 28,
        24,
        max_width,
    )
    name_lines, name_font, name_size = _fit_name(
        draw,
        name,
        max_width,
        start=46 if compact else 50,
        minimum=32,
    )
    line_height = name_size + 8
    block_height = 56 + len(name_lines) * line_height
    content_width = min(
        max_width,
        max(
            _text_width(draw, area, area_font),
            max(
                _text_width(draw, line, name_font)
                for line in name_lines
            ),
        )
        + 18,
    )
    return {
        "area": area,
        "area_font": area_font,
        "area_font_size": area_size,
        "name": name,
        "name_lines": name_lines,
        "name_font": name_font,
        "name_font_size": name_size,
        "name_line_count": len(name_lines),
        "line_height": line_height,
        "block_height": block_height,
        "content_width": content_width,
        "max_width": max_width,
    }


def _draw_header(
    base: Image.Image,
    draw: ImageDraw.ImageDraw,
    copy: dict[str, Any],
    theme: dict[str, str],
    *,
    xy: tuple[int, int],
    max_width: int,
    compact: bool = False,
    plate: bool = False,
) -> int:
    x, y = xy
    spec = _header_spec(
        draw,
        copy,
        max_width=max_width,
        compact=compact,
    )
    area = spec["area"]
    area_font = spec["area_font"]
    name_lines = spec["name_lines"]
    name_font = spec["name_font"]
    line_height = spec["line_height"]
    block_height = spec["block_height"]
    content_width = spec["content_width"]
    if plate:
        box = (
            x - 18,
            y - 14,
            x + content_width + 22,
            y + block_height + 12,
        )
        _rounded_shadow(base, box, 16, alpha=56, offset=(6, 7))
        draw = ImageDraw.Draw(base, "RGBA")
        draw.rounded_rectangle(
            box,
            radius=16,
            fill=_rgba(theme["ink"], 218),
            outline=_rgba(theme["accent"], 146),
            width=2,
        )
    accent = _rgba(theme["accent"])
    body = _rgba(theme["body"])
    area_box = draw.textbbox((0, 0), area, font=area_font)
    area_width = area_box[2] - area_box[0]
    draw.rounded_rectangle(
        (x - 4, y - 4, x + area_width + 18, y + 34),
        radius=7,
        fill=accent,
    )
    draw.text(
        (x + 7, y + 1),
        area,
        font=area_font,
        fill=_rgba(theme["ink"]),
        anchor="lt",
    )
    _draw_multiline(
        draw,
        (x, y + 48),
        name_lines,
        name_font,
        body,
        line_height,
    )
    return block_height


def _draw_side_layout(
    base: Image.Image,
    copy: dict[str, Any],
    theme: dict[str, str],
    *,
    right: bool,
) -> None:
    draw = ImageDraw.Draw(base, "RGBA")
    accent = _rgba(theme["accent"])
    body = _rgba(theme["body"])
    x = 510 if right else 58
    max_width = 512
    edge_box = (1041, 48, 1044, 1032) if right else (36, 48, 39, 1032)
    draw.rounded_rectangle(
        edge_box,
        radius=2,
        fill=_rgba(theme["accent"], 148),
    )
    header_height = _draw_header(
        base,
        draw,
        copy,
        theme,
        xy=(x, 58),
        max_width=max_width,
        compact=True,
        plate=True,
    )
    industry = INDUSTRY_LABELS[str(copy["industry"])]
    headline_top = max(310, 86 + header_height + 72)
    _draw_micro_label(
        draw,
        (x, headline_top),
        industry,
        accent,
        line_width=58,
    )
    headline_lines = _wrap_side_headline(
        draw,
        [str(line) for line in copy["headline"]],
        max_width,
    )
    headline_font, headline_size = _fit_headline(
        draw,
        headline_lines,
        max_width,
        start=64,
        minimum=40,
    )
    headline_height = headline_size + 13
    _draw_multiline(
        draw,
        (x, headline_top + 46),
        headline_lines,
        headline_font,
        body,
        headline_height,
    )
    separator_y = headline_top + 54 + len(headline_lines) * headline_height + 24
    draw.rounded_rectangle(
        (x, separator_y, x + 74, separator_y + 4),
        radius=2,
        fill=accent,
    )
    _draw_access(
        draw,
        (x, separator_y + 28),
        [str(line) for line in copy["access"]],
        theme,
        max_width,
        start=31,
    )


def _draw_bottom_layout(
    base: Image.Image,
    copy: dict[str, Any],
    theme: dict[str, str],
) -> None:
    draw = ImageDraw.Draw(base, "RGBA")
    accent = _rgba(theme["accent"])
    body = _rgba(theme["body"])
    _draw_header(
        base,
        draw,
        copy,
        theme,
        xy=(62, 58),
        max_width=660,
        compact=True,
        plate=True,
    )
    _draw_micro_label(
        draw,
        (60, 650),
        INDUSTRY_LABELS[str(copy["industry"])],
        accent,
        line_width=72,
    )
    headline_lines = [str(line) for line in copy["headline"]]
    headline_font, headline_size = _fit_headline(
        draw,
        headline_lines,
        952,
        start=76,
        minimum=54,
    )
    line_height = headline_size + 12
    headline_y = 698
    _draw_multiline(
        draw,
        (58, headline_y),
        headline_lines,
        headline_font,
        body,
        line_height,
    )
    info_y = headline_y + len(headline_lines) * line_height + 24
    info_y = min(info_y, 918)
    draw.rounded_rectangle(
        (58, info_y, 132, info_y + 4),
        radius=2,
        fill=accent,
    )
    _draw_access(
        draw,
        (58, info_y + 19),
        [str(line) for line in copy["access"]],
        theme,
        610,
        start=31,
    )


def _draw_top_layout(
    base: Image.Image,
    copy: dict[str, Any],
    theme: dict[str, str],
) -> None:
    draw = ImageDraw.Draw(base, "RGBA")
    accent = _rgba(theme["accent"])
    body = _rgba(theme["body"])
    header_height = _draw_header(
        base,
        draw,
        copy,
        theme,
        xy=(58, 52),
        max_width=870,
        compact=True,
        plate=True,
    )
    micro_y = min(242, 78 + header_height + 34)
    _draw_micro_label(
        draw,
        (58, micro_y),
        INDUSTRY_LABELS[str(copy["industry"])],
        accent,
        line_width=72,
    )
    headline_lines = [str(line) for line in copy["headline"]]
    headline_font, headline_size = _fit_headline(
        draw,
        headline_lines,
        940,
        start=70,
        minimum=50,
    )
    line_height = headline_size + 12
    _draw_multiline(
        draw,
        (56, micro_y + 44),
        headline_lines,
        headline_font,
        body,
        line_height,
    )
    draw.rounded_rectangle(
        (58, 901, 132, 905),
        radius=2,
        fill=accent,
    )
    _draw_access(
        draw,
        (58, 919),
        [str(line) for line in copy["access"]],
        theme,
        620,
        start=31,
    )


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read manifest {path}: {exc}") from exc
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ManifestError(
            f"schema_version must be {SCHEMA_VERSION!r}"
        )
    if manifest.get("design_version") != DESIGN_VERSION:
        raise ManifestError(
            f"design_version must be {DESIGN_VERSION!r}"
        )
    assets = manifest.get("assets")
    if not isinstance(assets, list) or not assets:
        raise ManifestError("manifest.assets must be a non-empty list")
    seen: set[str] = set()
    for asset in assets:
        asset_id = str(asset.get("asset_id") or "").strip()
        if not asset_id:
            raise ManifestError("every asset requires asset_id")
        if asset_id in seen:
            raise ManifestError(f"duplicate asset_id: {asset_id}")
        seen.add(asset_id)
        layout = str(asset.get("layout") or "")
        if layout not in LAYOUTS:
            raise ManifestError(
                f"{asset_id}: unsupported layout {layout!r}"
            )
        theme = str(asset.get("theme") or "")
        if theme not in THEMES:
            raise ManifestError(
                f"{asset_id}: unsupported theme {theme!r}"
            )
        copy = asset.get("copy") or {}
        for field in (
            "industry",
            "area",
            "salon_name",
            "access",
            "headline",
        ):
            if not copy.get(field):
                raise ManifestError(
                    f"{asset_id}: copy.{field} is required"
                )
        if str(copy.get("cta") or "").strip():
            raise ManifestError(
                f"{asset_id}: CTA must be omitted for catalog creatives"
            )
        if copy["industry"] not in INDUSTRY_LABELS:
            raise ManifestError(
                f"{asset_id}: unsupported industry "
                f"{copy['industry']!r}"
            )
        for field in ("access", "headline"):
            lines = copy[field]
            if not isinstance(lines, list) or not 1 <= len(lines) <= 2:
                raise ManifestError(
                    f"{asset_id}: copy.{field} must contain 1 or 2 lines"
                )
        if HTML_RE.search(json.dumps(copy, ensure_ascii=False)):
            raise ManifestError(
                f"{asset_id}: copy contains HTML or an HTML entity"
            )
        vision = (asset.get("source") or {}).get("vision") or {}
        if vision.get("status") != "approved":
            raise ManifestError(
                f"{asset_id}: source vision decision is not approved"
            )
        is_nail_service = (
            copy["industry"] in {"nail", "eye_nail"}
            and vision.get("nail_service_approved") is True
        )
        if (
            not is_nail_service
            and float(vision.get("person_score") or 0) < 0.30
        ):
            raise ManifestError(
                f"{asset_id}: person score is below the hard floor"
            )
        if float(vision.get("treatment_risk") or 0) > 0.72:
            raise ManifestError(
                f"{asset_id}: treatment risk exceeds the hard ceiling"
            )
        if vision.get("treatment_flags"):
            raise ManifestError(
                f"{asset_id}: treatment-scene flags are present"
            )
        if (
            not is_nail_service
            and vision.get("text_flags")
        ):
            raise ManifestError(
                f"{asset_id}: source contains baked-in text"
            )
        if (
            is_nail_service
            and vision.get("text_after_crop_clear") is not True
        ):
            raise ManifestError(
                f"{asset_id}: nail crop still contains source text"
            )
        if any(
            re.search(r"徒歩\s*0\s*(?:分|秒)", str(line))
            for line in copy["access"]
        ):
            raise ManifestError(
                f"{asset_id}: access must not contain zero-minute walking time"
            )
    return manifest


def _render_asset(
    manifest_path: Path,
    manifest_sha256: str,
    asset: dict[str, Any],
    output_dir: Path,
) -> dict[str, Any]:
    asset_id = str(asset["asset_id"])
    source_path = _resolve_source(manifest_path, asset)
    source_sha256 = sha256_file(source_path)
    image_cfg = asset.get("image") or {}
    focal_x = float(image_cfg.get("focal_x", 0.5))
    focal_y = float(image_cfg.get("focal_y", 0.5))
    if not (0 <= focal_x <= 1 and 0 <= focal_y <= 1):
        raise ManifestError(
            f"{asset_id}: focal point must be between 0 and 1"
        )
    brightness = float(image_cfg.get("brightness", 1.0))
    saturation = float(image_cfg.get("saturation", 1.0))
    contrast = float(image_cfg.get("contrast", 1.0))
    if not (0.78 <= brightness <= 1.22):
        raise ManifestError(f"{asset_id}: brightness outside safe range")
    if not (0.68 <= saturation <= 1.22):
        raise ManifestError(f"{asset_id}: saturation outside safe range")
    if not (0.82 <= contrast <= 1.18):
        raise ManifestError(f"{asset_id}: contrast outside safe range")

    with Image.open(source_path) as opened:
        source = ImageOps.exif_transpose(opened).convert("RGB")
        source_size = source.size
        crop_box = image_cfg.get("crop_box")
        if crop_box:
            if (
                not isinstance(crop_box, list)
                or len(crop_box) != 4
                or any(
                    not isinstance(value, (int, float))
                    for value in crop_box
                )
            ):
                raise ManifestError(
                    f"{asset_id}: crop_box must contain four numbers"
                )
            left, top, right, bottom = [
                float(value) for value in crop_box
            ]
            if not (
                0 <= left < right <= 1
                and 0 <= top < bottom <= 1
            ):
                raise ManifestError(
                    f"{asset_id}: crop_box is outside the source image"
                )
            source = source.crop(
                (
                    round(left * source.width),
                    round(top * source.height),
                    round(right * source.width),
                    round(bottom * source.height),
                )
            )
        base = ImageOps.fit(
            source,
            (W, H),
            method=Image.Resampling.LANCZOS,
            centering=(focal_x, focal_y),
        )
    base = ImageEnhance.Brightness(base).enhance(brightness)
    base = ImageEnhance.Color(base).enhance(saturation)
    base = ImageEnhance.Contrast(base).enhance(contrast).convert("RGBA")

    layout = str(asset["layout"])
    theme = dict(THEMES[str(asset["theme"])])
    theme.update(asset.get("theme_overrides") or {})
    ink_rgb = _rgba(theme["ink"])[:3]
    base.alpha_composite(_directional_scrim(layout, ink_rgb))
    _subtle_grain(base, asset_id)

    draw = ImageDraw.Draw(base, "RGBA")
    draw.rounded_rectangle(
        (24, 24, W - 24, H - 24),
        radius=18,
        outline=_rgba(theme["accent"], 112),
        width=2,
    )
    if layout == "copy_left":
        _draw_side_layout(base, asset["copy"], theme, right=False)
    elif layout == "copy_right":
        _draw_side_layout(base, asset["copy"], theme, right=True)
    elif layout == "top_editorial":
        _draw_top_layout(base, asset["copy"], theme)
    else:
        _draw_bottom_layout(base, asset["copy"], theme)

    header_width = (
        512
        if layout in {"copy_left", "copy_right"}
        else 870
        if layout == "top_editorial"
        else 660
    )
    header_metrics = _header_spec(
        ImageDraw.Draw(base, "RGBA"),
        asset["copy"],
        max_width=header_width,
        compact=True,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{asset_id}.jpg"
    preview_path = output_dir / f"{asset_id}--mobile.jpg"
    rgb = base.convert("RGB")
    rgb.save(
        output_path,
        "JPEG",
        quality=92,
        subsampling=0,
        optimize=True,
    )
    rgb.resize(
        (MOBILE_W, MOBILE_H),
        Image.Resampling.LANCZOS,
    ).save(
        preview_path,
        "JPEG",
        quality=90,
        subsampling=0,
        optimize=True,
    )
    copy_sha256 = _canonical_sha(asset["copy"])
    vision = (asset.get("source") or {}).get("vision") or {}
    return {
        "asset_id": asset_id,
        "salon_id": str(asset.get("salon_id") or ""),
        "product_ids": [
            str(value) for value in asset.get("product_ids") or []
        ],
        "design_version": DESIGN_VERSION,
        "representation": (
            "salon_page_nail_service_square_edit"
            if asset["copy"]["industry"] in {"nail", "eye_nail"}
            else "salon_page_person_image_square_edit"
        ),
        "render_mode": "complete_banner",
        "source_path": str(source_path),
        "source_page_url": str(
            (asset.get("source") or {}).get("page_url") or ""
        ),
        "source_image_url": str(
            (asset.get("source") or {}).get("image_url") or ""
        ),
        "source_sha256": source_sha256,
        "source_width": source_size[0],
        "source_height": source_size[1],
        "source_person_score": float(
            vision.get("person_score") or 0
        ),
        "source_treatment_risk": float(
            vision.get("treatment_risk") or 0
        ),
        "copy_sha256": copy_sha256,
        "manifest_sha256": manifest_sha256,
        "output_path": str(output_path.resolve()),
        "output_sha256": sha256_file(output_path),
        "preview_path": str(preview_path.resolve()),
        "preview_sha256": sha256_file(preview_path),
        "public_url": str(asset.get("public_url") or ""),
        "width": W,
        "height": H,
        "preview_width": MOBILE_W,
        "preview_height": MOBILE_H,
        "layout": layout,
        "layout_reason": str(asset.get("layout_reason") or ""),
        "theme": str(asset["theme"]),
        "industry": str(asset["copy"]["industry"]),
        "header_metrics": {
            "area_font_size": int(
                header_metrics["area_font_size"]
            ),
            "salon_name_font_size": int(
                header_metrics["name_font_size"]
            ),
            "salon_name_lines": int(
                header_metrics["name_line_count"]
            ),
            "max_width": int(header_metrics["max_width"]),
        },
    }


def render_manifest(
    manifest_path: Path,
    output_dir: Path,
    only: set[str] | None = None,
) -> dict[str, Any]:
    manifest_path = manifest_path.resolve()
    manifest = load_manifest(manifest_path)
    manifest_sha256 = sha256_file(manifest_path)
    records = []
    for asset in manifest["assets"]:
        asset_id = str(asset["asset_id"])
        if only and asset_id not in only:
            continue
        records.append(
            _render_asset(
                manifest_path,
                manifest_sha256,
                asset,
                output_dir.resolve(),
            )
        )
    if not records:
        raise ManifestError("no assets selected for rendering")
    payload = {
        "schema_version": RENDER_SCHEMA_VERSION,
        "environment": str(
            manifest.get("environment") or "production"
        ),
        "design_version": DESIGN_VERSION,
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha256,
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "output_format": manifest.get("output_format") or {},
        "assets": records,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    index_path = output_dir / "render_index.json"
    index_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Render 1:1 person-first editorial catalog banners."
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        help="Render only this asset_id. Repeat for multiple assets.",
    )
    args = parser.parse_args()
    payload = render_manifest(
        args.manifest,
        args.output_dir,
        set(args.only) or None,
    )
    print(
        json.dumps(
            {
                "status": "rendered",
                "design_version": payload["design_version"],
                "asset_count": len(payload["assets"]),
                "output_dir": str(args.output_dir.resolve()),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
