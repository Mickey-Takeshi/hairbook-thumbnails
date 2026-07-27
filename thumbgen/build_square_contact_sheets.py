"""Build deterministic visual-QA contact sheets for square catalog assets."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageOps

from person_v3 import _font


INDUSTRY_ORDER = {
    "esthetic": 0,
    "head_spa": 1,
    "eye_nail": 2,
    "eye": 3,
    "nail": 4,
    "hair_mens": 5,
    "hair": 6,
}


def _shorten(value: str, limit: int = 26) -> str:
    return value if len(value) <= limit else value[:limit - 1] + "…"


def build_sheets(
    manifest_path: Path,
    render_index_path: Path,
    output_dir: Path,
    *,
    prefix: str,
) -> list[Path]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    render_index = json.loads(
        render_index_path.read_text(encoding="utf-8")
    )
    rendered = {
        item["asset_id"]: item
        for item in render_index.get("assets") or []
    }
    assets: list[dict[str, Any]] = sorted(
        manifest.get("assets") or [],
        key=lambda item: (
            INDUSTRY_ORDER.get(item["copy"]["industry"], 99),
            -float(item["source"]["vision"]["treatment_risk"]),
            item["copy"]["salon_name"],
            item["asset_id"],
        ),
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    columns = 8
    rows = 10
    per_sheet = columns * rows
    card_width = 260
    card_height = 310
    image_size = 252
    label_font = _font("gothic_regular", 15)
    salon_font = _font("gothic_bold", 17)
    outputs: list[Path] = []
    for offset in range(0, len(assets), per_sheet):
        page_assets = assets[offset:offset + per_sheet]
        sheet = Image.new(
            "RGB",
            (columns * card_width, rows * card_height),
            "#f5f1e8",
        )
        draw = ImageDraw.Draw(sheet)
        for local_index, asset in enumerate(page_assets):
            record = rendered.get(asset["asset_id"])
            if not record:
                raise ValueError(
                    f"render record missing: {asset['asset_id']}"
                )
            preview_path = Path(record["preview_path"])
            with Image.open(preview_path) as opened:
                preview = ImageOps.fit(
                    opened.convert("RGB"),
                    (image_size, image_size),
                    method=Image.Resampling.LANCZOS,
                )
            column = local_index % columns
            row = local_index // columns
            x = column * card_width + 4
            y = row * card_height + 4
            sheet.paste(preview, (x, y))
            vision = asset["source"]["vision"]
            rank = offset + local_index + 1
            draw.text(
                (x, y + image_size + 5),
                (
                    f"{rank:03d} {asset['copy']['industry']} "
                    f"P{vision['person_score']:.2f} "
                    f"T{vision['treatment_risk']:.2f}"
                ),
                font=label_font,
                fill="#544b42",
            )
            draw.text(
                (x, y + image_size + 27),
                _shorten(asset["copy"]["salon_name"]),
                font=salon_font,
                fill="#221c15",
            )
        page_number = len(outputs) + 1
        output = output_dir / f"{prefix}-{page_number:02d}.jpg"
        sheet.save(output, "JPEG", quality=88, optimize=True)
        outputs.append(output)
    return outputs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--render-index", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--prefix", default="visual-qa-final")
    args = parser.parse_args()
    outputs = build_sheets(
        args.manifest,
        args.render_index,
        args.output_dir,
        prefix=args.prefix,
    )
    print(
        json.dumps(
            {
                "status": "created",
                "sheets": len(outputs),
                "outputs": [str(path.resolve()) for path in outputs],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
