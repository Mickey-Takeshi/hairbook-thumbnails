from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

THUMBGEN = Path(__file__).resolve().parents[1]
if str(THUMBGEN) not in sys.path:
    sys.path.insert(0, str(THUMBGEN))

import build_person_square_catalog as builder
import creative_rollout
import person_square
from build_person_v3_catalog import PageInfo


class PersonSquareCopyTest(unittest.TestCase):
    def test_html_is_removed_and_nail_copy_matches_industry(self):
        row = {
            "id": "61_01TEST",
            "title": "梅田駅より徒歩3分 NAIL TEST",
            "description": "<p>大人のための<br>ジェルネイル</p>",
            "address.region": "大阪府",
            "address.city": "大阪市",
            "address.street_address": "北区1-2-3",
        }
        info = PageInfo(
            landing_url="https://hairbook.jp/salons/61/",
            component="salons/show",
            version="",
            salon_id="61",
            stylist_id="",
            salon_name="NAIL TEST【ネイルテスト】",
            location="梅田駅より徒歩3分",
            region="大阪府",
            city="大阪市",
            nearest_stations=["梅田"],
            source_text="<b>ネイル</b>",
            staff_name="",
            candidates=[],
        )
        copy, audit = builder._copy_payload(row, info)
        self.assertEqual(copy["industry"], "nail")
        self.assertIn("ネイル", " ".join(copy["headline"]))
        self.assertEqual(copy["access"], ["梅田駅 徒歩3分"])
        self.assertNotRegex(
            json.dumps(copy, ensure_ascii=False),
            builder.HTML_TAG_RE,
        )
        self.assertNotIn("<", audit["feed_description_clean"])

    def test_treatment_cues_are_hard_flags(self):
        labels = {
            "hospital": 0.44,
            "people": 0.88,
            "adult": 0.88,
        }
        self.assertIn(
            "hospital_or_facial_treatment",
            builder._treatment_flags(labels),
        )
        labels = {
            "bathroom": 0.22,
            "washbasin": 0.21,
            "people": 0.80,
        }
        self.assertIn(
            "shampoo_or_washbasin_scene",
            builder._treatment_flags(labels),
        )

    def test_explicit_eye_and_head_spa_businesses_get_matching_copy(self):
        base = {
            "id": "61_01TEST",
            "address.region": "愛知県",
            "address.city": "半田市",
            "address.street_address": "1-2-3",
        }
        eye_row = {
            **base,
            "title": "半田駅 徒歩5分 mite eyesalon 半田店",
            "description": "自然な目元をご提案",
        }
        eye_info = PageInfo(
            landing_url="https://hairbook.jp/salons/61/",
            component="salons/show",
            version="",
            salon_id="61",
            stylist_id="",
            salon_name="mite eyesalon 半田店",
            location="半田駅 徒歩5分",
            region="愛知県",
            city="半田市",
            nearest_stations=["半田"],
            source_text="アイデザイン",
            staff_name="",
            candidates=[],
        )
        eye_copy, _ = builder._copy_payload(eye_row, eye_info)
        self.assertEqual(eye_copy["industry"], "eye")
        self.assertIn("目元", " ".join(eye_copy["headline"]))

        spa_row = {
            **base,
            "title": "半田駅 徒歩5分 ドライヘッドスパ専門店 if",
            "description": "頭からリフレッシュ",
        }
        spa_info = PageInfo(
            landing_url="https://hairbook.jp/salons/61/",
            component="salons/show",
            version="",
            salon_id="61",
            stylist_id="",
            salon_name="ドライヘッドスパ専門店 if",
            location="半田駅 徒歩5分",
            region="愛知県",
            city="半田市",
            nearest_stations=["半田"],
            source_text="ドライヘッドスパ",
            staff_name="",
            candidates=[],
        )
        spa_copy, _ = builder._copy_payload(spa_row, spa_info)
        self.assertEqual(spa_copy["industry"], "head_spa")
        self.assertIn("ヘッドスパ", " ".join(spa_copy["headline"]))

    def test_compact_eyelashsalon_name_is_classified_as_eye(self):
        row = {
            "id": "61_01TEST",
            "title": "CLARA 明野店 eyelashsalon",
            "description": "自然なデザインをご提案",
            "address.region": "大分県",
            "address.city": "大分市",
            "address.street_address": "明野1-2-3",
        }
        info = PageInfo(
            landing_url="https://hairbook.jp/salons/61/",
            component="salons/show",
            version="",
            salon_id="61",
            stylist_id="",
            salon_name="CLARA 明野店 eyelashsalon",
            location="高城駅より車で8分",
            region="大分県",
            city="大分市",
            nearest_stations=["高城"],
            source_text="eyelashsalon",
            staff_name="",
            candidates=[],
        )
        copy, _ = builder._copy_payload(row, info)
        self.assertEqual(copy["industry"], "eye")
        self.assertIn("目元", " ".join(copy["headline"]))

    def test_manual_source_rejections_are_validated(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "rejections.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": (
                            "hairbook.person_square_source_rejections.v1"
                        ),
                        "rejections": [
                            {
                                "source_type": "hairbook_feed_post_photo",
                                "record_id": "01TEST",
                                "reason": "施術中カット",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                builder._load_source_rejections(path),
                {
                    (
                        "hairbook_feed_post_photo",
                        "01TEST",
                    ): "施術中カット"
                },
            )

    def test_access_parser_keeps_zero_minutes_car_and_approximate_walk(self):
        cases = [
            (
                "北加賀屋駅3番出口0分",
                [("北加賀屋", "徒歩0分")],
            ),
            (
                "名鉄「多治見駅」より車で約5分",
                [("多治見", "車5分")],
            ),
            (
                "JR土岐市駅より徒歩約18分",
                [("土岐市", "徒歩18分")],
            ),
            (
                "「明治神宮前駅」7番出口徒歩30秒",
                [("明治神宮前", "徒歩30秒")],
            ),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(builder._station_hits(value), expected)

    def test_bus_stop_access_is_preserved_without_station_guessing(self):
        self.assertEqual(
            builder._location_access_lines(
                "バス停「泡瀬３丁目」から徒歩約4分"
            ),
            ["バス停「泡瀬3丁目」から徒歩4分"],
        )


class PersonSquareRendererTest(unittest.TestCase):
    def test_all_layouts_render_and_pass_square_qa(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source.jpg"
            Image.new("RGB", (900, 1200), "#8c7669").save(
                source,
                "JPEG",
            )
            source_hash = person_square.sha256_file(source)
            assets = []
            for index, layout in enumerate(sorted(person_square.LAYOUTS)):
                assets.append(
                    {
                        "asset_id": f"square-{index}",
                        "salon_id": "61",
                        "product_ids": [f"61_01TEST{index}"],
                        "layout": layout,
                        "layout_reason": "unit test",
                        "theme": "warm_clay",
                        "source": {
                            "path": "source.jpg",
                            "page_url": "https://hairbook.jp/salons/61/",
                            "image_url": "https://example.com/source.jpg",
                            "sha256": source_hash,
                            "source_type": "hairbook_salon_style_photo",
                            "vision": {
                                "status": "approved",
                                "person_score": 0.9,
                                "face_score": 0.8,
                                "human_score": 0.8,
                                "treatment_risk": 0.02,
                                "treatment_flags": [],
                            },
                        },
                        "image": {
                            "focal_x": 0.5,
                            "focal_y": 0.4,
                            "brightness": 1.0,
                            "saturation": 1.0,
                            "contrast": 1.0,
                        },
                        "copy": {
                            "industry": "hair",
                            "area": "大阪・梅田",
                            "salon_name": "テストサロン",
                            "access": ["梅田駅 徒歩3分"],
                            "headline": [
                                "あなたらしさを引き出す",
                                "似合わせヘア",
                            ],
                            "cta": "サロンを見る",
                        },
                    }
                )
            manifest = {
                "schema_version": person_square.SCHEMA_VERSION,
                "design_version": person_square.DESIGN_VERSION,
                "environment": "review_draft",
                "account_scope": "HB_02",
                "output_format": {
                    "width": 1080,
                    "height": 1080,
                    "review_width": 360,
                    "review_height": 360,
                },
                "assets": assets,
            }
            manifest_path = root / "manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False),
                encoding="utf-8",
            )
            output = root / "render"
            index = person_square.render_manifest(
                manifest_path,
                output,
            )
            self.assertEqual(len(index["assets"]), 4)
            for record in index["assets"]:
                with Image.open(record["output_path"]) as image:
                    self.assertEqual(image.size, (1080, 1080))
                with Image.open(record["preview_path"]) as preview:
                    self.assertEqual(preview.size, (360, 360))
            qa = creative_rollout.run_qa(
                manifest_path,
                output / "render_index.json",
                output / "qa.json",
                min_source_short_side=720,
            )
            self.assertEqual(qa["schema_version"], creative_rollout.SQUARE_QA_SCHEMA)
            self.assertEqual(qa["overall_status"], "pass")


if __name__ == "__main__":
    unittest.main()
