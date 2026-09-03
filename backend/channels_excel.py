"""Dependency-free Excel export for saved WeChat Channels authors."""

from __future__ import annotations

import math
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr


_INVALID_XML_CHARS = re.compile(
    "[^\x09\x0A\x0D\x20-\uD7FF\uE000-\uFFFD\U00010000-\U0010FFFF]"
)
_EXCEL_EPOCH = datetime(1899, 12, 30)


def _column_name(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _cell_ref(row: int, column: int) -> str:
    return f"{_column_name(column)}{row}"


def _safe_text(value) -> str:
    text = "" if value is None else str(value)
    text = _INVALID_XML_CHARS.sub("", text)
    return text[:32767]


def _text_cell(row: int, column: int, value, style: int = 3) -> str:
    text = _safe_text(value)
    preserve = ' xml:space="preserve"' if text != text.strip() or "\n" in text else ""
    return (
        f'<c r="{_cell_ref(row, column)}" s="{style}" t="inlineStr">'
        f"<is><t{preserve}>{escape(text)}</t></is></c>"
    )


def _number_cell(row: int, column: int, value, style: int = 4) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0
    if not math.isfinite(number):
        number = 0
    rendered = str(int(number)) if number.is_integer() else repr(number)
    return f'<c r="{_cell_ref(row, column)}" s="{style}" t="n"><v>{rendered}</v></c>'


def _excel_datetime(value):
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        timestamp = float(value)
        dt = datetime.fromtimestamp(timestamp)
    except (TypeError, ValueError, OSError, OverflowError):
        return None
    return (dt - _EXCEL_EPOCH).total_seconds() / 86400


def _date_cell(row: int, column: int, value) -> str:
    serial = _excel_datetime(value)
    if serial is None:
        return _text_cell(row, column, "", 3)
    return f'<c r="{_cell_ref(row, column)}" s="5" t="n"><v>{serial:.10f}</v></c>'


def _sheet_xml(
    title: str,
    summary: str,
    headers: list[str],
    rows: list[list[dict]],
    widths: list[float],
    hyperlinks: list[tuple[str, str]],
    empty_message: str,
    selected: bool = False,
) -> str:
    column_count = len(headers)
    last_column = _column_name(column_count)
    last_row = max(5, 4 + len(rows))
    selection = ' tabSelected="1"' if selected else ""
    columns_xml = "".join(
        f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
        for index, width in enumerate(widths, 1)
    )

    sheet_rows = [
        f'<row r="1" ht="28" customHeight="1">{_text_cell(1, 1, title, 1)}</row>',
        f'<row r="2" ht="22" customHeight="1">{_text_cell(2, 1, summary, 8)}</row>',
        '<row r="3" ht="8" customHeight="1"/>',
        (
            '<row r="4" ht="26" customHeight="1">'
            + "".join(_text_cell(4, column, header, 2) for column, header in enumerate(headers, 1))
            + "</row>"
        ),
    ]

    if rows:
        for row_number, row in enumerate(rows, 5):
            cells = []
            row_height = 36 if any(
                cell.get("type") == "long"
                and len(_safe_text(cell.get("value"))) > 36
                for cell in row
            ) else 21
            for column, cell in enumerate(row, 1):
                cell_type = cell.get("type", "text")
                if cell_type == "number":
                    cells.append(_number_cell(row_number, column, cell.get("value")))
                elif cell_type == "date":
                    cells.append(_date_cell(row_number, column, cell.get("value")))
                elif cell_type == "long":
                    cells.append(_text_cell(row_number, column, cell.get("value"), 7))
                elif cell_type == "url":
                    value = _safe_text(cell.get("value"))
                    cells.append(_text_cell(row_number, column, value, 6 if value else 3))
                    if value:
                        hyperlinks.append((_cell_ref(row_number, column), value))
                else:
                    cells.append(_text_cell(row_number, column, cell.get("value"), 3))
            sheet_rows.append(
                f'<row r="{row_number}" ht="{row_height}" customHeight="1">'
                + "".join(cells)
                + "</row>"
            )
    else:
        sheet_rows.append(
            f'<row r="5" ht="24" customHeight="1">{_text_cell(5, 1, empty_message, 9)}</row>'
        )

    hyperlink_xml = ""
    if hyperlinks:
        hyperlink_xml = "<hyperlinks>" + "".join(
            f'<hyperlink ref="{cell_ref}" r:id="rId{index}"/>'
            for index, (cell_ref, _) in enumerate(hyperlinks, 1)
        ) + "</hyperlinks>"

    merge_ranges = [f"A1:{last_column}1", f"A2:{last_column}2"]
    if not rows:
        merge_ranges.append(f"A5:{last_column}5")
    merges_xml = (
        f'<mergeCells count="{len(merge_ranges)}">'
        + "".join(f'<mergeCell ref="{cell_range}"/>' for cell_range in merge_ranges)
        + "</mergeCells>"
    )

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<dimension ref="A1:{last_column}{last_row}"/>'
        f'<sheetViews><sheetView workbookViewId="0" showGridLines="0"{selection}>'
        '<pane ySplit="4" topLeftCell="A5" activePane="bottomLeft" state="frozen"/>'
        '<selection pane="bottomLeft" activeCell="A5" sqref="A5"/>'
        '</sheetView></sheetViews>'
        '<sheetFormatPr defaultRowHeight="18"/>'
        f"<cols>{columns_xml}</cols>"
        f"<sheetData>{''.join(sheet_rows)}</sheetData>"
        f'<autoFilter ref="A4:{last_column}{max(4, 4 + len(rows))}"/>'
        f"{merges_xml}{hyperlink_xml}"
        '<pageMargins left="0.3" right="0.3" top="0.5" bottom="0.5" header="0.2" footer="0.2"/>'
        '<pageSetup orientation="landscape" fitToWidth="1" fitToHeight="0"/>'
        '</worksheet>'
    )


def _hyperlink_relationships(hyperlinks: list[tuple[str, str]]) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(
            '<Relationship '
            f'Id="rId{index}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
            f'Target={quoteattr(url)} TargetMode="External"/>'
            for index, (_, url) in enumerate(hyperlinks, 1)
        )
        + '</Relationships>'
    )


def _styles_xml() -> str:
    return '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy-mm-dd hh:mm:ss"/></numFmts>
  <fonts count="4">
    <font><sz val="10"/><name val="Microsoft YaHei"/><family val="2"/></font>
    <font><b/><sz val="16"/><color rgb="FFFFFFFF"/><name val="Microsoft YaHei"/><family val="2"/></font>
    <font><b/><sz val="10"/><color rgb="FFFFFFFF"/><name val="Microsoft YaHei"/><family val="2"/></font>
    <font><u/><sz val="10"/><color rgb="FF0563C1"/><name val="Microsoft YaHei"/><family val="2"/></font>
  </fonts>
  <fills count="5">
    <fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FF1F4E78"/><bgColor indexed="64"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FF07A652"/><bgColor indexed="64"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFEAF7F0"/><bgColor indexed="64"/></patternFill></fill>
  </fills>
  <borders count="2">
    <border><left/><right/><top/><bottom/><diagonal/></border>
    <border><left/><right/><top/><bottom style="thin"><color rgb="FFD9E2E8"/></bottom><diagonal/></border>
  </borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="10">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment vertical="center"/></xf>
    <xf numFmtId="0" fontId="2" fillId="3" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
    <xf numFmtId="49" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyAlignment="1" quotePrefix="1"><alignment vertical="center"/></xf>
    <xf numFmtId="3" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment horizontal="right" vertical="center"/></xf>
    <xf numFmtId="164" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
    <xf numFmtId="49" fontId="3" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" applyAlignment="1" quotePrefix="1"><alignment vertical="center"/></xf>
    <xf numFmtId="49" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyAlignment="1" quotePrefix="1"><alignment vertical="top" wrapText="1"/></xf>
    <xf numFmtId="0" fontId="0" fillId="4" borderId="0" xfId="0" applyFill="1" applyAlignment="1"><alignment vertical="center"/></xf>
    <xf numFmtId="0" fontId="0" fillId="4" borderId="0" xfId="0" applyFill="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
  </cellXfs>
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>'''


def write_channels_export_xlsx(payload: dict, output_path: Path) -> None:
    """Write the export payload to a polished two-sheet .xlsx workbook."""
    creators = payload.get("creators") if isinstance(payload, dict) else []
    creators = creators if isinstance(creators, list) else []
    oss_configured = bool(payload.get("oss_configured"))

    creator_rows = []
    video_rows = []
    video_index = 0
    for creator_index, creator in enumerate(creators, 1):
        creator = creator if isinstance(creator, dict) else {}
        username = creator.get("username") or ""
        nickname = creator.get("nickname") or username
        videos = creator.get("videos")
        videos = videos if isinstance(videos, list) else []
        creator_rows.append([
            {"type": "number", "value": creator_index},
            {"value": nickname},
            {"value": username},
            {"type": "url", "value": creator.get("head_img_url") or ""},
            {"type": "url", "value": creator.get("video_url") or ""},
            {"type": "date", "value": creator.get("added_time")},
            {"type": "number", "value": len(videos)},
        ])

        for video in videos:
            video = video if isinstance(video, dict) else {}
            video_index += 1
            duration = (
                video.get("duration_seconds", video.get("duration"))
                if video.get("duration_seconds", video.get("duration")) is not None
                else video.get("videoDuration", video.get("videoPlayLen"))
            )
            oss_video_url = ""
            if oss_configured:
                oss_video_url = video.get("oss_video_url") or ""
                if not oss_video_url and video.get("oss_object_key"):
                    from backend.oss import LEGACY_OSS_BUCKET, build_oss_public_url
                    # Never relocate old objects when the current credentials change.
                    oss_video_url = build_oss_public_url(
                        video.get("oss_object_key"),
                        video.get("oss_bucket") or LEGACY_OSS_BUCKET,
                    )
            video_rows.append([
                {"type": "number", "value": video_index},
                {"value": nickname},
                {"value": username},
                {"value": video.get("id") or ""},
                {"type": "long", "value": video.get("description") or ""},
                {"type": "date", "value": video.get("createtime")},
                {"type": "number", "value": duration or 0},
                {"type": "number", "value": video.get("like_count", video.get("likeCount", 0)) or 0},
                {"type": "number", "value": video.get("share_count", video.get("forwardCount", video.get("shareCount", 0))) or 0},
                {"type": "number", "value": video.get("favorite_count", video.get("favCount", 0)) or 0},
                {"type": "number", "value": video.get("comment_count", video.get("commentCount", 0)) or 0},
                {"type": "url", "value": video.get("cover_url") or ""},
                {"type": "url", "value": video.get("video_url") or ""},
                {"type": "url", "value": oss_video_url},
            ])

    exported_at = payload.get("exported_at") or datetime.now().astimezone().isoformat()
    summary = (
        f"导出时间：{exported_at}    |    "
        f"创作者：{len(creator_rows)} 个    |    作品：{len(video_rows)} 个"
    )
    creator_headers = [
        "序号", "创作者昵称", "创作者 ID", "头像链接", "来源视频链接",
        "收藏时间", "作品数量",
    ]
    video_headers = [
        "序号", "创作者昵称", "创作者 ID", "作品 ID", "作品描述", "发布时间",
        "时长（秒）", "点赞数", "分享数", "收藏数", "评论数", "封面链接",
        "默认视频链接", "OSS视频链接",
    ]
    creator_links: list[tuple[str, str]] = []
    video_links: list[tuple[str, str]] = []
    creator_sheet = _sheet_xml(
        "视频号创作者数据",
        summary,
        creator_headers,
        creator_rows,
        [7, 20, 36, 34, 34, 20, 12],
        creator_links,
        "暂无创作者数据",
        selected=True,
    )
    video_sheet = _sheet_xml(
        "视频号作品明细",
        summary,
        video_headers,
        video_rows,
        [7, 20, 36, 24, 42, 20, 12, 11, 11, 11, 11, 32, 34, 34],
        video_links,
        "当前导出范围内暂无已解析作品",
    )

    now_utc = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    content_types = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>'''
    root_relationships = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>'''
    workbook_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <bookViews><workbookView activeTab="0"/></bookViews>
  <sheets>
    <sheet name="创作者" sheetId="1" r:id="rId1"/>
    <sheet name="作品明细" sheetId="2" r:id="rId2"/>
  </sheets>
  <calcPr calcId="191029" calcMode="auto"/>
</workbook>'''
    workbook_relationships = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>'''
    core_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>视频号创作者数据导出</dc:title><dc:creator>自媒体内容采集工具</dc:creator>
  <dcterms:created xsi:type="dcterms:W3CDTF">{now_utc}</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">{now_utc}</dcterms:modified>
</cp:coreProperties>'''
    app_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>自媒体内容采集工具</Application><DocSecurity>0</DocSecurity><ScaleCrop>false</ScaleCrop>
  <HeadingPairs><vt:vector size="2" baseType="variant"><vt:variant><vt:lpstr>工作表</vt:lpstr></vt:variant><vt:variant><vt:i4>2</vt:i4></vt:variant></vt:vector></HeadingPairs>
  <TitlesOfParts><vt:vector size="2" baseType="lpstr"><vt:lpstr>创作者</vt:lpstr><vt:lpstr>作品明细</vt:lpstr></vt:vector></TitlesOfParts>
</Properties>'''

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as workbook:
        workbook.writestr("[Content_Types].xml", content_types)
        workbook.writestr("_rels/.rels", root_relationships)
        workbook.writestr("docProps/core.xml", core_xml)
        workbook.writestr("docProps/app.xml", app_xml)
        workbook.writestr("xl/workbook.xml", workbook_xml)
        workbook.writestr("xl/_rels/workbook.xml.rels", workbook_relationships)
        workbook.writestr("xl/styles.xml", _styles_xml())
        workbook.writestr("xl/worksheets/sheet1.xml", creator_sheet)
        workbook.writestr("xl/worksheets/sheet2.xml", video_sheet)
        if creator_links:
            workbook.writestr(
                "xl/worksheets/_rels/sheet1.xml.rels",
                _hyperlink_relationships(creator_links),
            )
        if video_links:
            workbook.writestr(
                "xl/worksheets/_rels/sheet2.xml.rels",
                _hyperlink_relationships(video_links),
            )
