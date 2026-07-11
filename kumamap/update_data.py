#!/usr/bin/env python3
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

DETAIL_PAGE_URL = "https://www2.wagmap.jp/sendaicity/OpenDataDetail?lid=20129&mids=331"
FALLBACK_CSV_URL = (
    "https://www2.wagmap.jp/sendaicity/"
    "sendaicity/opendatafile/map_331/CSV/opendata_20129.csv"
)
SOURCE_PAGE_URL = (
    "https://www.city.sendai.jp/joho-kikaku/shise/security/"
    "kokai/opendeta_r08kumashutubotu.html"
)

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_JSON = ROOT / "kumamap" / "incidents.json"
STATUS_JSON = ROOT / "kumamap" / "status.json"

USER_AGENT = "KumaMapDataUpdater/1.1"


def fetch_bytes(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Cache-Control": "no-cache",
        },
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        return response.read()


def discover_csv_url() -> str:
    try:
        html = fetch_bytes(DETAIL_PAGE_URL).decode("utf-8", errors="ignore")
    except Exception as error:
        print(f"詳細ページの確認に失敗。固定URLを使用します: {error}")
        return FALLBACK_CSV_URL

    patterns = [
        r'https?://[^"\']+?/CSV/[^"\']+?\.csv',
        r'/(?:[^"\']*/)?opendatafile/[^"\']+?/CSV/[^"\']+?\.csv',
    ]

    for pattern in patterns:
        match = re.search(pattern, html, flags=re.IGNORECASE)
        if match:
            found = match.group(0).replace("&amp;", "&")
            if found.startswith("http"):
                return found
            return urllib.request.urljoin(DETAIL_PAGE_URL, found)

    print("CSV URLをページから検出できなかったため固定URLを使用します。")
    return FALLBACK_CSV_URL


def decode_text(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp932", "shift_jis", "utf-16"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise RuntimeError("CSVの文字コードを判定できませんでした。")


def normalize_header(value: str) -> str:
    return str(value or "").replace("\ufeff", "").replace("　", " ").strip()


def find_column(headers: Iterable[str], names: Iterable[str]) -> str | None:
    normalized = {normalize_header(header).lower(): header for header in headers}

    for name in names:
        key = normalize_header(name).lower()
        if key in normalized:
            return normalized[key]

    return None


def parse_rows(text: str) -> list[dict[str, str]]:
    sample = text[:4096]

    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel

    reader = csv.DictReader(io.StringIO(text), dialect=dialect)

    if not reader.fieldnames:
        raise RuntimeError("CSVのヘッダーを検出できませんでした。")

    headers = [
        normalize_header(header)
        for header in reader.fieldnames
        if header is not None
    ]

    detected = {
        "date": find_column(headers, ("出没日時", "発見日時", "日時", "日付")),
        "location": find_column(headers, ("出没場所", "発見場所", "場所", "所在地")),
        "longitude": find_column(headers, ("経度", "longitude", "lng", "x")),
        "latitude": find_column(headers, ("緯度", "latitude", "lat", "y")),
        "category": find_column(headers, ("分類", "区分", "種別")),
        "size": find_column(headers, ("頭数及び体長", "頭数・体長", "頭数", "体長")),
        "other": find_column(headers, ("その他", "備考", "詳細", "概要")),
    }

    missing = [
        key
        for key in ("date", "location", "longitude", "latitude")
        if not detected[key]
    ]

    if missing:
        raise RuntimeError(
            "必要な列を検出できません: "
            + ", ".join(missing)
            + f" / ヘッダー: {headers}"
        )

    rows: list[dict[str, str]] = []

    for raw_row in reader:
        row = {
            normalize_header(key): (
                str(value).strip() if value is not None else ""
            )
            for key, value in raw_row.items()
            if key is not None
        }

        if not any(row.values()):
            continue

        for key, column in detected.items():
            row[f"_{key}_col"] = column or ""

        rows.append(row)

    return rows


def get(row: dict[str, str], marker: str) -> str:
    column = row.get(marker, "")
    return row.get(column, "").strip() if column else ""


def normalize_date(value: str) -> str:
    text = value.strip()

    if not text:
        return ""

    gregorian = re.search(
        r"(20\d{2})[年/.\-](\d{1,2})[月/.\-](\d{1,2})",
        text,
    )
    if gregorian:
        year, month, day = map(int, gregorian.groups())
        return f"{year:04d}-{month:02d}-{day:02d}"

    reiwa = re.search(
        r"令和\s*(\d+|元)\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})",
        text,
    )
    if reiwa:
        era_year_text, month_text, day_text = reiwa.groups()
        era_year = 1 if era_year_text == "元" else int(era_year_text)
        return (
            f"{2018 + era_year:04d}-"
            f"{int(month_text):02d}-"
            f"{int(day_text):02d}"
        )

    abbreviated = re.search(
        r"R\s*(\d+)\s*[./\-]\s*(\d{1,2})\s*[./\-]\s*(\d{1,2})",
        text,
        flags=re.IGNORECASE,
    )
    if abbreviated:
        era_year, month, day = map(int, abbreviated.groups())
        return f"{2018 + era_year:04d}-{month:02d}-{day:02d}"

    return text


def parse_float(value: str) -> float | None:
    cleaned = value.replace(",", "").strip()
    match = re.search(r"-?\d+(?:\.\d+)?", cleaned)

    if not match:
        return None

    try:
        return float(match.group(0))
    except ValueError:
        return None


def normalize_category(value: str, combined_text: str) -> str:
    raw_category = value.strip()

    # 仙台市CSVの「分類」列には、出没種別ではなく
    # データセット名が入る場合があるため、その場合は無視する。
    generic_category = (
        not raw_category
        or "クマ出没情報" in raw_category
        or "熊出没情報" in raw_category
    )

    text = combined_text if generic_category else f"{raw_category} {combined_text}"

    if any(word in text for word in ("人身", "負傷", "けが", "ケガ", "襲撃", "襲われ")):
        return "被害"

    if any(word in text for word in ("捕獲", "駆除")):
        return "捕獲"

    if any(word in text for word in ("痕跡", "足跡", "ふん", "フン", "糞", "食痕")):
        return "痕跡"

    if not generic_category:
        return raw_category

    return "目撃"


def infer_municipality(location: str) -> str:
    for ward in ("青葉区", "宮城野区", "若林区", "太白区", "泉区"):
        if ward in location:
            return f"仙台市{ward}"

    return "仙台市"


def infer_risk_level(category: str, location: str, description: str) -> str:
    text = f"{category} {location} {description}"

    if category == "被害":
        return "高"

    high_words = (
        "住宅", "民家", "学校", "小学校", "中学校", "高校",
        "幼稚園", "保育園", "市街地", "駅", "公園",
        "通学路", "道路", "敷地", "店舗",
    )

    if any(word in text for word in high_words):
        return "高"

    if category in ("目撃", "痕跡"):
        return "中"

    return "低"


def stable_id(*parts: object) -> str:
    raw = "|".join(str(part) for part in parts)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"SENDAI-{digest}"


def build_incidents(rows: list[dict[str, str]]) -> list[dict[str, object]]:
    incidents: list[dict[str, object]] = []

    for row in rows:
        date_text = normalize_date(get(row, "_date_col"))
        location = get(row, "_location_col")
        longitude = parse_float(get(row, "_longitude_col"))
        latitude = parse_float(get(row, "_latitude_col"))
        raw_category = get(row, "_category_col")
        size_text = get(row, "_size_col")
        other_text = get(row, "_other_col")

        if latitude is None or longitude is None:
            continue

        if not (
            20.0 <= latitude <= 50.0
            and 120.0 <= longitude <= 155.0
        ):
            continue

        description_parts = [
            part
            for part in (
                f"頭数・体長：{size_text}" if size_text else "",
                other_text,
            )
            if part
        ]

        description = " / ".join(description_parts) or "詳細情報なし"

        category = normalize_category(
            raw_category,
            f"{location} {size_text} {other_text}",
        )

        municipality = infer_municipality(location)
        risk_level = infer_risk_level(category, location, description)

        incidents.append(
            {
                "id": stable_id(
                    date_text,
                    location,
                    latitude,
                    longitude,
                    category,
                ),
                "date": date_text,
                "prefecture": "宮城県",
                "municipality": municipality,
                "locationText": location or f"{municipality} 位置情報あり",
                "category": category,
                "description": description,
                "riskLevel": risk_level,
                "sourceName": "仙台市",
                "sourceUrl": SOURCE_PAGE_URL,
                "latitude": latitude,
                "longitude": longitude,
            }
        )

    incidents.sort(
        key=lambda item: (
            str(item.get("date", "")),
            str(item.get("id", "")),
        ),
        reverse=True,
    )

    return incidents


def main() -> int:
    csv_url = discover_csv_url()
    print(f"CSV取得先: {csv_url}")

    raw = fetch_bytes(csv_url)
    text = decode_text(raw)
    rows = parse_rows(text)
    incidents = build_incidents(rows)

    if not incidents:
        raise RuntimeError(
            "変換結果が0件のため、既存JSONを更新しません。"
        )

    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)

    OUTPUT_JSON.write_text(
        json.dumps(incidents, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    status = {
        "updatedAt": (
            datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        ),
        "count": len(incidents),
        "sourceName": "仙台市",
        "sourceCsvUrl": csv_url,
        "sourcePageUrl": SOURCE_PAGE_URL,
    }

    STATUS_JSON.write_text(
        json.dumps(status, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"生成完了: {len(incidents)}件")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise
