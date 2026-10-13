"""
docx_updater.py - put the uploaded video's link into the course-records .docx
that lives next to the videos in Drive, and (new) revert that edit again.

Why this file was rewritten
---------------------------
The old matcher failed with "Matching row not found or it already had a link":

  * It tried the DATE as a fallback on every row, even rows whose Session #
    clearly differed.  "4/10/2026" (Oct 4) matched a recording from Oct 10
    because both "4" and "10" appear in it, so an earlier row that was already
    linked "won" and the real row was never reached.
  * It only looked at ``doc.tables[0]``.  The records docs repeat the title +
    a new table on later pages (e.g. Crash: sessions 26-29 sit in a second
    table that has no header row), so those sessions were never searched.
  * It treated "already has a link" as an error.

Now: every table is searched, the header row is optional, the Session # is the
only thing that decides the row (date is used only for rows with no session
number), and an already-linked row is reported, not raised.
"""
from __future__ import annotations

import copy
import io
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import docx
from docx.opc.constants import RELATIONSHIP_TYPE
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import qn
from docx.shared import RGBColor
from docx.text.run import Run
from lxml import etree

log = logging.getLogger("zoom_to_drive")

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# The exact colour Google Docs gives text when you press Ctrl+K and add a link.
GOOGLE_LINK_BLUE = RGBColor(0x11, 0x55, 0xCC)  # #1155CC

# Used for tables that have no header row (e.g. the Crash doc's second table).
DEFAULT_LAYOUT = {"session": 0, "date": 1, "day": 2, "link": None}  # link = last col


# --------------------------------------------------------------------------- #
# Table / row detection
# --------------------------------------------------------------------------- #
@dataclass
class _Cols:
    session: Optional[int]
    date: Optional[int]
    link: int


@dataclass
class LinkResult:
    """Outcome of trying to link one session inside one document."""
    status: str                      # "updated" | "already_linked" | "not_found"
    detail: str = ""
    # XML of everything that was in the Link cell before we touched it (undo).
    original_cell_xml: list[str] = field(default_factory=list)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def _is_session_header(h: str) -> bool:
    return h.startswith("session") or h in ("#", "no", "no.", "s", "num", "number")


def _row_texts(row) -> list[str]:
    return [_norm(c.text) for c in row.cells]


def _is_header_row(texts: list[str]) -> bool:
    """A header row mentions 'link' but has no bare session number in it."""
    if not any("link" in t for t in texts):
        return False
    return not any(re.fullmatch(r"s?\s*\d+", t) for t in texts)


def _columns_from_header(texts: list[str]) -> Optional[_Cols]:
    link = next((i for i, t in enumerate(texts) if "link" in t), None)
    if link is None:
        return None
    session = next((i for i, t in enumerate(texts) if _is_session_header(t)), None)
    date = next((i for i, t in enumerate(texts) if t == "date" or t.startswith("date")), None)
    return _Cols(session, date, link)


def _document_layout(doc) -> _Cols:
    """Column layout taken from the first table that has a real header row."""
    for table in doc.tables:
        if table.rows:
            texts = _row_texts(table.rows[0])
            if _is_header_row(texts):
                cols = _columns_from_header(texts)
                if cols:
                    return cols
    return _Cols(DEFAULT_LAYOUT["session"], DEFAULT_LAYOUT["date"], -1)  # -1 = last col


def _table_columns(table, layout: _Cols) -> tuple[Optional[_Cols], int]:
    """Return (columns, first_data_row_index) for a table, or (None, 0) if unusable."""
    if not table.rows:
        return None, 0
    texts = _row_texts(table.rows[0])
    ncols = len(texts)
    if _is_header_row(texts):
        cols = _columns_from_header(texts)
        return (cols, 1) if cols else (None, 0)

    # Header-less table: reuse the layout from the rest of the document.
    link = layout.link if 0 <= layout.link < ncols else ncols - 1
    session = layout.session if layout.session is not None and layout.session < ncols else None
    date = layout.date if layout.date is not None and layout.date < ncols else None
    if ncols < 2:
        return None, 0
    return _Cols(session, date, link), 0


def _session_matches(text: str, session_num: int) -> bool:
    m = re.fullmatch(r"s?\s*0*(\d+)", (text or "").strip(), re.IGNORECASE)
    matches = bool(m) and int(m.group(1)) == session_num
    log.debug("Session matching: text='%s', session_num=%d, match=%s, extracted=%s",
              text, session_num, matches, int(m.group(1)) if m else None)
    return matches


def _date_matches(text: str, when: Optional[datetime]) -> bool:
    """True if the cell date equals `when`, reading d/m/y or m/d/y (also 'm-d')."""
    if not when:
        return False
    nums = [int(n) for n in re.findall(r"\d+", text or "")]
    if len(nums) < 2:
        return False
    a, b = nums[0], nums[1]
    matches = (a, b) in ((when.day, when.month), (when.month, when.day))
    log.debug("Date matching: text='%s', nums=%s, when=%s, matches=%s", text, nums, when, matches)
    if not matches:
        return False
    if len(nums) >= 3 and nums[2] % 100 != when.year % 100:
        log.debug("Year mismatch: nums[2]=%s %% 100 = %s != when.year %% 100 = %s",
                  nums[2], nums[2] % 100, when.year % 100)
        return False
    return True


_HYPERLINK_XPATH = (
    ".//w:hyperlink | .//w:instrText[contains(., 'HYPERLINK')] "
    "| .//w:fldSimple[contains(@w:instr, 'HYPERLINK')]"
)


def _cell_has_link(cell) -> bool:
    return bool(cell._tc.xpath(_HYPERLINK_XPATH))


def _find_row(doc, session_num: int, when: Optional[datetime]):
    """
    Locate the Link cell for `session_num` in any table.

    Returns (link_cell, label, already_linked_cell_or_None).
    """
    layout = _document_layout(doc)
    log.info("Document layout: session_col=%s, date_col=%s, link_col=%s", layout.session, layout.date, layout.link)
    session_hits, date_hits = [], []

    for ti, table in enumerate(doc.tables, start=1):
        cols, start = _table_columns(table, layout)
        if not cols:
            log.debug("Table %d: no valid columns, skipping", ti)
            continue
        log.info("Table %d: cols.session=%s, cols.date=%s, cols.link=%s, start_row=%d, total_rows=%d",
                 ti, cols.session, cols.date, cols.link, start, len(table.rows))
        for ri in range(start, len(table.rows)):
            cells = table.rows[ri].cells
            if cols.link >= len(cells):
                log.debug("Table %d row %d: link column %d out of range (has %d cells)", ti, ri+1, cols.link, len(cells))
                continue
            link_cell = cells[cols.link]
            label = f"table {ti}, row {ri + 1}"

            sess_text = cells[cols.session].text if cols.session is not None and cols.session < len(cells) else ""
            has_session_value = bool(re.search(r"\d", sess_text))

            if has_session_value:
                if _session_matches(sess_text, session_num):
                    log.info("Found session match in %s: session_text='%s'", label, sess_text)
                    session_hits.append((link_cell, label))
                else:
                    log.debug("Session mismatch in %s: session_text='%s' != %d", label, sess_text, session_num)
                # A row with a *different* session number is never our row,
                # no matter what its date looks like.
            elif cols.date is not None and cols.date < len(cells):
                date_text = cells[cols.date].text
                if _date_matches(date_text, when):
                    log.info("Found date match in %s: date_text='%s'", label, date_text)
                    date_hits.append((link_cell, label))
                else:
                    log.debug("Date mismatch in %s: date_text='%s' != %s", label, date_text, when)

    log.info("Session hits: %d, Date hits: %d", len(session_hits), len(date_hits))
    hits = session_hits or date_hits
    if not hits:
        return None, "", None
    for cell, label in hits:
        if not _cell_has_link(cell):
            return cell, label, None
    return None, hits[0][1], hits[0][0]


# --------------------------------------------------------------------------- #
# Writing the hyperlink
# --------------------------------------------------------------------------- #
def _cell_blocks(cell) -> list:
    """Everything in the cell except its properties (tcPr)."""
    return [el for el in cell._tc if el.tag != qn("w:tcPr")]


def _serialize(el) -> str:
    return etree.tostring(el, encoding="unicode")


def _make_hyperlink(doc, paragraph, url: str, text: str, base_rpr):
    r_id = doc.part.relate_to(url, RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), r_id)
    hyperlink.set(qn("w:history"), "1")

    r = OxmlElement("w:r")
    if base_rpr is not None:
        rpr = copy.deepcopy(base_rpr)
        for tag in ("w:rStyle", "w:color", "w:u"):
            for el in rpr.findall(qn(tag)):
                rpr.remove(el)
        r.append(rpr)
    hyperlink.append(r)
    paragraph._p.append(hyperlink)

    run = Run(r, paragraph)
    run.text = text
    # Google Docs link look: #1155CC + underline.  Set explicitly so it is blue
    # in Word / Docs / LibreOffice whether or not the doc defines "Hyperlink".
    run.font.color.rgb = GOOGLE_LINK_BLUE
    run.font.underline = True
    if any(s.style_id == "Hyperlink" for s in doc.styles):
        r.get_or_add_rPr().style = "Hyperlink"
    return hyperlink


def _write_link(doc, cell, url: str, text: str = "Link") -> list[str]:
    """Replace the cell's content with a blue hyperlink. Returns the old XML."""
    original = [_serialize(el) for el in _cell_blocks(cell)]

    paragraph = cell.paragraphs[0]
    # Keep the cell's own font (Arial 11 etc.) by borrowing the first run's props.
    base_rpr = None
    for r in paragraph._p.iter(qn("w:r")):
        rpr = r.find(qn("w:rPr"))
        if rpr is not None:
            base_rpr = rpr
            break

    # Clear: every block after the first paragraph, and everything in it but pPr.
    for el in _cell_blocks(cell)[1:]:
        cell._tc.remove(el)
    for el in list(paragraph._p):
        if el.tag != qn("w:pPr"):
            paragraph._p.remove(el)

    _make_hyperlink(doc, paragraph, url, text, base_rpr)
    return original


def link_session_in_document(doc, session_num: int, url: str, when: Optional[datetime]) -> LinkResult:
    cell, label, linked_cell = _find_row(doc, session_num, when)
    if cell is None and linked_cell is not None:
        return LinkResult("already_linked", f"session {session_num} ({label}) already has a link")
    if cell is None:
        return LinkResult("not_found", f"no row for session {session_num}")
    original = _write_link(doc, cell, url)
    return LinkResult("updated", f"session {session_num} ({label})", original)


# --------------------------------------------------------------------------- #
# Undo: put the Link cell back exactly as it was
# --------------------------------------------------------------------------- #
def _link_targets(doc) -> dict[str, str]:
    return {rid: rel.target_ref for rid, rel in doc.part.rels.items() if rel.is_external}


def revert_link_in_document(doc, url: str, original_cell_xml: list[str]) -> str:
    """
    Find the cell whose hyperlink points at `url` and restore its old content.
    Returns "reverted" or "not_found" (link already removed / edited away).
    """
    targets = _link_targets(doc)
    wanted = {rid for rid, t in targets.items() if t == url}
    if not wanted:
        return "not_found"

    rid_attr = qn("r:id")
    for tc in doc.element.body.iter(qn("w:tc")):
        links = [h for h in tc.iter(qn("w:hyperlink")) if h.get(rid_attr) in wanted]
        if not links:
            continue
        for el in [e for e in tc if e.tag != qn("w:tcPr")]:
            tc.remove(el)
        for xml in original_cell_xml:
            tc.append(parse_xml(xml))
        if not any(e.tag == qn("w:p") for e in tc):
            tc.append(OxmlElement("w:p"))
        for rid in wanted:  # drop the now-unused relationship
            try:
                doc.part.drop_rel(rid)
            except Exception:  # pragma: no cover - harmless leftovers
                pass
        return "reverted"
    return "not_found"


# --------------------------------------------------------------------------- #
# Drive wrappers
# --------------------------------------------------------------------------- #
def _download(drive, file_id: str) -> io.BytesIO:
    from googleapiclient.http import MediaIoBaseDownload

    # First, get file metadata to check if it's a Google Docs file
    metadata = drive.files().get(fileId=file_id, fields="mimeType", supportsAllDrives=True).execute()
    mime_type = metadata.get("mimeType", "")

    # Google Docs files need to be exported
    if mime_type.startswith("application/vnd.google-apps"):
        log.info("File is a Google Docs file (mimeType=%s), exporting as .docx", mime_type)
        # export() does not accept supportsAllDrives; shared-drive files still export if the caller has access.
        request = drive.files().export(fileId=file_id, mimeType=DOCX_MIME)
    else:
        # Binary file, download directly
        request = drive.files().get_media(fileId=file_id, supportsAllDrives=True)

    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    fh.seek(0)
    return fh


def _upload(drive, file_id: str, doc) -> None:
    from googleapiclient.http import MediaIoBaseUpload

    # Get file metadata to check if it's a Google Docs file
    metadata = drive.files().get(fileId=file_id, fields="mimeType", supportsAllDrives=True).execute()
    mime_type = metadata.get("mimeType", "")

    out = io.BytesIO()
    doc.save(out)
    out.seek(0)
    media = MediaIoBaseUpload(out, mimetype=DOCX_MIME, resumable=True)

    # If it's a Google Docs file, we need to convert it to a regular .docx file
    if mime_type.startswith("application/vnd.google-apps"):
        log.info("Converting Google Docs file to regular .docx")
        # Update with conversion - this will convert Google Docs to binary .docx
        drive.files().update(
            fileId=file_id,
            body={'mimeType': DOCX_MIME},
            media_body=media,
            supportsAllDrives=True,
            fields="id"
        ).execute()
    else:
        # Regular .docx file, just update
        drive.files().update(fileId=file_id, media_body=media, supportsAllDrives=True, fields="id").execute()


def update_docx_in_drive(drive, folder_id: str, rec, web_view_link: str, session_num: int) -> dict:
    """
    Add `web_view_link` to the row for `session_num` in the folder's .docx.

    Returns a dict:
      status             "updated" | "already_linked" | "not_found" | "skipped"
      detail             human readable
      file_id/file_name  the docx that was inspected / changed
      original_cell_xml  (only when status == "updated") for undo
    Raises ValueError only if the folder has no .docx at all.
    """
    log.info("update_docx_in_drive called: folder_id=%s, session_num=%d, link=%s", folder_id, session_num, web_view_link[:50] if web_view_link else "None")

    if not web_view_link:
        log.warning("No web_view_link provided, skipping docx update")
        return {"status": "skipped", "detail": "no link to insert"}

    # Search for .docx files by name (most reliable method)
    query = (f"'{folder_id}' in parents and name contains '.docx' and trashed = false")
    log.info("Searching for .docx files with query: %s", query)
    result = drive.files().list(
        q=query, fields="files(id, name, mimeType)", pageSize=20,
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute()
    files = result.get("files", [])

    log.info("Found %d .docx files in folder %s", len(files), folder_id)
    for f in files:
        log.info("  - %s (id: %s, mimeType: %s)", f.get("name"), f.get("id"), f.get("mimeType"))

    if not files:
        log.error("No .docx file found in folder %s", folder_id)
        raise ValueError(f"No .docx file found in folder {folder_id}.")

    when = rec.local_start()
    log.info("Recording start time: %s (local)", when)
    last: Optional[dict] = None
    for f in files:
        log.info("Checking docx: %s (id: %s)", f["name"], f["id"])
        try:
            doc = docx.Document(_download(drive, f["id"]))
            log.info("Successfully downloaded and parsed docx: %s", f["name"])
            result = link_session_in_document(doc, session_num, web_view_link, when)
            log.info("link_session_in_document result: status=%s, detail=%s", result.status, result.detail)
            info = {"status": result.status, "detail": result.detail,
                    "file_id": f["id"], "file_name": f["name"]}

            if result.status == "updated":
                log.info("Uploading modified docx back to Drive (%s)...", result.detail)
                _upload(drive, f["id"], doc)
                log.info("Successfully uploaded modified docx")
                info["original_cell_xml"] = result.original_cell_xml
                return info
            if result.status == "already_linked":
                log.info("Session already linked in docx: %s", f["name"])
                return info          # right row found, nothing to do
            last = info
        except Exception as exc:
            log.error("Error processing docx file %s: %s", f["name"], exc, exc_info=True)
            last = {"status": "error", "detail": f"error processing {f['name']}: {str(exc)}",
                    "file_id": f["id"], "file_name": f["name"]}

    assert last is not None
    names = ", ".join(f["name"] for f in files)
    last["detail"] = f"no row for session {session_num} in: {names}"
    log.warning("No matching row found for session %d in any docx file: %s", session_num, names)
    return last


def revert_docx_in_drive(drive, file_id: str, url: str, original_cell_xml: list[str]) -> str:
    """Undo `update_docx_in_drive`. Returns "reverted" or "not_found"."""
    doc = docx.Document(_download(drive, file_id))
    status = revert_link_in_document(doc, url, original_cell_xml)
    if status == "reverted":
        _upload(drive, file_id, doc)
    return status
