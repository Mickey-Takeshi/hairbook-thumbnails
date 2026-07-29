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
import apply_person_square_edits as edit_applier
import creative_rollout
import person_square
from build_person_v3_catalog import PageInfo, SourceCandidate


class PersonSquareSourcePolicyTest(unittest.TestCase):
    def test_staff_landing_keeps_only_matching_gallery_styles(self):
        style = SourceCandidate(
            source_type="hairbook_stylist_style_photo",
            record_id="166989",
            page_url="https://hairbook.jp/staffs/16979/styles/166989/",
            image_url="https://hairbook.jp/photo/Style/166989/",
        )
        profile = SourceCandidate(
            source_type="hairbook_staff_profile_photo",
            record_id="16979",
            page_url="https://hairbook.jp/staffs/16979/",
            image_url="https://hairbook.jp/thumbnail/StaffProfile/16979/",
        )
        override = SourceCandidate(
            source_type="official_salon_person_photo",
            record_id="official",
            page_url="https://example.com/staff",
            image_url="https://example.com/staff.jpg",
        )
        info = PageInfo(
            landing_url="https://hairbook.jp/staffs/16979/",
            component="staff/profiles/show",
            version="v1",
            salon_id="44645",
            stylist_id="16979",
            salon_name="e's【イーズ】大阪 梅田店",
            location="梅田駅 徒歩5分",
            region="大阪府",
            city="大阪市",
            nearest_stations=["梅田"],
            source_text="ヘアサロン",
            staff_name="神崎 夢羽",
            candidates=[style, profile],
        )
        candidates = builder._page_source_candidates(
            landing_url=info.landing_url,
            rows=[
                {
                    "id": "44645_16979_01TEST",
                    "link": info.landing_url,
                    "image_link": "https://hairbook.jp/thumbnail/Post/01TEST/",
                    "title": "e's",
                    "description": "カラー",
                }
            ],
            info=info,
            override_candidates={"44645": [override]},
        )

        self.assertEqual(candidates, [style])

    def test_rollout_gate_rejects_profile_for_staff_landing(self):
        valid, _ = creative_rollout._staff_styles_source_matches(
            {
                "landing_url": "https://hairbook.jp/staffs/16979/",
                "source": {
                    "source_type": "hairbook_staff_profile_photo",
                    "page_url": "https://hairbook.jp/staffs/16979/",
                },
            }
        )
        self.assertFalse(valid)
        valid, _ = creative_rollout._staff_styles_source_matches(
            {
                "landing_url": (
                    "https://hairbook.jp/staffs/16979/?utm_source=meta"
                ),
                "source": {
                    "source_type": "hairbook_stylist_style_photo",
                    "page_url": (
                        "https://hairbook.jp/staffs/16979/styles/166989/"
                    ),
                },
            }
        )
        self.assertTrue(valid)


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
        self.assertIn(
            "face_mask_or_obstruction",
            builder._treatment_flags(
                {"people": 0.9, "mask": 0.02}
            ),
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

    def test_access_parser_rewrites_zero_minutes_and_keeps_other_modes(self):
        cases = [
            (
                "北加賀屋駅3番出口0分",
                [("北加賀屋", "すぐ")],
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

    def test_general_hair_copy_is_gender_neutral_and_has_no_cta(self):
        row = {
            "id": "61_01TEST",
            "title": "梅田駅 徒歩3分 TEST SALON",
            "description": "髪質改善と似合わせカラー",
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
            salon_name="TEST SALON",
            location="梅田駅 徒歩3分",
            region="大阪府",
            city="大阪市",
            nearest_stations=["梅田"],
            source_text="ヘアサロン",
            staff_name="",
            candidates=[],
        )
        copy, _ = builder._copy_payload(row, info)
        headline = " ".join(copy["headline"])
        self.assertEqual(copy["industry"], "hair")
        self.assertEqual(copy["cta"], "")
        for phrase in ("美髪", "肌まできれい", "今の私", "大人女性"):
            self.assertNotIn(phrase, headline)

    def test_product_description_overrides_multi_service_salon_name(self):
        row = {
            "id": "43745_01TEST",
            "title": "hair.nail.eye beauty salon",
            "description": "全店舗 縮毛矯正20%オフ",
            "address.region": "福岡県",
            "address.city": "北九州市",
            "address.street_address": "1-2-3",
        }
        info = PageInfo(
            landing_url="https://hairbook.jp/salons/43745/",
            component="salons/show",
            version="",
            salon_id="43745",
            stylist_id="",
            salon_name="hair.nail.eye beauty&life",
            location="黒崎駅 徒歩5分",
            region="福岡県",
            city="北九州市",
            nearest_stations=["黒崎"],
            source_text="hair nail eye",
            staff_name="",
            candidates=[],
        )
        copy, _ = builder._copy_payload(row, info)
        self.assertEqual(copy["industry"], "hair")

    def test_general_salon_mens_mention_and_unavailable_menu_stay_hair(self):
        base = {
            "id": "44814_18078_01TEST",
            "title": "心斎橋駅 徒歩2分 CYM ITSUKI",
            "address.region": "大阪府",
            "address.city": "大阪市",
            "address.street_address": "中央区1-2-3",
        }
        info = PageInfo(
            landing_url="https://hairbook.jp/staffs/18078/",
            component="staffs/show",
            version="",
            salon_id="44814",
            stylist_id="18078",
            salon_name="CYM【シィム】",
            location="心斎橋駅 徒歩2分",
            region="大阪府",
            city="大阪市",
            nearest_stations=["心斎橋"],
            source_text="ハイトーンとエクステが得意",
            staff_name="ITSUKI",
            candidates=[],
        )
        rows = [
            {
                **base,
                "description": (
                    "ブリーチなしダブルカラーが得意。"
                    "メンズカット、予約不可"
                ),
            },
            {
                **base,
                "description": (
                    "似合わせカットとカラー。"
                    "メンズカットもご相談ください"
                ),
            },
        ]
        for row in rows:
            with self.subTest(row=row["description"]):
                copy, _ = builder._copy_payload(row, info)
                self.assertEqual(copy["industry"], "hair")

    def test_dedicated_mens_business_keeps_mens_copy(self):
        row = {
            "id": "61_01TEST",
            "title": "MEN'S BARBER SHOP NEO",
            "description": "カットとパーマ",
            "address.region": "東京都",
            "address.city": "渋谷区",
            "address.street_address": "1-2-3",
        }
        info = PageInfo(
            landing_url="https://hairbook.jp/salons/61/",
            component="salons/show",
            version="",
            salon_id="61",
            stylist_id="",
            salon_name="MEN'S BARBER SHOP NEO",
            location="渋谷駅 徒歩3分",
            region="東京都",
            city="渋谷区",
            nearest_stations=["渋谷"],
            source_text="メンズ専門",
            staff_name="",
            candidates=[],
        )
        copy, _ = builder._copy_payload(row, info)
        self.assertEqual(copy["industry"], "hair_mens")
        self.assertIn("メンズ", " ".join(copy["headline"]))

    def test_ocr_detects_baked_text(self):
        analysis = {
            "texts": [
                {
                    "text": "限定キャンペーン",
                    "confidence": 0.92,
                    "x": 0.05,
                    "y": 0.04,
                    "width": 0.52,
                    "height": 0.12,
                }
            ]
        }
        result = builder._source_text_analysis(analysis)
        self.assertIn("baked_text_detected", result["text_flags"])

    def test_nail_selection_allows_finished_nail_closeup_only(self):
        def item(metadata: str):
            candidate = builder.SourceCandidate(
                source_type="hairbook_salon_style_photo",
                record_id="159062",
                page_url="https://hairbook.jp/salons/43219/",
                image_url="https://hairbook.jp/example.jpg",
                metadata_text=metadata,
            )
            return {
                "candidate": candidate,
                "prepared": {
                    "candidate": candidate,
                    "path": Path("/tmp/nail.jpg"),
                    "sha256": metadata,
                    "width": 1080,
                    "height": 1440,
                    "final_url": candidate.image_url,
                },
                "vision": {
                    "status": "rejected",
                    "score": 0.1,
                    "person_score": 0.0,
                    "face_score": 0.0,
                    "human_score": 0.0,
                    "treatment_risk": 0.02,
                    "treatment_flags": [],
                    "non_person_risk": 0.30,
                    "text_boxes": [
                        {
                            "x": 0.04,
                            "y": 0.82,
                            "width": 0.50,
                            "height": 0.08,
                        }
                    ],
                },
            }

        selected = builder._selected_pool(
            [
                item("深爪改善 ジェルネイル"),
                item("縮毛矯正 艶カラー"),
            ],
            "nail",
        )
        self.assertEqual(len(selected), 1)
        self.assertTrue(
            selected[0]["vision"]["nail_service_approved"]
        )
        self.assertTrue(
            selected[0]["vision"]["text_after_crop_clear"]
        )
        mixed_selected = builder._selected_pool(
            [item("深爪改善 ジェルネイル")],
            "eye_nail",
        )
        self.assertEqual(len(mixed_selected), 1)
        self.assertTrue(
            mixed_selected[0]["vision"]["nail_service_approved"]
        )

    def test_non_hair_service_rejects_body_part_only_fallback(self):
        candidate = builder.SourceCandidate(
            source_type="hairbook_salon_style_photo",
            record_id="159062",
            page_url="https://hairbook.jp/salons/43219/",
            image_url="https://hairbook.jp/example.jpg",
            metadata_text="小顔ケア",
        )
        selected = builder._selected_pool(
            [
                {
                    "candidate": candidate,
                    "prepared": {
                        "candidate": candidate,
                        "path": Path("/tmp/body-part.jpg"),
                        "sha256": "test",
                        "width": 1080,
                        "height": 1440,
                        "final_url": candidate.image_url,
                    },
                    "vision": {
                        "status": "approved",
                        "score": 4.0,
                        "person_score": 0.67,
                        "face_score": 0.0,
                        "human_score": 0.62,
                        "treatment_risk": 0.01,
                        "treatment_flags": [],
                        "non_person_risk": 0.10,
                        "text_flags": [],
                    },
                }
            ],
            "esthetic",
        )
        self.assertEqual(selected, [])

    def test_tiny_mirror_face_is_only_a_finished_back_style_fallback(self):
        candidate = builder.SourceCandidate(
            source_type="hairbook_salon_style_photo",
            record_id="127354",
            page_url="https://hairbook.jp/salons/280/",
            image_url="https://hairbook.jp/photo/Style/127354/",
            metadata_text="髪質改善ロング",
        )
        selected = builder._selected_pool(
            [
                {
                    "candidate": candidate,
                    "prepared": {
                        "candidate": candidate,
                        "path": Path("/tmp/back-hair.jpg"),
                        "sha256": "test",
                        "width": 375,
                        "height": 500,
                        "final_url": candidate.image_url,
                    },
                    "vision": {
                        "status": "approved",
                        "score": 8.0,
                        "person_score": 0.90,
                        "face_score": 0.80,
                        "human_score": 0.90,
                        "face_box": {
                            "x": 0.40,
                            "y": 0.91,
                            "width": 0.06,
                            "height": 0.05,
                        },
                        "treatment_risk": 0.01,
                        "treatment_flags": [],
                        "non_person_risk": 0.01,
                        "text_flags": [],
                    },
                }
            ],
            "hair",
        )
        self.assertEqual(len(selected), 1)
        self.assertTrue(
            selected[0]["vision"][
                "hair_finished_style_back_view_approved"
            ]
        )

    def test_display_salon_name_drops_search_menu_suffixes(self):
        self.assertEqual(
            builder._display_salon_name(
                "eyelash Cocoa byTJ天気予報 豊田長興寺店/"
                "まつ毛パーマ/マツエク/美眉毛/アイブロウ"
            ),
            "eyelash Cocoa byTJ天気予報 豊田長興寺店",
        )
        self.assertEqual(
            builder._display_salon_name(
                "大人の美髪髪質改善サロン the laughter /"
                "デザイン&カラー特化型サロン The bleach 新下関"
            ),
            "the laughter / The bleach 新下関",
        )
        self.assertEqual(
            builder._display_salon_name(
                "眉毛/アイブロウサロン "
                "LUMICIA.-TOKYO-大阪梅田店【ルミシアトウキョウ】"
            ),
            "LUMICIA.-TOKYO-大阪梅田店",
        )

    def test_top_portrait_face_is_kept_below_header_safe_zone(self):
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / "portrait.jpg"
            Image.new("RGB", (853, 1280), "#cab8aa").save(
                source,
                "JPEG",
            )
            result = builder._layout_analysis(
                source,
                {
                    "face_box": {
                        "x": 0.36,
                        "y": 0.74,
                        "width": 0.23,
                        "height": 0.15,
                    },
                    "person_box": None,
                },
                prefer_wide_text=False,
            )
            box = result["subject_box_after_crop"]
            self.assertIsNotNone(box)
            self.assertGreaterEqual(
                box["y"] + box["height"] / 2,
                0.219,
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
                            "cta": "",
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


class PersonSquareDashboardEditTest(unittest.TestCase):
    def test_structured_edit_switches_an_approved_source_and_copy(self):
        asset = {
            "asset_id": "asset-1",
            "layout": "copy_left",
            "layout_reason": "auto",
            "theme": "neutral_ink",
            "source": {
                "path": "sources/a.jpg",
                "source_type": "hairbook_salon_style_photo",
                "record_id": "a",
                "subject_scope": "salon",
                "selection_scope": "landing_page",
            },
            "image": {"focal_x": 0.5, "focal_y": 0.5},
            "copy": {
                "industry": "hair",
                "area": "大阪・梅田",
                "salon_name": "旧店名",
                "access": ["梅田駅 徒歩3分"],
                "headline": ["自分らしさが見つかる", "似合わせヘア"],
                "cta": "",
            },
            "source_options": [
                {
                    "option_id": "hairbook_salon_style_photo:b",
                    "source": {
                        "path": "sources/b.jpg",
                        "source_type": "hairbook_salon_style_photo",
                        "record_id": "b",
                    },
                    "image": {"focal_x": 0.4, "focal_y": 0.4},
                    "layout": "copy_right",
                    "layout_reason": "option",
                    "theme": "warm_clay",
                }
            ],
        }
        changed = edit_applier._apply_request(
            asset,
            {
                "source_option_id": (
                    "hairbook_salon_style_photo:b"
                ),
                "industry": "hair_mens",
                "salon_name": "新店名",
                "headline": [
                    "清潔感を、デザインする",
                    "扱いやすいメンズヘア",
                ],
                "layout": "top_editorial",
                "issue_types": ["gender_copy"],
                "cta_visible": False,
            },
        )
        self.assertEqual(asset["source"]["record_id"], "b")
        self.assertEqual(asset["source"]["subject_scope"], "salon")
        self.assertEqual(asset["copy"]["industry"], "hair_mens")
        self.assertEqual(asset["copy"]["salon_name"], "新店名")
        self.assertEqual(asset["copy"]["cta"], "")
        self.assertEqual(asset["layout"], "top_editorial")
        self.assertIn("source", changed)


if __name__ == "__main__":
    unittest.main()
