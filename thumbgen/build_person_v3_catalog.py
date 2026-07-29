"""Build a full HB_02 Person V3 draft manifest from live Hairbook pages.

This command is read-only with respect to Google Sheets and Hairbook. It:

1. reads the public catalog feed (or an exported CSV);
2. resolves every active product to its Hairbook salon/staff landing page;
3. selects existing Hairbook Style photographs (never video captures);
4. downloads and hashes the selected source photographs;
5. writes a Person V3 render manifest plus an all-product classification.

It deliberately does not publish assets or change ``thumbnail_override``.

Example:
    python3 thumbgen/build_person_v3_catalog.py \
      --output-dir dashboard/private_snapshots/person-v3-full-20260724
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

import requests
from PIL import Image, ImageOps

from creative_inventory import FEED_ID, FEED_TAB, parse_product_id


FEED_QUERY = "select A,B,C,D,G,H,AB,AF,AG,AX where A is not null"
FEED_URL = f"https://docs.google.com/spreadsheets/d/{FEED_ID}/gviz/tq"
SCHEMA_VERSION = "hairbook.person_v3_manifest.v1"
DESIGN_VERSION = "person_v3_layered_v1"
USER_AGENT = (
    "Mozilla/5.0 (compatible; HairbookPersonV3Builder/1.0; "
    "+https://hairbook.jp/)"
)
DEFAULT_TIMEOUT = 30
# Hairbook ``photo/Style`` is normally 394x500. Some eye/nail assets and
# StaffProfile portraits are 229-281px on the short side even though they are
# the largest public salon-page representation. The finished banner is always
# reviewed at the actual 360x450 delivery size, so 220px is the hard ingestion
# floor and the original dimensions remain visible in the review record.
MIN_SOURCE_SHORT_SIDE = 200
MAX_SOURCE_BYTES = 25 * 1024 * 1024


class CatalogBuildError(RuntimeError):
    """A full-catalog draft could not be built safely."""


class _DataPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.capture = False
        self.parts: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag != "script":
            return
        attributes = dict(attrs)
        self.capture = (
            attributes.get("data-page") == "app"
            and attributes.get("type") == "application/json"
        )

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self.capture:
            self.capture = False

    def handle_data(self, data: str) -> None:
        if self.capture:
            self.parts.append(data)


@dataclass(frozen=True)
class SourceCandidate:
    source_type: str
    record_id: str
    page_url: str
    image_url: str
    metadata_text: str = ""
    local_path: str = ""


@dataclass
class PageInfo:
    landing_url: str
    component: str
    version: str
    salon_id: str
    stylist_id: str
    salon_name: str
    location: str
    region: str
    city: str
    nearest_stations: list[str]
    source_text: str
    staff_name: str
    candidates: list[SourceCandidate]
    error: str = ""


_thread_local = threading.local()


def _session() -> requests.Session:
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept-Language": "ja,en;q=0.8",
            }
        )
        _thread_local.session = session
    return session


def _request(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    attempts: int = 3,
) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = _session().get(
                url,
                headers=headers,
                params=params,
                timeout=timeout,
                allow_redirects=True,
            )
            if response.status_code == 429 or response.status_code >= 500:
                raise requests.HTTPError(
                    f"HTTP {response.status_code}",
                    response=response,
                )
            response.raise_for_status()
            return response
        except (requests.RequestException, TimeoutError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.6 * (2**attempt))
    raise CatalogBuildError(f"request failed: {url}: {last_error}")


def _landing_key(url: str) -> str:
    parts = urlsplit(str(url).strip())
    if parts.scheme != "https" or parts.netloc not in {
        "hairbook.jp",
        "salonpage.hairbook.jp",
    }:
        raise CatalogBuildError(f"unsupported landing URL: {url!r}")
    path = re.sub(r"/+$", "/", parts.path or "/")
    return urlunsplit(("https", parts.netloc, path, "", ""))


def _staff_styles_source_url(url: str) -> str | None:
    """Return the matching styles gallery for a Hairbook staff landing."""
    parts = urlsplit(str(url).strip())
    if parts.scheme != "https" or parts.netloc != "hairbook.jp":
        return None
    path = re.sub(r"/+$", "/", parts.path or "/")
    match = re.fullmatch(
        r"/(?:(?:salons/\d+)/)?staffs/\d+/(?:styles/)?",
        path,
    )
    if not match:
        return None
    if not path.endswith("/styles/"):
        path = f"{path.rstrip('/')}/styles/"
    return urlunsplit(("https", parts.netloc, path, "", ""))


def _extract_data_page(text: str, url: str) -> dict[str, Any]:
    parser = _DataPageParser()
    parser.feed(text)
    raw = html.unescape("".join(parser.parts)).strip()
    if not raw:
        raise CatalogBuildError(f"{url}: Inertia data-page JSON was not found")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CatalogBuildError(f"{url}: invalid data-page JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise CatalogBuildError(f"{url}: data-page root must be an object")
    return payload


def _fetch_landing_payload(url: str) -> dict[str, Any]:
    response = _request(
        url,
        headers={"Accept": "text/html, application/xhtml+xml"},
    )
    return _extract_data_page(response.text, url)


def _salon_style_items(
    landing_url: str,
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    props = payload.get("props") or {}
    existing = props.get("styles")
    if isinstance(existing, list):
        return existing
    if isinstance(existing, dict) and isinstance(existing.get("items"), list):
        return existing["items"]

    component = str(payload.get("component") or "")
    version = str(payload.get("version") or "")
    if not component or not version:
        return []
    response = _request(
        landing_url,
        headers={
            "X-Inertia": "true",
            "X-Inertia-Partial-Component": component,
            "X-Inertia-Partial-Data": "styles",
            "X-Inertia-Version": version,
            "Accept": "text/html, application/xhtml+xml",
        },
    )
    try:
        partial = response.json()
    except requests.JSONDecodeError as exc:
        raise CatalogBuildError(
            f"{landing_url}: invalid deferred styles JSON"
        ) from exc
    styles = (partial.get("props") or {}).get("styles")
    if isinstance(styles, list):
        return styles
    if isinstance(styles, dict) and isinstance(styles.get("items"), list):
        return styles["items"]
    return []


def _string_list(values: Iterable[Any]) -> list[str]:
    return [
        str(value).strip()
        for value in values
        if str(value or "").strip()
    ]


def _candidate_list(
    *,
    salon_id: str,
    stylist_id: str,
    styles: list[dict[str, Any]],
    staff_styles_url: str = "",
) -> list[SourceCandidate]:
    seen: set[str] = set()
    candidates: list[SourceCandidate] = []
    for style in styles:
        style_id = str(style.get("id") or "").strip()
        if not style_id or style_id in seen:
            continue
        seen.add(style_id)
        if stylist_id or staff_styles_url:
            styles_base = (
                staff_styles_url
                or f"https://hairbook.jp/staffs/{stylist_id}/styles/"
            )
            page_url = f"{styles_base.rstrip('/')}/{style_id}/"
            source_type = "hairbook_stylist_style_photo"
        else:
            page_url = (
                f"https://hairbook.jp/salons/{salon_id}/styles/{style_id}/"
            )
            source_type = "hairbook_salon_style_photo"
        candidates.append(
            SourceCandidate(
                source_type=source_type,
                record_id=style_id,
                page_url=page_url,
                image_url=f"https://hairbook.jp/photo/Style/{style_id}/",
                metadata_text=str(
                    style.get("name")
                    or style.get("title")
                    or style.get("description")
                    or ""
                ).strip(),
            )
        )
    return candidates


def _legacy_style_candidates(
    styles: list[dict[str, Any]],
) -> list[SourceCandidate]:
    seen: set[str] = set()
    candidates: list[SourceCandidate] = []
    for style in styles:
        style_id = str(style.get("id") or "").strip()
        image_url = str(style.get("imageUrl") or "").strip()
        page_url = str(style.get("styleUrl") or "").strip()
        if (
            not style_id
            or style_id in seen
            or not image_url.startswith("https://")
        ):
            continue
        seen.add(style_id)
        candidates.append(
            SourceCandidate(
                source_type="hairbook_salonpage_style_photo",
                record_id=style_id,
                page_url=page_url or image_url,
                image_url=image_url,
                metadata_text=str(
                    style.get("name")
                    or style.get("title")
                    or style.get("description")
                    or ""
                ).strip(),
            )
        )
    return candidates


def _staff_profile_candidates(
    *,
    props: dict[str, Any],
    stylist_id: str,
) -> list[SourceCandidate]:
    profiles: list[dict[str, Any]] = []
    exact = props.get("staffProfile")
    if isinstance(exact, dict):
        profiles.append(exact)
    if not stylist_id:
        profiles.extend(
            profile
            for profile in props.get("staffs") or []
            if isinstance(profile, dict)
        )

    seen: set[str] = set()
    candidates: list[SourceCandidate] = []
    for profile in profiles:
        profile_id = str(profile.get("id") or "").strip()
        if not profile_id or profile_id in seen:
            continue
        seen.add(profile_id)
        candidates.append(
            SourceCandidate(
                source_type="hairbook_staff_profile_photo",
                record_id=profile_id,
                page_url=f"https://hairbook.jp/staffs/{profile_id}/",
                image_url=(
                    "https://hairbook.jp/thumbnail/"
                    f"StaffProfile/{profile_id}/"
                ),
                metadata_text=" ".join(
                    str(value).strip()
                    for value in (
                        profile.get("name"),
                        profile.get("position"),
                        profile.get("jobTitle"),
                        profile.get("introduction"),
                        profile.get("profileIntroduction"),
                    )
                    if str(value or "").strip()
                ),
            )
        )
    return candidates


def _page_source_text(props: dict[str, Any], stylist_id: str) -> str:
    values: list[str] = []
    if (props.get("salon") or {}).get("hpbSalonId"):
        salon = props.get("salon") or {}
        values.extend(
            [
                str(salon.get("catchCopy") or ""),
                str(salon.get("description") or ""),
            ]
        )
    elif stylist_id:
        staff = props.get("staffProfile") or {}
        values.extend(
            [
                str(staff.get("introduction") or ""),
                str(staff.get("profileIntroduction") or ""),
                str((props.get("salon") or {}).get("description") or ""),
            ]
        )
    else:
        values.extend(_string_list(props.get("catchCopy") or []))
        for commitment in props.get("commitments") or []:
            values.extend(
                [
                    str(commitment.get("title") or ""),
                    str(commitment.get("subtitle") or ""),
                ]
            )
    return "\n".join(value.strip() for value in values if value.strip())


def _station_names(value: str) -> list[str]:
    names: list[str] = []
    for match in re.finditer(r"[「『【]?\s*([^「」『』【】、,/]{1,14})駅", value):
        name = match.group(1).strip()
        if name and name not in names:
            names.append(name)
    return names[:3]


def _feed_location_prefix(title: str, salon_name: str) -> str:
    title = re.sub(r"\s+", " ", str(title or "")).strip()
    primary = _clean_salon_name(salon_name)
    position = title.find(primary)
    if position > 0:
        return title[:position].strip(" /／")
    return ""


def _alternate_staff_payload(
    *,
    stylist_id: str,
    current_landing_url: str,
) -> tuple[str, dict[str, Any]] | None:
    if not stylist_id or f"/staffs/{stylist_id}" in current_landing_url:
        return None
    staff_url = f"https://hairbook.jp/staffs/{stylist_id}/"
    try:
        return staff_url, _fetch_landing_payload(staff_url)
    except Exception:
        return None


def _fetch_page_info(
    landing_url: str,
    representative: dict[str, str],
) -> PageInfo:
    parsed = parse_product_id(representative["id"])
    salon_id = parsed["salon_id"]
    stylist_id = parsed["stylist_id"]
    try:
        payload = _fetch_landing_payload(landing_url)
        props = payload.get("props") or {}
        component = str(payload.get("component") or "")
        shared = (props.get("sharedViews") or {}).get("salonInfo") or {}
        staff_styles_url = _staff_styles_source_url(landing_url)
        styles_payload = (
            _fetch_landing_payload(staff_styles_url)
            if staff_styles_url and staff_styles_url != landing_url
            else payload
        )
        styles = _salon_style_items(
            staff_styles_url or landing_url,
            styles_payload,
        )
        salon_name = str(
            shared.get("name")
            or (props.get("salon") or {}).get("name")
            or ""
        ).strip()
        location = str(
            shared.get("location")
            or (props.get("salon") or {}).get("access")
            or ""
        ).strip()
        if component == "salonpage/show":
            location = (
                _feed_location_prefix(
                    representative.get("title", ""),
                    salon_name,
                )
                or location
            )
        nearest = _string_list(
            item.get("name") for item in props.get("nearestStations") or []
        )
        if not nearest:
            nearest = _station_names(
                "\n".join(
                    [
                        representative.get("title", ""),
                        location,
                    ]
                )
            )
        staff = props.get("staffProfile") or {}
        candidates = (
            _legacy_style_candidates(styles)
            if component == "salonpage/show"
            else _candidate_list(
                salon_id=salon_id,
                stylist_id=stylist_id,
                styles=styles,
                staff_styles_url=staff_styles_url or "",
            )
        )
        profile_candidates = (
            []
            if staff_styles_url
            else _staff_profile_candidates(
                props=props,
                stylist_id=stylist_id,
            )
        )
        candidate_keys = {
            (candidate.source_type, candidate.record_id)
            for candidate in candidates
        }
        candidates.extend(
            candidate
            for candidate in profile_candidates
            if (candidate.source_type, candidate.record_id)
            not in candidate_keys
        )
        if not candidates and not staff_styles_url:
            alternate = _alternate_staff_payload(
                stylist_id=stylist_id,
                current_landing_url=landing_url,
            )
            if alternate:
                alternate_url, alternate_payload = alternate
                alternate_props = alternate_payload.get("props") or {}
                alternate_styles = _salon_style_items(
                    alternate_url,
                    alternate_payload,
                )
                candidates = _candidate_list(
                    salon_id=salon_id,
                    stylist_id=stylist_id,
                    styles=alternate_styles,
                )
                alternate_profiles = _staff_profile_candidates(
                    props=alternate_props,
                    stylist_id=stylist_id,
                )
                alternate_keys = {
                    (candidate.source_type, candidate.record_id)
                    for candidate in candidates
                }
                candidates.extend(
                    candidate
                    for candidate in alternate_profiles
                    if (candidate.source_type, candidate.record_id)
                    not in alternate_keys
                )
                alternate_staff = alternate_props.get("staffProfile") or {}
                if alternate_staff:
                    staff = alternate_staff
                alternate_source_text = _page_source_text(
                    alternate_props,
                    stylist_id,
                )
            else:
                alternate_source_text = ""
        else:
            alternate_source_text = ""
        return PageInfo(
            landing_url=landing_url,
            component=component,
            version=str(payload.get("version") or ""),
            salon_id=salon_id,
            stylist_id=stylist_id,
            salon_name=salon_name,
            location=location,
            region=representative.get("address.region", ""),
            city=representative.get("address.city", ""),
            nearest_stations=nearest,
            source_text="\n".join(
                value
                for value in (
                    _page_source_text(props, stylist_id),
                    alternate_source_text,
                )
                if value
            ),
            staff_name=str(staff.get("name") or "").strip(),
            candidates=candidates,
        )
    except Exception as exc:  # keep the other 900+ products progressing
        return PageInfo(
            landing_url=landing_url,
            component="",
            version="",
            salon_id=salon_id,
            stylist_id=stylist_id,
            salon_name="",
            location="",
            region=representative.get("address.region", ""),
            city=representative.get("address.city", ""),
            nearest_stations=[],
            source_text="",
            staff_name="",
            candidates=[],
            error=str(exc),
        )


def _read_feed_csv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    except OSError as exc:
        raise CatalogBuildError(f"cannot read feed CSV {path}: {exc}") from exc


def _fetch_feed() -> list[dict[str, str]]:
    response = _request(
        FEED_URL,
        params={
            "tqx": "out:csv",
            "sheet": FEED_TAB,
            "tq": FEED_QUERY,
        },
        headers={"Accept": "text/csv"},
    )
    return [
        dict(row)
        for row in csv.DictReader(response.text.splitlines(keepends=True))
    ]


def _active_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    active = [
        row
        for row in rows
        if str(row.get("availability") or "").strip().lower() == "in stock"
        and str(row.get("id") or "").strip()
    ]
    active.sort(key=lambda row: str(row["id"]))
    return active


def _clean_salon_name(value: str) -> str:
    name = re.sub(r"\s+", " ", str(value or "")).strip()
    name = re.sub(r"\s*[（(]\s*旧[:：].*?[）)]\s*$", "", name)
    if "【" in name:
        primary = name.split("【", 1)[0].strip()
        if primary:
            name = primary
    name = re.split(
        (
            r"\s+(?=(?:ヘッドスパ|髪質改善|縮毛矯正|トリートメント|"
            r"カラー|眉毛|まつ毛|まつげ|ネイル|アイラッシュ|"
            r"韓国ヘア|メンズ専門))"
        ),
        name,
        maxsplit=1,
    )[0].strip()
    return name or "Hairbook掲載サロン"


def _prefecture_short(value: str) -> str:
    return re.sub(r"(都|道|府|県)$", "", str(value or "").strip())


def _area_copy(info: PageInfo) -> str:
    prefix = _prefecture_short(info.region)
    place = info.nearest_stations[0] if info.nearest_stations else info.city
    values = [value for value in (prefix, place) if value]
    return "・".join(values) or "Hairbook掲載エリア"


def _walk_minutes(value: str) -> str:
    match = re.search(r"(?:徒歩|歩いて)\s*([0-9０-９]+)\s*分", value)
    if not match:
        match = re.search(r"([0-9０-９]+)\s*分", value)
    return match.group(1) if match else ""


def _access_copy(info: PageInfo) -> list[str]:
    location = re.sub(r"\s+", " ", info.location).strip()
    chunks = [
        chunk.strip(" ・")
        for chunk in re.split(r"[/／\n]", location)
        if chunk.strip(" ・")
    ]
    cleaned = [
        re.sub(r"(駅)?より", "駅 ", chunk).replace("  ", " ").strip()
        for chunk in chunks[:2]
    ]
    if cleaned and all(len(line) <= 25 for line in cleaned):
        return cleaned

    stations = info.nearest_stations[:2]
    minutes = _walk_minutes(location)
    if stations:
        first = f"{stations[0]}駅"
        if minutes:
            first += f" 徒歩{minutes}分"
        else:
            first += " 最寄り"
        lines = [first]
        if len(stations) > 1:
            lines.append(f"{stations[1]}駅からもアクセス")
        return lines
    if location:
        return [location[:24]]
    return ["店舗ページでアクセスを確認"]


def _headline_copy(info: PageInfo, description: str) -> list[str]:
    text = "\n".join(
        value for value in (info.source_text, description) if value
    )
    rules = [
        (
            r"ネイル|nail|爪|深爪",
            ["指先のお悩みに寄り添う、", "ネイルデザインをご提案。"],
        ),
        (
            r"まつ毛|まつげ|眉毛|アイサロン|アイラッシュ|eyelash",
            ["目元の魅力を引き出す、", "似合わせデザイン。"],
        ),
        (
            r"メンズ|男性",
            ["清潔感と扱いやすさを、", "あなたらしいスタイルで。"],
        ),
        (
            r"縮毛|髪質改善|ストレート|うねり",
            ["髪のお悩みに寄り添う、", "扱いやすい美髪へ。"],
        ),
        (
            r"カラー|ブリーチ|ハイライト|インナー|イヤリング",
            ["自分らしさを彩る、", "似合わせカラー。"],
        ),
        (
            r"ショート|ボブ",
            ["毎日が扱いやすい、", "似合わせスタイルへ。"],
        ),
        (
            r"トリートメント|ケア|ダメージ|ツヤ",
            ["髪をいたわりながら、", "つややかな仕上がりへ。"],
        ),
        (
            r"ヘア|カット|美容師|美容室|髪",
            ["あなたらしさを活かす、", "似合わせヘアをご提案。"],
        ),
        (
            r"エステ|脱毛|フェイシャル|ボディケア",
            ["自分らしい美しさへ、", "丁寧に寄り添うケア。"],
        ),
    ]
    for pattern, headline in rules:
        if re.search(pattern, text, re.IGNORECASE):
            return headline
    return ["あなたらしさを活かす、", "似合わせデザインをご提案。"]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_source_name(candidate: SourceCandidate, extension: str) -> str:
    prefix = (
        "style"
        if "style_photo" in candidate.source_type
        else "person"
    )
    return f"{prefix}-{candidate.record_id}.{extension}"


def _download_candidate(
    candidate: SourceCandidate,
    source_dir: Path,
) -> dict[str, Any]:
    response = _request(
        candidate.image_url,
        headers={"Accept": "image/avif,image/webp,image/*,*/*;q=0.8"},
    )
    content_type = str(response.headers.get("Content-Type") or "").lower()
    if not content_type.startswith("image/"):
        raise CatalogBuildError(
            f"{candidate.image_url}: unexpected Content-Type {content_type!r}"
        )
    if len(response.content) > MAX_SOURCE_BYTES:
        raise CatalogBuildError(
            f"{candidate.image_url}: source exceeds {MAX_SOURCE_BYTES} bytes"
        )

    extension = {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "image/avif": "avif",
    }.get(content_type.split(";", 1)[0], "img")
    target = source_dir / _safe_source_name(candidate, extension)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(
        f".{target.name}.{threading.get_ident()}.download"
    )
    temp.write_bytes(response.content)
    try:
        with Image.open(temp) as opened:
            oriented = ImageOps.exif_transpose(opened)
            oriented.load()
            width, height = oriented.size
            image_format = str(opened.format or "").upper()
        if min(width, height) < MIN_SOURCE_SHORT_SIDE:
            raise CatalogBuildError(
                f"{candidate.image_url}: source too small {width}x{height}"
            )
        temp.replace(target)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    return {
        "candidate": candidate,
        "path": target,
        "sha256": _sha256(target),
        "width": width,
        "height": height,
        "format": image_format,
        "final_url": response.url,
    }


def _prepare_group_sources(
    info: PageInfo,
    product_count: int,
    source_dir: Path,
) -> tuple[list[dict[str, Any]], list[str]]:
    if info.error:
        return [], [info.error]
    if not info.candidates:
        return [], ["Hairbookページに既存ヘアスタイル画像がありません"]

    prepared: list[dict[str, Any]] = []
    errors: list[str] = []
    needed = min(product_count, len(info.candidates))
    for candidate in info.candidates:
        try:
            prepared.append(_download_candidate(candidate, source_dir))
        except Exception as exc:
            errors.append(f"{candidate.record_id}: {exc}")
        if len(prepared) >= needed:
            break
    return prepared, errors


def _asset_id(product_id: str) -> str:
    parsed = parse_product_id(product_id)
    suffix = hashlib.sha256(product_id.encode("utf-8")).hexdigest()[:14]
    return f"product-{parsed['salon_id']}-{suffix}-person-v3-v1"


def _manifest_source_path(manifest_dir: Path, source_path: Path) -> str:
    try:
        return str(source_path.resolve().relative_to(manifest_dir.resolve()))
    except ValueError as exc:
        raise CatalogBuildError(
            f"source must be under manifest directory: {source_path}"
        ) from exc


def _build_asset(
    row: dict[str, str],
    info: PageInfo,
    source: dict[str, Any],
    manifest_dir: Path,
) -> dict[str, Any]:
    product_id = str(row["id"])
    parsed = parse_product_id(product_id)
    candidate: SourceCandidate = source["candidate"]
    copy: dict[str, Any] = {
        "area": _area_copy(info),
        "salon_name": _clean_salon_name(info.salon_name),
        "salon_name_full": info.salon_name,
        "access": _access_copy(info),
        "headline": _headline_copy(
            info,
            str(row.get("description") or ""),
        ),
        "support": (
            f"{info.staff_name}｜担当者ページへ"
            if info.staff_name
            else ""
        ),
        "cta": "詳しく見る",
    }
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
        "theme": "ink_gold",
        "source": {
            "path": _manifest_source_path(manifest_dir, source["path"]),
            "page_url": candidate.page_url,
            "image_url": candidate.image_url,
            "resolved_image_url": source["final_url"],
            "sha256": source["sha256"],
            "source_type": candidate.source_type,
            "subject_scope": "stylist" if parsed["stylist_id"] else "salon",
            "width": source["width"],
            "height": source["height"],
        },
        "image": {
            "focal_x": 0.5,
            "focal_y": 0.35,
            "brightness": 1.0,
            "saturation": 0.96,
            "contrast": 1.02,
        },
        "copy": copy,
    }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def build_catalog(
    *,
    rows: list[dict[str, str]],
    output_dir: Path,
    workers: int,
    limit: int = 0,
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
    for row in active:
        try:
            key = _landing_key(row["link"])
        except Exception:
            key = f"invalid:{row.get('id', '')}"
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

    prepared_by_page: dict[str, tuple[list[dict[str, Any]], list[str]]] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                _prepare_group_sources,
                page_infos[key],
                len(group),
                source_dir,
            ): key
            for key, group in grouped.items()
            if key in page_infos
        }
        for future in as_completed(futures):
            prepared_by_page[futures[future]] = future.result()

    assets: list[dict[str, Any]] = []
    assets_by_signature: dict[str, dict[str, Any]] = {}
    classifications: list[dict[str, Any]] = []
    next_review_on = (date.today() + timedelta(days=7)).isoformat()
    for key, group in grouped.items():
        info = page_infos.get(key)
        prepared, source_errors = prepared_by_page.get(key, ([], []))
        for index, row in enumerate(group):
            parsed = parse_product_id(row["id"])
            common = {
                "product_id": row["id"],
                "salon_id": parsed["salon_id"],
                "stylist_id": parsed["stylist_id"],
                "landing_url": row.get("link", ""),
                "current_image_url": row.get("image_link", ""),
                "feed_title_reference": row.get("title", ""),
            }
            if info and prepared:
                source = prepared[index % len(prepared)]
                asset = _build_asset(row, info, source, output_dir)
                signature = json.dumps(
                    {
                        "salon_id": asset["salon_id"],
                        "stylist_id": asset.get("stylist_id", ""),
                        "landing_url": asset["landing_url"],
                        "source_sha256": asset["source"]["sha256"],
                        "theme": asset["theme"],
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
                    }
                )
            else:
                reason_parts = []
                if not info:
                    reason_parts.append("着地URLを解決できません")
                elif info.error:
                    reason_parts.append(info.error)
                else:
                    reason_parts.append("利用可能な既存人物スタイル画像がありません")
                if source_errors:
                    reason_parts.append("; ".join(source_errors[:3]))
                classifications.append(
                    {
                        **common,
                        "asset_id": "",
                        "eligibility_status": "person_source_hold",
                        "hold_reason": " / ".join(reason_parts),
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
        "source_policy": {
            "priority": "existing_person_style_images_on_product_landing_page",
            "video_capture_used": False,
            "generated_person_used": False,
            "fallback_to_current_banner_used": False,
        },
        "experiment": {
            "experiment_id": f"hb02_person_v3_full_{date.today():%Y%m%d}",
            "rollout_batch": "full_review_draft",
            "baseline": "28_complete_days_before_switch",
            "evaluation": "first_7_complete_days_then_14_complete_days",
            "primary_metrics": ["ctr", "cpm"],
            "secondary_metrics": ["meta_add_to_cart"],
            "aggregation": "salon_id_x_creative_version_x_complete_day",
            "exclude": ["switch_day", "mixed_old_new_day", "incomplete_day"],
        },
        "assets": assets,
    }
    summary = {
        "schema_version": "hairbook.person_v3_full_build_summary.v1",
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
        "assets": len(assets),
        "eligible_products": sum(
            row["eligibility_status"] == "eligible"
            for row in classifications
        ),
        "held_products": sum(
            row["eligibility_status"] != "eligible"
            for row in classifications
        ),
        "page_errors": sum(bool(info.error) for info in page_infos.values()),
        "pages_without_style_sources": sum(
            not info.candidates for info in page_infos.values()
        ),
        "unique_source_files": len(
            {asset["source"]["sha256"] for asset in assets}
        ),
        "manifest": str((output_dir / "manifest.json").resolve()),
        "classification": str(
            (output_dir / "classification.json").resolve()
        ),
    }
    _write_json(output_dir / "manifest.json", manifest)
    _write_json(
        output_dir / "classification.json",
        {
            "schema_version": "hairbook.person_v3_classification.v1",
            "generated_at": now,
            "rows": classifications,
        },
    )
    _write_json(
        output_dir / "page_discovery.json",
        {
            "schema_version": "hairbook.person_v3_page_discovery.v1",
            "generated_at": now,
            "pages": {
                key: {
                    "salon_id": info.salon_id,
                    "stylist_id": info.stylist_id,
                    "salon_name": info.salon_name,
                    "location": info.location,
                    "nearest_stations": info.nearest_stations,
                    "staff_name": info.staff_name,
                    "candidate_count": len(info.candidates),
                    "candidates": [
                        {
                            "source_type": candidate.source_type,
                            "record_id": candidate.record_id,
                            "page_url": candidate.page_url,
                            "image_url": candidate.image_url,
                        }
                        for candidate in info.candidates
                    ],
                    "error": info.error,
                }
                for key, info in sorted(page_infos.items())
            },
        },
    )
    _write_json(output_dir / "summary.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build a full Person V3 draft manifest from existing Hairbook "
            "salon/staff style photographs."
        )
    )
    parser.add_argument("--feed-csv", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=8)
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
        )
    except CatalogBuildError as exc:
        parser.exit(2, f"full catalog build failed: {exc}\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
