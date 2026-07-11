#!/usr/bin/env python3
from __future__ import annotations

import csv
import hashlib
import html as html_module
import io
import json
import math
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

SENDAI_DETAIL_PAGE_URL = (
    "https://www2.wagmap.jp/sendaicity/"
    "OpenDataDetail?lid=20129&mids=331"
)
SENDAI_FALLBACK_CSV_URL = (
    "https://www2.wagmap.jp/sendaicity/"
    "sendaicity/opendatafile/map_331/CSV/"
    "opendata_20129.csv"
)
SENDAI_SOURCE_PAGE_URL = (
    "https://www.city.sendai.jp/joho-kikaku/"
    "shise/security/kokai/"
    "opendeta_r08kumashutubotu.html"
)

MIYAGI_SOURCE_PAGE_URL = (
    "https://www.pref.miyagi.jp/soshiki/"
    "sizenhogo/r8kumamokugeki.html"
)
MIYAGI_FALLBACK_MAP_ID = (
    "12_b92SRipXWwvkUfNCsDdEUWhEOmzUc"
)

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_JSON = ROOT / "kumamap" / "incidents.json"
STATUS_JSON = ROOT / "kumamap" / "status.json"
DIAGNOSTICS_JSON = (
    ROOT / "kumamap" / "miyagi_diagnostics.json"
)

USER_AGENT = "KumaMapDataUpdater/4.0-miyagi-prefecture"
DEFAULT_YEAR = 2026
SENDAI_WARDS = (
    "青葉区",
    "宮城野区",
    "若林区",
    "太白区",
    "泉区",
)


def fetch_bytes(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Cache-Control": "no-cache",
        },
    )

    with urllib.request.urlopen(
        request,
        timeout=60,
    ) as response:
        return response.read()


def decode_text(raw: bytes) -> str:
    for encoding in (
        "utf-8-sig",
        "utf-8",
        "cp932",
        "shift_jis",
        "utf-16",
    ):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue

    raise RuntimeError(
        "テキストの文字コードを判定できませんでした。"
    )


def normalize_header(value: str) -> str:
    return (
        str(value or "")
        .replace("\ufeff", "")
        .replace("　", " ")
        .strip()
    )


def find_column(
    headers: Iterable[str],
    names: Iterable[str],
) -> str | None:
    normalized = {
        normalize_header(header).lower(): header
        for header in headers
    }

    for name in names:
        key = normalize_header(name).lower()

        if key in normalized:
            return normalized[key]

    return None


def discover_sendai_csv_url() -> str:
    try:
        page_html = decode_text(
            fetch_bytes(SENDAI_DETAIL_PAGE_URL)
        )
    except Exception as error:
        print(
            "仙台市詳細ページの確認に失敗。"
            f"固定URLを使用します: {error}"
        )
        return SENDAI_FALLBACK_CSV_URL

    patterns = (
        r'https?://[^"\']+?/CSV/[^"\']+?\.csv',
        (
            r'/(?:[^"\']*/)?opendatafile/'
            r'[^"\']+?/CSV/[^"\']+?\.csv'
        ),
    )

    for pattern in patterns:
        match = re.search(
            pattern,
            page_html,
            flags=re.IGNORECASE,
        )

        if match:
            found = html_module.unescape(
                match.group(0)
            )

            if found.startswith("http"):
                return found

            return urllib.parse.urljoin(
                SENDAI_DETAIL_PAGE_URL,
                found,
            )

    print(
        "仙台市CSV URLをページから検出できないため、"
        "固定URLを使用します。"
    )
    return SENDAI_FALLBACK_CSV_URL


def parse_sendai_rows(
    text: str,
) -> list[dict[str, str]]:
    sample = text[:4096]

    try:
        dialect = csv.Sniffer().sniff(
            sample,
            delimiters=",\t;",
        )
    except csv.Error:
        dialect = csv.excel

    reader = csv.DictReader(
        io.StringIO(text),
        dialect=dialect,
    )

    if not reader.fieldnames:
        raise RuntimeError(
            "仙台市CSVのヘッダーを検出できませんでした。"
        )

    headers = [
        normalize_header(header)
        for header in reader.fieldnames
        if header is not None
    ]

    detected = {
        "date": find_column(
            headers,
            ("出没日時", "発見日時", "日時", "日付"),
        ),
        "location": find_column(
            headers,
            ("出没場所", "発見場所", "場所", "所在地"),
        ),
        "longitude": find_column(
            headers,
            ("経度", "longitude", "lng", "x"),
        ),
        "latitude": find_column(
            headers,
            ("緯度", "latitude", "lat", "y"),
        ),
        "category": find_column(
            headers,
            ("分類", "区分", "種別"),
        ),
        "size": find_column(
            headers,
            (
                "頭数及び体長",
                "頭数・体長",
                "頭数",
                "体長",
            ),
        ),
        "other": find_column(
            headers,
            ("その他", "備考", "詳細", "概要"),
        ),
    }

    missing = [
        key
        for key in (
            "date",
            "location",
            "longitude",
            "latitude",
        )
        if not detected[key]
    ]

    if missing:
        raise RuntimeError(
            "仙台市CSVで必要な列を検出できません: "
            + ", ".join(missing)
            + f" / ヘッダー: {headers}"
        )

    rows: list[dict[str, str]] = []

    for raw_row in reader:
        row = {
            normalize_header(key): (
                str(value).strip()
                if value is not None
                else ""
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


def get_row_value(
    row: dict[str, str],
    marker: str,
) -> str:
    column = row.get(marker, "")

    if not column:
        return ""

    return row.get(column, "").strip()


def normalize_date(
    value: str,
    default_year: int = DEFAULT_YEAR,
) -> str:
    text = value.strip()

    if not text:
        return ""

    gregorian = re.search(
        (
            r"(20\d{2})\s*[年/.\-]\s*"
            r"(\d{1,2})\s*[月/.\-]\s*"
            r"(\d{1,2})"
        ),
        text,
    )

    if gregorian:
        year, month, day = map(
            int,
            gregorian.groups(),
        )
        return f"{year:04d}-{month:02d}-{day:02d}"

    reiwa = re.search(
        (
            r"令和\s*(\d+|元)\s*年\s*"
            r"(\d{1,2})\s*月\s*"
            r"(\d{1,2})"
        ),
        text,
    )

    if reiwa:
        era_year_text, month_text, day_text = (
            reiwa.groups()
        )
        era_year = (
            1
            if era_year_text == "元"
            else int(era_year_text)
        )
        return (
            f"{2018 + era_year:04d}-"
            f"{int(month_text):02d}-"
            f"{int(day_text):02d}"
        )

    abbreviated = re.search(
        (
            r"R\s*(\d+)\s*[./\-]\s*"
            r"(\d{1,2})\s*[./\-]\s*"
            r"(\d{1,2})"
        ),
        text,
        flags=re.IGNORECASE,
    )

    if abbreviated:
        era_year, month, day = map(
            int,
            abbreviated.groups(),
        )
        return (
            f"{2018 + era_year:04d}-"
            f"{month:02d}-{day:02d}"
        )

    month_day_japanese = re.search(
        r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*日",
        text,
    )

    if month_day_japanese:
        month, day = map(
            int,
            month_day_japanese.groups(),
        )
        return (
            f"{default_year:04d}-"
            f"{month:02d}-{day:02d}"
        )

    month_day_slash = re.search(
        (
            r"(?<![\d/])(\d{1,2})\s*[./\-]\s*"
            r"(\d{1,2})(?!\s*[./\-]\s*\d)"
        ),
        text,
    )

    if month_day_slash:
        month, day = map(
            int,
            month_day_slash.groups(),
        )

        if 1 <= month <= 12 and 1 <= day <= 31:
            return (
                f"{default_year:04d}-"
                f"{month:02d}-{day:02d}"
            )

    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        serial = float(text)
        if 40000 <= serial <= 70000:
            converted = datetime(1899, 12, 30) + timedelta(days=serial)
            return converted.strftime("%Y-%m-%d")

    return ""


def parse_float(value: str) -> float | None:
    cleaned = value.replace(",", "").strip()
    match = re.search(
        r"-?\d+(?:\.\d+)?",
        cleaned,
    )

    if not match:
        return None

    try:
        return float(match.group(0))
    except ValueError:
        return None


def normalize_category(
    value: str,
    combined_text: str,
) -> str:
    raw_category = value.strip()

    generic_category = (
        not raw_category
        or "クマ出没情報" in raw_category
        or "熊出没情報" in raw_category
    )

    text = (
        combined_text
        if generic_category
        else f"{raw_category} {combined_text}"
    )

    if any(
        word in text
        for word in (
            "人身",
            "負傷",
            "けが",
            "ケガ",
            "襲撃",
            "襲われ",
        )
    ):
        return "被害"

    if any(
        word in text
        for word in ("捕獲", "駆除")
    ):
        return "捕獲"

    if any(
        word in text
        for word in (
            "痕跡",
            "足跡",
            "ふん",
            "フン",
            "糞",
            "食痕",
        )
    ):
        return "痕跡"

    if any(
        word in text
        for word in (
            "目撃",
            "出没",
            "発見",
        )
    ):
        return "目撃"

    if not generic_category:
        return raw_category

    return "目撃"


def infer_municipality(location: str) -> str:
    for ward in SENDAI_WARDS:
        if ward in location:
            return f"仙台市{ward}"

    return "仙台市"


def infer_risk_level(
    category: str,
    location: str,
    description: str,
) -> str:
    text = f"{category} {location} {description}"

    if category == "被害":
        return "高"

    high_words = (
        "住宅",
        "民家",
        "学校",
        "小学校",
        "中学校",
        "高校",
        "幼稚園",
        "保育園",
        "市街地",
        "駅",
        "公園",
        "通学路",
        "道路",
        "敷地",
        "店舗",
    )

    if any(word in text for word in high_words):
        return "高"

    if category in ("目撃", "痕跡"):
        return "中"

    return "低"


def stable_id(
    prefix: str,
    *parts: object,
) -> str:
    raw = "|".join(str(part) for part in parts)
    digest = hashlib.sha1(
        raw.encode("utf-8")
    ).hexdigest()[:16]

    return f"{prefix}-{digest}"


def build_sendai_incidents(
    rows: list[dict[str, str]],
) -> list[dict[str, object]]:
    incidents: list[dict[str, object]] = []

    for row in rows:
        date_text = normalize_date(
            get_row_value(row, "_date_col")
        )
        location = get_row_value(
            row,
            "_location_col",
        )
        longitude = parse_float(
            get_row_value(row, "_longitude_col")
        )
        latitude = parse_float(
            get_row_value(row, "_latitude_col")
        )
        raw_category = get_row_value(
            row,
            "_category_col",
        )
        size_text = get_row_value(
            row,
            "_size_col",
        )
        other_text = get_row_value(
            row,
            "_other_col",
        )

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
                (
                    f"頭数・体長：{size_text}"
                    if size_text
                    else ""
                ),
                other_text,
            )
            if part
        ]

        description = (
            " / ".join(description_parts)
            or "詳細情報なし"
        )

        category = normalize_category(
            raw_category,
            f"{location} {size_text} {other_text}",
        )
        municipality = infer_municipality(location)
        risk_level = infer_risk_level(
            category,
            location,
            description,
        )

        incidents.append(
            {
                "id": stable_id(
                    "SENDAI",
                    date_text,
                    location,
                    round(latitude, 6),
                    round(longitude, 6),
                    category,
                ),
                "date": date_text,
                "prefecture": "宮城県",
                "municipality": municipality,
                "locationText": (
                    location
                    or f"{municipality} 位置情報あり"
                ),
                "category": category,
                "description": description,
                "riskLevel": risk_level,
                "sourceName": "仙台市",
                "sourceUrl": SENDAI_SOURCE_PAGE_URL,
                "latitude": latitude,
                "longitude": longitude,
            }
        )

    return incidents



def excel_column_index(cell_reference: str) -> int:
    letters = re.match(r"[A-Z]+", cell_reference or "")
    if not letters:
        return 0

    index = 0
    for character in letters.group(0):
        index = index * 26 + (ord(character) - ord("A") + 1)

    return max(index - 1, 0)


def read_xlsx_shared_strings(
    archive: zipfile.ZipFile,
) -> list[str]:
    path = "xl/sharedStrings.xml"
    if path not in archive.namelist():
        return []

    root = ET.fromstring(archive.read(path))
    values: list[str] = []

    for item in root.iter():
        if local_tag_name(item.tag) != "si":
            continue

        parts = [
            element.text or ""
            for element in item.iter()
            if local_tag_name(element.tag) == "t"
        ]
        values.append("".join(parts).strip())

    return values


def read_xlsx_sheet_paths(
    archive: zipfile.ZipFile,
) -> list[tuple[str, str]]:
    workbook_root = ET.fromstring(
        archive.read("xl/workbook.xml")
    )
    relationships_root = ET.fromstring(
        archive.read("xl/_rels/workbook.xml.rels")
    )

    relationships: dict[str, str] = {}
    for relationship in relationships_root:
        relationship_id = relationship.attrib.get("Id", "")
        target = relationship.attrib.get("Target", "")
        if relationship_id and target:
            if target.startswith("/"):
                full_path = target.lstrip("/")
            else:
                full_path = "xl/" + target.lstrip("/")
            relationships[relationship_id] = full_path

    result: list[tuple[str, str]] = []
    for sheet in workbook_root.iter():
        if local_tag_name(sheet.tag) != "sheet":
            continue

        name = sheet.attrib.get("name", "")
        relationship_id = ""
        for key, value in sheet.attrib.items():
            if key.endswith("}id") or key == "r:id":
                relationship_id = value
                break

        path = relationships.get(relationship_id, "")
        if name and path:
            result.append((name, path))

    return result


def read_xlsx_rows(
    archive: zipfile.ZipFile,
    sheet_path: str,
    shared_strings: list[str],
) -> list[list[str]]:
    root = ET.fromstring(archive.read(sheet_path))
    rows: list[list[str]] = []

    for row_element in root.iter():
        if local_tag_name(row_element.tag) != "row":
            continue

        values_by_column: dict[int, str] = {}
        max_column = -1

        for cell in list(row_element):
            if local_tag_name(cell.tag) != "c":
                continue

            reference = cell.attrib.get("r", "A1")
            column_index = excel_column_index(reference)
            max_column = max(max_column, column_index)
            cell_type = cell.attrib.get("t", "")

            raw_value = ""
            inline_parts = [
                element.text or ""
                for element in cell.iter()
                if local_tag_name(element.tag) == "t"
            ]

            value_element = next(
                (
                    element
                    for element in cell
                    if local_tag_name(element.tag) == "v"
                ),
                None,
            )

            if cell_type == "inlineStr":
                raw_value = "".join(inline_parts)
            elif value_element is not None:
                raw_value = value_element.text or ""
                if cell_type == "s":
                    try:
                        raw_value = shared_strings[int(raw_value)]
                    except (ValueError, IndexError):
                        pass
            elif inline_parts:
                raw_value = "".join(inline_parts)

            values_by_column[column_index] = normalize_header(raw_value)

        if max_column < 0:
            continue

        row = [
            values_by_column.get(index, "")
            for index in range(max_column + 1)
        ]

        if any(row):
            rows.append(row)

    return rows


def header_score(row: list[str]) -> int:
    combined = "|".join(normalize_header(value) for value in row)
    keywords = (
        "市町村",
        "自治体",
        "目撃日時",
        "出没日時",
        "日時",
        "日付",
        "場所",
        "所在地",
        "区分",
        "種別",
        "分類",
        "頭数",
        "体長",
    )
    return sum(1 for keyword in keywords if keyword in combined)


def unique_headers(values: list[str]) -> list[str]:
    result: list[str] = []
    counts: dict[str, int] = {}

    for index, value in enumerate(values):
        base = normalize_header(value) or f"列{index + 1}"
        counts[base] = counts.get(base, 0) + 1
        if counts[base] == 1:
            result.append(base)
        else:
            result.append(f"{base}_{counts[base]}")

    return result


def row_to_dict(
    headers: list[str],
    values: list[str],
) -> dict[str, str]:
    return {
        header: (values[index] if index < len(values) else "")
        for index, header in enumerate(headers)
        if (values[index] if index < len(values) else "")
    }


def normalize_serial_number(value: str) -> str:
    text = normalize_header(value)

    if not text:
        return ""

    match = re.search(r"\d+(?:\.0+)?", text)

    if not match:
        return ""

    try:
        return str(int(float(match.group(0))))
    except ValueError:
        return ""


def parse_integer_cell(value: str) -> int | None:
    text = normalize_header(value)

    if not text:
        return None

    try:
        number = int(float(text))
    except ValueError:
        match = re.search(r"\d+", text)
        if not match:
            return None
        number = int(match.group(0))

    return number


def excel_time_to_text(value: str) -> str:
    text = normalize_header(value)

    if not text:
        return ""

    clock_match = re.search(
        r"(?<!\d)(\d{1,2})\s*[:：]\s*(\d{1,2})(?!\d)",
        text,
    )

    if clock_match:
        hour, minute = map(int, clock_match.groups())
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}"

    try:
        fraction = float(text)
    except ValueError:
        return text

    fraction = fraction % 1.0
    total_minutes = int(round(fraction * 24 * 60)) % (24 * 60)
    hour, minute = divmod(total_minutes, 60)
    return f"{hour:02d}:{minute:02d}"


def find_header_position(
    headers: list[str],
    keywords: tuple[str, ...],
) -> int | None:
    for index, header in enumerate(headers):
        normalized = normalize_header(header)
        if any(keyword in normalized for keyword in keywords):
            return index

    return None


def cell_at(row: list[str], index: int | None) -> str:
    if index is None or index < 0 or index >= len(row):
        return ""

    return normalize_header(row[index])


def parse_miyagi_xlsx(
    raw: bytes,
) -> tuple[list[dict[str, str]], dict[str, object]]:
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        shared_strings = read_xlsx_shared_strings(archive)
        sheet_paths = read_xlsx_sheet_paths(archive)
        candidates: list[dict[str, object]] = []

        for sheet_name, sheet_path in sheet_paths:
            rows = read_xlsx_rows(
                archive,
                sheet_path,
                shared_strings,
            )

            if not rows:
                continue

            scored_rows = [
                (header_score(row), index)
                for index, row in enumerate(rows[:30])
            ]
            best_score, header_index = max(
                scored_rows,
                default=(0, 0),
            )
            headers = rows[header_index]

            number_index = find_header_position(
                headers,
                ("番号",),
            )
            month_index = find_header_position(
                headers,
                ("発見日時", "目撃日時", "出没日時", "日時"),
            )
            municipality_index = find_header_position(
                headers,
                ("市区町村", "市町村", "自治体"),
            )
            district_index = find_header_position(
                headers,
                ("地区", "場所", "所在地"),
            )
            count_index = find_header_position(
                headers,
                ("頭数",),
            )
            category_index = find_header_position(
                headers,
                ("痕跡", "種別", "区分", "分類"),
            )
            office_index = find_header_position(
                headers,
                ("事務所",),
            )

            if (
                number_index is None
                or month_index is None
                or municipality_index is None
            ):
                continue

            day_index = month_index + 1
            time_index = month_index + 2
            parsed_rows: list[dict[str, str]] = []

            for row in rows[header_index + 1 :]:
                serial = normalize_serial_number(
                    cell_at(row, number_index)
                )

                if not serial:
                    continue

                municipality = cell_at(
                    row,
                    municipality_index,
                )

                if not municipality:
                    continue

                month = parse_integer_cell(
                    cell_at(row, month_index)
                )
                day = parse_integer_cell(
                    cell_at(row, day_index)
                )

                if (
                    month is None
                    or day is None
                    or not (1 <= month <= 12)
                    or not (1 <= day <= 31)
                ):
                    continue

                parsed_rows.append(
                    {
                        "serial": serial,
                        "date": (
                            f"{DEFAULT_YEAR:04d}-"
                            f"{month:02d}-{day:02d}"
                        ),
                        "time": excel_time_to_text(
                            cell_at(row, time_index)
                        ),
                        "office": cell_at(row, office_index),
                        "municipality": municipality,
                        "district": cell_at(row, district_index),
                        "count": cell_at(row, count_index),
                        "category": cell_at(row, category_index),
                    }
                )

            candidates.append(
                {
                    "sheetName": sheet_name,
                    "sheetPath": sheet_path,
                    "headerRowIndex": header_index + 1,
                    "headerScore": best_score,
                    "headers": unique_headers(headers),
                    "rows": parsed_rows,
                }
            )

        if not candidates:
            raise RuntimeError(
                "宮城県Excelから対象シートを検出できませんでした。"
            )

        selected = max(
            candidates,
            key=lambda candidate: len(candidate["rows"]),
        )
        selected_rows = selected["rows"]

        if not isinstance(selected_rows, list):
            raise RuntimeError(
                "宮城県Excelの解析結果が不正です。"
            )

        diagnostics = {
            "xlsxByteCount": len(raw),
            "sheetCount": len(sheet_paths),
            "selectedSheetName": selected["sheetName"],
            "selectedHeaderRowIndex": selected["headerRowIndex"],
            "selectedHeaders": selected["headers"],
            "miyagiRowCount": len(selected_rows),
            "miyagiSampleRows": selected_rows[:20],
            "candidateSheets": [
                {
                    "sheetName": candidate["sheetName"],
                    "miyagiRowCount": len(candidate["rows"]),
                }
                for candidate in candidates
            ],
        }

        return selected_rows, diagnostics

def discover_miyagi_sources(
    page_html: str,
) -> tuple[str, str]:
    xlsx_url = ""
    map_id = ""

    xlsx_match = re.search(
        r'href=["\']([^"\']+?\.xlsx(?:\?[^"\']*)?)["\']',
        page_html,
        flags=re.IGNORECASE,
    )

    if xlsx_match:
        xlsx_url = urllib.parse.urljoin(
            MIYAGI_SOURCE_PAGE_URL,
            html_module.unescape(
                xlsx_match.group(1)
            ),
        )

    map_patterns = (
        r'[?&]mid=([A-Za-z0-9_-]+)',
        (
            r'google\.com/maps/d/(?:u/\d+/)?'
            r'(?:edit|viewer)\?[^"\']*?'
            r'mid=([A-Za-z0-9_-]+)'
        ),
    )

    for pattern in map_patterns:
        match = re.search(
            pattern,
            page_html,
            flags=re.IGNORECASE,
        )

        if match:
            map_id = html_module.unescape(
                match.group(1)
            )
            break

    return (
        xlsx_url,
        map_id or MIYAGI_FALLBACK_MAP_ID,
    )


def kml_candidate_urls(
    map_id: str,
) -> list[str]:
    quoted_map_id = urllib.parse.quote(
        map_id,
        safe="_-",
    )

    return [
        (
            "https://www.google.com/maps/d/kml"
            f"?mid={quoted_map_id}&forcekml=1"
        ),
        (
            "https://www.google.com/maps/d/u/0/kml"
            f"?mid={quoted_map_id}&forcekml=1"
        ),
        (
            "https://www.google.com/maps/d/u/1/kml"
            f"?mid={quoted_map_id}&forcekml=1"
        ),
    ]


def decode_kml_payload(raw: bytes) -> str:
    if raw[:2] == b"PK":
        with zipfile.ZipFile(
            io.BytesIO(raw)
        ) as archive:
            kml_names = [
                name
                for name in archive.namelist()
                if name.lower().endswith(".kml")
            ]

            if not kml_names:
                raise RuntimeError(
                    "KMZ内にKMLファイルがありません。"
                )

            return decode_text(
                archive.read(kml_names[0])
            )

    return decode_text(raw)


def fetch_miyagi_kml(
    map_id: str,
) -> tuple[str, str, list[str]]:
    errors: list[str] = []

    for url in kml_candidate_urls(map_id):
        try:
            raw = fetch_bytes(url)
            kml_text = decode_kml_payload(raw)

            if (
                "<kml" not in kml_text.lower()
                or "<placemark" not in kml_text.lower()
            ):
                raise RuntimeError(
                    "取得内容がKMLではありません。"
                )

            return kml_text, url, errors
        except Exception as error:
            errors.append(f"{url}: {error}")

    raise RuntimeError(
        " / ".join(errors)
        or "宮城県Googleマップを取得できません。"
    )


def local_tag_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def direct_child_text(
    element: ET.Element,
    child_name: str,
) -> str:
    for child in list(element):
        if local_tag_name(child.tag) == child_name:
            return (child.text or "").strip()

    return ""


def all_extended_values(
    placemark: ET.Element,
) -> list[str]:
    values: list[str] = []

    for element in placemark.iter():
        tag_name = local_tag_name(element.tag)

        if tag_name not in (
            "value",
            "SimpleData",
        ):
            continue

        text = (element.text or "").strip()

        if text:
            values.append(text)

    return values


def html_to_text(value: str) -> str:
    text = html_module.unescape(value or "")
    text = re.sub(
        r"(?i)<\s*br\s*/?\s*>",
        " | ",
        text,
    )
    text = re.sub(
        r"(?i)</\s*(?:p|div|tr|li|h\d)\s*>",
        " | ",
        text,
    )
    text = re.sub(
        r"(?i)</\s*(?:td|th)\s*>",
        "：",
        text,
    )
    text = re.sub(
        r"<[^>]+>",
        " ",
        text,
    )
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    text = re.sub(
        r"(?:\s*\|\s*)+",
        " | ",
        text,
    )

    return text.strip(" |")


def collect_placemarks(
    container: ET.Element,
    folder_names: tuple[str, ...] = (),
) -> list[tuple[ET.Element, tuple[str, ...]]]:
    collected: list[
        tuple[ET.Element, tuple[str, ...]]
    ] = []

    for child in list(container):
        tag_name = local_tag_name(child.tag)

        if tag_name == "Folder":
            folder_name = direct_child_text(
                child,
                "name",
            )
            next_names = (
                folder_names
                + ((folder_name,) if folder_name else ())
            )
            collected.extend(
                collect_placemarks(
                    child,
                    next_names,
                )
            )
        elif tag_name == "Placemark":
            collected.append(
                (child, folder_names)
            )
        else:
            collected.extend(
                collect_placemarks(
                    child,
                    folder_names,
                )
            )

    return collected


def parse_coordinates(
    placemark: ET.Element,
) -> tuple[float, float] | None:
    for element in placemark.iter():
        if local_tag_name(element.tag) != "coordinates":
            continue

        text = (element.text or "").strip()

        if not text:
            continue

        first_coordinate = text.split()[0]
        parts = first_coordinate.split(",")

        if len(parts) < 2:
            continue

        try:
            longitude = float(parts[0])
            latitude = float(parts[1])
        except ValueError:
            continue

        if (
            20.0 <= latitude <= 50.0
            and 120.0 <= longitude <= 155.0
        ):
            return latitude, longitude

    return None


def is_sendai_text(text: str) -> bool:
    if "仙台市" in text:
        return True

    return any(
        ward in text
        for ward in SENDAI_WARDS
    )


def extract_location_text(
    combined_text: str,
    placemark_name: str,
) -> str:
    label_match = re.search(
        (
            r"(?:場所|所在地|出没場所|目撃場所)"
            r"\s*[：:]\s*"
            r"([^|]{2,100})"
        ),
        combined_text,
    )

    if label_match:
        location = label_match.group(1).strip(
            " ：:、,。"
        )
    else:
        sendai_match = re.search(
            (
                r"仙台市"
                r"(?:青葉区|宮城野区|若林区|太白区|泉区)?"
                r"[^|]{0,90}"
            ),
            combined_text,
        )

        if sendai_match:
            location = sendai_match.group(0)
        else:
            location = placemark_name

    location = re.sub(
        (
            r"(?:目撃|痕跡|その他|出没|発見)"
            r"\s*[：:]?\s*$"
        ),
        "",
        location,
    )
    location = re.sub(
        (
            r"(?:20\d{2}[年/.\-]\d{1,2}[月/.\-]\d{1,2}日?"
            r"|\d{1,2}月\d{1,2}日"
            r"|\d{1,2}[./\-]\d{1,2})"
        ),
        "",
        location,
    )
    location = re.sub(r"\s+", " ", location)
    location = location.strip(
        " ：:、,。|-"
    )

    return location or "仙台市内（宮城県速報地点）"


def clean_description(
    name: str,
    description_text: str,
    folder_text: str,
) -> str:
    parts = []

    for part in (
        description_text,
        f"区分：{folder_text}" if folder_text else "",
    ):
        cleaned = re.sub(r"\s+", " ", part).strip()

        if (
            cleaned
            and cleaned != name
            and cleaned not in parts
        ):
            parts.append(cleaned)

    result = " / ".join(parts)

    if not result:
        return "宮城県クマ目撃等情報マップ掲載地点"

    return result[:280]


def extract_kml_serial_number(
    combined_text: str,
) -> str:
    match = re.search(
        r"通し番号\s*[：:]\s*(\d+(?:\.0+)?)",
        combined_text,
    )

    if not match:
        return ""

    return normalize_serial_number(match.group(1))


def parse_miyagi_kml_points(
    kml_text: str,
) -> tuple[
    dict[str, list[dict[str, object]]],
    dict[str, object],
]:
    root = ET.fromstring(kml_text)
    placemark_entries = collect_placemarks(root)
    points_by_serial: dict[
        str,
        list[dict[str, object]],
    ] = {}
    sample_points: list[dict[str, object]] = []
    point_count = 0
    serial_point_count = 0

    for placemark, folder_names in placemark_entries:
        coordinates = parse_coordinates(placemark)

        if coordinates is None:
            continue

        point_count += 1
        name = html_to_text(
            direct_child_text(placemark, "name")
        )
        description_text = html_to_text(
            direct_child_text(placemark, "description")
        )
        extended_values = [
            html_to_text(value)
            for value in all_extended_values(placemark)
            if value
        ]
        folder_text = " / ".join(
            folder_name
            for folder_name in folder_names
            if folder_name
        )
        combined_text = " | ".join(
            part
            for part in (
                folder_text,
                name,
                description_text,
                *extended_values,
            )
            if part
        )
        serial = extract_kml_serial_number(
            combined_text
        )

        if not serial:
            continue

        serial_point_count += 1
        latitude, longitude = coordinates
        point = {
            "serial": serial,
            "date": normalize_date(
                combined_text,
                default_year=DEFAULT_YEAR,
            ),
            "name": name,
            "folderText": folder_text,
            "description": description_text,
            "latitude": latitude,
            "longitude": longitude,
        }
        points_by_serial.setdefault(serial, []).append(point)

        if len(sample_points) < 20:
            sample_points.append(point)

    diagnostics = {
        "placemarkCount": len(placemark_entries),
        "pointPlacemarkCount": point_count,
        "serialPointCount": serial_point_count,
        "uniqueSerialCount": len(points_by_serial),
        "duplicateSerialCount": sum(
            1
            for points in points_by_serial.values()
            if len(points) > 1
        ),
        "pointSamples": sample_points,
    }

    return points_by_serial, diagnostics


def choose_kml_point(
    row: dict[str, str],
    points: list[dict[str, object]],
) -> dict[str, object] | None:
    if not points:
        return None

    row_date = row.get("date", "")
    matching_date = [
        point
        for point in points
        if str(point.get("date", "")) == row_date
    ]

    if matching_date:
        return matching_date[0]

    return points[0]


def compose_miyagi_location(
    municipality: str,
    district: str,
) -> str:
    municipality = normalize_header(municipality)
    district = normalize_header(district)

    if not district:
        return municipality or "宮城県内"

    if district.startswith(municipality):
        return district

    if district.startswith("仙台市"):
        return district

    for ward in SENDAI_WARDS:
        if district.startswith(ward):
            return f"仙台市{district}"

    return f"{municipality} {district}".strip()


def build_miyagi_incidents(
    xlsx_rows: list[dict[str, str]],
    points_by_serial: dict[
        str,
        list[dict[str, object]],
    ],
) -> tuple[
    list[dict[str, object]],
    dict[str, object],
]:
    incidents: list[dict[str, object]] = []
    unmatched_rows: list[dict[str, str]] = []
    date_mismatch_count = 0

    for row in xlsx_rows:
        serial = row.get("serial", "")
        points = points_by_serial.get(serial, [])
        point = choose_kml_point(row, points)

        if point is None:
            if len(unmatched_rows) < 30:
                unmatched_rows.append(row)
            continue

        row_date = row.get("date", "")
        point_date = str(point.get("date", ""))

        if row_date and point_date and row_date != point_date:
            date_mismatch_count += 1

        date_text = row_date or point_date
        latitude = float(point["latitude"])
        longitude = float(point["longitude"])
        municipality = row.get("municipality", "宮城県")
        district = row.get("district", "")
        location = compose_miyagi_location(
            municipality,
            district,
        )
        category = normalize_category(
            row.get("category", ""),
            " ".join(row.values()),
        )
        description_parts = [
            part
            for part in (
                (
                    f"時刻：{row.get('time', '')}"
                    if row.get("time")
                    else ""
                ),
                (
                    f"頭数：{row.get('count', '')}"
                    if row.get("count")
                    else ""
                ),
                f"宮城県一覧番号：{serial}",
            )
            if part
        ]
        description = " / ".join(description_parts)
        risk_level = infer_risk_level(
            category,
            location,
            description,
        )

        incidents.append(
            {
                "id": stable_id(
                    "MIYAGI",
                    serial,
                    date_text,
                    round(latitude, 6),
                    round(longitude, 6),
                    category,
                ),
                "date": date_text,
                "prefecture": "宮城県",
                "municipality": (
                    municipality or infer_municipality(location)
                ),
                "locationText": location,
                "category": category,
                "description": description,
                "riskLevel": risk_level,
                "sourceName": "宮城県",
                "sourceUrl": MIYAGI_SOURCE_PAGE_URL,
                "latitude": latitude,
                "longitude": longitude,
            }
        )

    diagnostics = {
        "xlsxMiyagiCount": len(xlsx_rows),
        "matchedPointCount": len(incidents),
        "unmatchedPointCount": (
            len(xlsx_rows) - len(incidents)
        ),
        "dateMismatchCount": date_mismatch_count,
        "unmatchedSamples": unmatched_rows,
    }

    return incidents, diagnostics

def distance_km(
    first: dict[str, object],
    second: dict[str, object],
) -> float:
    lat1 = math.radians(
        float(first["latitude"])
    )
    lon1 = math.radians(
        float(first["longitude"])
    )
    lat2 = math.radians(
        float(second["latitude"])
    )
    lon2 = math.radians(
        float(second["longitude"])
    )

    delta_latitude = lat2 - lat1
    delta_longitude = lon2 - lon1

    value = (
        math.sin(delta_latitude / 2) ** 2
        + math.cos(lat1)
        * math.cos(lat2)
        * math.sin(delta_longitude / 2) ** 2
    )

    return 6371.0 * 2.0 * math.atan2(
        math.sqrt(value),
        math.sqrt(1.0 - value),
    )


def normalize_location_key(value: str) -> str:
    return re.sub(
        r"[\s　、,。・\-ー（）()\[\]【】]",
        "",
        value or "",
    )


def is_probable_duplicate(
    candidate: dict[str, object],
    existing: dict[str, object],
) -> bool:
    candidate_date = str(
        candidate.get("date", "")
    )
    existing_date = str(
        existing.get("date", "")
    )

    if (
        candidate_date
        and existing_date
        and candidate_date != existing_date
    ):
        return False

    if distance_km(candidate, existing) <= 0.35:
        return True

    candidate_location = normalize_location_key(
        str(candidate.get("locationText", ""))
    )
    existing_location = normalize_location_key(
        str(existing.get("locationText", ""))
    )

    if (
        len(candidate_location) >= 6
        and len(existing_location) >= 6
        and (
            candidate_location in existing_location
            or existing_location in candidate_location
        )
    ):
        return True

    return False


def is_sendai_municipality(
    municipality: object,
) -> bool:
    return str(municipality or "").startswith(
        "仙台市"
    )


def merge_incidents(
    sendai_incidents: list[dict[str, object]],
    miyagi_incidents: list[dict[str, object]],
) -> tuple[
    list[dict[str, object]],
    int,
    int,
]:
    merged = list(sendai_incidents)
    added_count = 0
    sendai_duplicate_count = 0

    for candidate in miyagi_incidents:
        candidate_is_sendai = (
            is_sendai_municipality(
                candidate.get("municipality", "")
            )
        )

        duplicate = False

        if candidate_is_sendai:
            duplicate = any(
                is_probable_duplicate(
                    candidate,
                    existing,
                )
                for existing in sendai_incidents
            )

        if duplicate:
            sendai_duplicate_count += 1
            continue

        merged.append(candidate)
        added_count += 1

    merged.sort(
        key=lambda item: (
            str(item.get("date", "")),
            str(item.get("id", "")),
        ),
        reverse=True,
    )

    return (
        merged,
        added_count,
        sendai_duplicate_count,
    )


def write_json(
    path: Path,
    value: object,
) -> None:
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    path.write_text(
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )



def load_previous_miyagi_incidents() -> list[dict[str, object]]:
    if not OUTPUT_JSON.exists():
        return []

    try:
        value = json.loads(
            OUTPUT_JSON.read_text(encoding="utf-8")
        )
    except Exception:
        return []

    if not isinstance(value, list):
        return []

    preserved: list[dict[str, object]] = []

    for item in value:
        if not isinstance(item, dict):
            continue

        source_name = str(
            item.get("sourceName", "")
        )

        if "宮城県" not in source_name:
            continue

        latitude = item.get("latitude")
        longitude = item.get("longitude")

        if not isinstance(
            latitude,
            (int, float),
        ) or not isinstance(
            longitude,
            (int, float),
        ):
            continue

        preserved.append(item)

    return preserved


def main() -> int:
    sendai_csv_url = discover_sendai_csv_url()
    print(f"仙台市CSV取得先: {sendai_csv_url}")

    sendai_raw = fetch_bytes(sendai_csv_url)
    sendai_rows = parse_sendai_rows(
        decode_text(sendai_raw)
    )
    sendai_incidents = build_sendai_incidents(
        sendai_rows
    )

    if not sendai_incidents:
        raise RuntimeError(
            "仙台市データが0件のため、"
            "既存JSONを更新しません。"
        )

    updated_at = (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    previous_miyagi_incidents = (
        load_previous_miyagi_incidents()
    )

    diagnostics: dict[str, object] = {
        "updatedAt": updated_at,
        "sendaiCitySourceCount": len(sendai_incidents),
        "miyagiPageUrl": MIYAGI_SOURCE_PAGE_URL,
        "miyagiStatus": "未取得",
        "miyagiMapId": "",
        "miyagiKmlUrl": "",
        "miyagiExcelUrl": "",
        "miyagiErrors": [],
    }
    miyagi_incidents: list[dict[str, object]] = []

    try:
        miyagi_page_html = decode_text(
            fetch_bytes(MIYAGI_SOURCE_PAGE_URL)
        )
        xlsx_url, map_id = discover_miyagi_sources(
            miyagi_page_html
        )

        if not xlsx_url:
            raise RuntimeError(
                "宮城県公式ページからExcel URLを検出できません。"
            )

        diagnostics["miyagiMapId"] = map_id
        diagnostics["miyagiExcelUrl"] = xlsx_url

        xlsx_raw = fetch_bytes(xlsx_url)
        xlsx_rows, xlsx_diagnostics = parse_miyagi_xlsx(
            xlsx_raw
        )
        diagnostics["xlsxDiagnostics"] = xlsx_diagnostics

        kml_text, kml_url, kml_errors = fetch_miyagi_kml(
            map_id
        )
        diagnostics["miyagiKmlUrl"] = kml_url
        diagnostics["miyagiErrors"] = kml_errors

        points_by_serial, kml_diagnostics = (
            parse_miyagi_kml_points(kml_text)
        )
        diagnostics["kmlDiagnostics"] = kml_diagnostics

        miyagi_incidents, join_diagnostics = (
            build_miyagi_incidents(
                xlsx_rows,
                points_by_serial,
            )
        )
        diagnostics["joinDiagnostics"] = join_diagnostics

        if len(xlsx_rows) < 100:
            raise RuntimeError(
                "宮城県Excelの件数が異常に少ないため、"
                "既存データを保持します。"
            )

        if len(miyagi_incidents) < 100:
            raise RuntimeError(
                "宮城県ExcelとGoogleマップの照合件数が"
                "異常に少ないため、既存データを保持します。"
            )

        if (
            len(previous_miyagi_incidents) >= 300
            and len(miyagi_incidents)
            < len(previous_miyagi_incidents) * 0.65
        ):
            raise RuntimeError(
                "宮城県データが前回から大幅に減少したため、"
                "既存データを保持します。"
            )

        diagnostics["miyagiStatus"] = "取得成功"
        diagnostics["miyagiExcelCount"] = len(
            xlsx_rows
        )
        diagnostics["miyagiMunicipalityCount"] = len(
            {
                row.get("municipality", "")
                for row in xlsx_rows
                if row.get("municipality", "")
            }
        )
        diagnostics["miyagiMatchedPointCount"] = len(
            miyagi_incidents
        )
    except Exception as error:
        errors = list(
            diagnostics.get("miyagiErrors", [])
        )
        errors.append(str(error))
        diagnostics["miyagiErrors"] = errors

        if previous_miyagi_incidents:
            diagnostics["miyagiStatus"] = (
                "前回データ保持"
            )
            diagnostics["miyagiPreservedCount"] = (
                len(previous_miyagi_incidents)
            )
            miyagi_incidents = (
                previous_miyagi_incidents
            )
            print(
                "宮城県データ取得に失敗したため、"
                f"前回の宮城県データ"
                f"{len(previous_miyagi_incidents)}件を保持します: "
                f"{error}"
            )
        else:
            diagnostics["miyagiStatus"] = (
                "取得失敗"
            )
            diagnostics["miyagiPreservedCount"] = 0
            miyagi_incidents = []
            print(
                "宮城県データ取得に失敗し、"
                "保持可能な前回データもありません: "
                f"{error}"
            )

    (
        incidents,
        miyagi_added_count,
        sendai_duplicate_count,
    ) = merge_incidents(
        sendai_incidents,
        miyagi_incidents,
    )

    diagnostics["miyagiJoinedCount"] = len(
        miyagi_incidents
    )
    diagnostics["miyagiAddedCount"] = miyagi_added_count
    diagnostics["sendaiDuplicateCount"] = (
        sendai_duplicate_count
    )
    diagnostics["finalCount"] = len(incidents)
    diagnostics["municipalityCount"] = len(
        {
            str(item.get("municipality", ""))
            for item in incidents
            if str(item.get("municipality", ""))
        }
    )
    diagnostics["sendaiAreaCount"] = sum(
        1
        for item in incidents
        if is_sendai_municipality(
            item.get("municipality", "")
        )
    )
    diagnostics["otherAreaCount"] = (
        len(incidents)
        - int(diagnostics["sendaiAreaCount"])
    )

    write_json(OUTPUT_JSON, incidents)

    status = {
        "updatedAt": updated_at,
        "count": len(incidents),
        "coverage": "宮城県全域",
        "sourceName": "宮城県・仙台市",
        "municipalityCount": diagnostics.get(
            "municipalityCount",
            0,
        ),
        "sendaiAreaCount": diagnostics.get(
            "sendaiAreaCount",
            0,
        ),
        "otherAreaCount": diagnostics.get(
            "otherAreaCount",
            0,
        ),
        "sendaiCitySourceCount": len(
            sendai_incidents
        ),
        "miyagiExcelCount": diagnostics.get(
            "miyagiExcelCount",
            0,
        ),
        "miyagiMatchedPointCount": diagnostics.get(
            "miyagiMatchedPointCount",
            0,
        ),
        "miyagiAddedCount": miyagi_added_count,
        "sendaiDuplicateCount": (
            sendai_duplicate_count
        ),
        "miyagiStatus": diagnostics["miyagiStatus"],
        "miyagiPreservedCount": diagnostics.get(
            "miyagiPreservedCount",
            0,
        ),
        "sourceCsvUrl": sendai_csv_url,
        "sourcePageUrl": SENDAI_SOURCE_PAGE_URL,
        "miyagiSourcePageUrl": MIYAGI_SOURCE_PAGE_URL,
        "miyagiKmlUrl": diagnostics.get(
            "miyagiKmlUrl",
            "",
        ),
        "miyagiExcelUrl": diagnostics.get(
            "miyagiExcelUrl",
            "",
        ),
    }

    write_json(STATUS_JSON, status)
    write_json(DIAGNOSTICS_JSON, diagnostics)

    print(
        "更新完了: "
        f"仙台市公式 {len(sendai_incidents)}件 / "
        f"宮城県照合 {len(miyagi_incidents)}件 / "
        f"仙台市重複除外 "
        f"{sendai_duplicate_count}件 / "
        f"宮城県全域 最終 "
        f"{len(incidents)}件"
    )

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"更新失敗: {error}", file=sys.stderr)
        raise
