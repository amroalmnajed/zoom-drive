import io, sys, zipfile
from datetime import datetime
sys.path.insert(0, "/home/claude/zd/zoom-drive")
import docx
from docx_updater import (link_session_in_document, revert_link_in_document, _cell_has_link)

URL = "https://drive.google.com/file/d/ABC/view"
OCT10 = datetime(2026, 10, 10, 9, 0)

def add_link(cell, url, text="Link"):
    # simulate an already-linked cell (like the user's blue links)
    link_session_in_document  # noqa
    from docx_updater import _write_link
    _write_link(cell.part.document if hasattr(cell.part,'document') else None, cell, url, text) if False else None

def build_main():
    d = docx.Document()
    d.add_paragraph("Main Revision Records")
    t = d.add_table(rows=1, cols=5)
    for c, h in zip(t.rows[0].cells, ["Session #", "Date", "Day", "Content", "Link"]):
        c.text = h
    rows = [(40,"4/10/2026","Sunday"),(41,"5/10/2026","Monday"),(42,"6/10/2026","Tuesday"),
            (43,"10/10/2026","Saturday"),(44,"11/10/2026","Sunday"),(45,"12/10/2026","Monday"),
            (47,"18/10/2026","")]
    for s, dt, day in rows:
        cells = t.add_row().cells
        cells[0].text, cells[1].text, cells[2].text, cells[4].text = str(s), dt, day, "Link"
    return d, t

def build_crash():
    d = docx.Document()
    d.add_paragraph("Crash Course Records")
    t1 = d.add_table(rows=1, cols=5)
    for c, h in zip(t1.rows[0].cells, ["Session #","Date","Day","Content","Link"]): c.text = h
    for s, dt, day in [(21,"10-3","Saturday"),(25,"10-7","Wednesday")]:
        r = t1.add_row().cells; r[0].text, r[1].text, r[2].text, r[4].text = str(s), dt, day, "link"
    d.add_page_break()
    d.add_paragraph("Crash Course Records")
    t2 = d.add_table(rows=0, cols=5)          # NO header row
    for s, dt, day in [(26,"10-10","Saturday"),(27,"10-11","Sunday"),(28,"10-12","Monday")]:
        r = t2.add_row().cells; r[0].text, r[1].text, r[2].text, r[4].text = str(s), dt, day, "link"
    return d, t1, t2

def roundtrip(d):
    b = io.BytesIO(); d.save(b); b.seek(0); return docx.Document(b)

def cell_for(d, ti, ri, ci): return d.tables[ti].rows[ri].cells[ci]

# ---- 1. Main S43 (the old bug: row 40 "4/10/2026" matched Oct 10 by date) ----
d, t = build_main()
# pre-link rows 40..42 like in the screenshot
for ri, s in ((1,40),(2,41),(3,42)):
    r = link_session_in_document(d, s, f"https://x/{s}", None); assert r.status == "updated"
r = link_session_in_document(d, 43, URL, OCT10)
assert r.status == "updated", r
d = roundtrip(d)
assert _cell_has_link(cell_for(d,0,4,4)) and cell_for(d,0,4,4).text == "Link"
assert not _cell_has_link(cell_for(d,0,5,4))          # S44 untouched
print("1 ok: Main S43 -> row 43 (old code hit row 40 and failed)")

# ---- 2. already linked -> status, not an exception ----
r = link_session_in_document(d, 43, URL+"2", OCT10)
assert r.status == "already_linked", r
print("2 ok: already linked reported cleanly")

# ---- 3. Crash S26 lives in the 2nd, header-less table ----
d, t1, t2 = build_crash()
r = link_session_in_document(d, 26, URL, OCT10)
assert r.status == "updated" and "table 2, row 1" in r.detail, r
d = roundtrip(d)
assert _cell_has_link(cell_for(d,1,0,4)) and not _cell_has_link(cell_for(d,1,1,4))
print("3 ok: Crash S26 found in header-less table 2 (first row not skipped as header)")

# ---- 4. unknown session -> not_found ----
r = link_session_in_document(d, 99, URL, OCT10); assert r.status == "not_found"
print("4 ok: unknown session -> not_found")

# ---- 5. date fallback only for rows with no session number (row 47-49 style) ----
d, t = build_main()
t.rows[7].cells[0].text = ""               # blank session cell, date 18/10/2026
r = link_session_in_document(d, 50, URL, datetime(2026,10,18,9,0))
assert r.status == "updated", r
print("5 ok: date fallback works for blank-session row")
# ...but never for a row with a different session number
d, t = build_main()
r = link_session_in_document(d, 99, URL, datetime(2026,10,18,9,0))
assert r.status == "not_found", r
print("5b ok: date does not override a different session number")

# ---- 6. link look: Google blue + underline, font kept ----
d, t = build_main()
cell = t.rows[4].cells[4]
rp = cell.paragraphs[0].runs[0]._r.get_or_add_rPr(); 
cell.paragraphs[0].runs[0].font.name = "Arial"; cell.paragraphs[0].runs[0].font.size = docx.shared.Pt(11)
link_session_in_document(d, 43, URL, OCT10)
d = roundtrip(d)
xml = cell_for(d,0,4,4)._tc.xml
assert 'w:val="1155CC"' in xml and '<w:u w:val="single"/>' in xml and 'Arial' in xml and 'w:hyperlink' in xml, xml
print("6 ok: #1155CC + underline + Arial kept")

# ---- 7. revert restores the exact original cell ----
d, t = build_main()
before = cell_for(d,0,4,4)._tc.xml
r = link_session_in_document(d, 43, URL, OCT10)
d = roundtrip(d)
assert revert_link_in_document(d, URL, r.original_cell_xml) == "reverted"
d = roundtrip(d)
after = cell_for(d,0,4,4)._tc.xml
assert not _cell_has_link(cell_for(d,0,4,4)) and cell_for(d,0,4,4).text == "Link"
assert URL not in {rel.target_ref for rel in d.part.rels.values()}
assert revert_link_in_document(d, URL, r.original_cell_xml) == "not_found"
print("7 ok: undo restores cell, drops relationship, 2nd undo is a no-op")
print("ALL PASSED")
