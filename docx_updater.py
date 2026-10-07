import re
import io
import logging
import docx
from docx.opc.constants import RELATIONSHIP_TYPE
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

log = logging.getLogger("zoom_to_drive")

def add_hyperlink(paragraph, url, text):
    part = paragraph.part
    r_id = part.relate_to(url, RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
    hyperlink = docx.oxml.shared.OxmlElement('w:hyperlink')
    hyperlink.set(docx.oxml.shared.qn('r:id'), r_id)
    new_run = docx.oxml.shared.OxmlElement('w:r')
    rPr = docx.oxml.shared.OxmlElement('w:rPr')
    rStyle = docx.oxml.shared.OxmlElement('w:rStyle')
    rStyle.set(docx.oxml.shared.qn('w:val'), 'Hyperlink')
    rPr.append(rStyle)
    new_run.append(rPr)
    new_run.text = text
    hyperlink.append(new_run)
    paragraph._p.append(hyperlink)
    return hyperlink

def update_docx_in_drive(drive, folder_id, rec, web_view_link, session_num):
    if not web_view_link:
        return
        
    query = f"'{folder_id}' in parents and mimeType = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' and trashed = false"
    result = drive.files().list(q=query, fields="files(id, name)", pageSize=1, supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
    files = result.get("files", [])
    if not files:
        raise ValueError("No .docx file found in the folder.")
        
    file_id = files[0]["id"]
    log.info("Downloading docx: %s", files[0]['name'])
    
    request = drive.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        status, done = downloader.next_chunk()
        
    fh.seek(0)
    doc = docx.Document(fh)
    
    if not doc.tables:
        raise ValueError("No tables found in the document.")
        
    table = doc.tables[0]
    if not table.rows:
        raise ValueError("Table is empty.")
        
    headers = [c.text.strip().lower() for c in table.rows[0].cells]
    
    link_col = -1
    for i, h in enumerate(headers):
        if "link" in h:
            link_col = i
            break
            
    if link_col == -1:
        raise ValueError("No 'Link' column found in the table.")
        
    date_col = headers.index("date") if "date" in headers else -1
    session_col = headers.index("session #") if "session #" in headers else -1

    rec_date = rec.local_start()
    updated = False
    
    for row in table.rows[1:]:
        if len(row.cells) <= link_col:
            continue
            
        is_match = False
        
        # 1. Match by session number (most reliable)
        if session_col != -1 and row.cells[session_col].text.strip() == str(session_num):
            is_match = True
        # 2. Fallback: match by date
        elif date_col != -1 and rec_date:
            date_text = row.cells[date_col].text.strip().lower()
            m_str, d_str = str(rec_date.month), str(rec_date.day)
            m_pad, d_pad = f"{rec_date.month:02d}", f"{rec_date.day:02d}"
            
            if f"{m_pad}-{d_pad}" in date_text or f"{m_str}-{d_str}" in date_text or \
               f"{m_pad}/{d_pad}" in date_text or f"{m_str}/{d_str}" in date_text:
                is_match = True
            elif re.search(rf'\b{m_str}\b', date_text) and re.search(rf'\b{d_str}\b', date_text):
                is_match = True
                
        if is_match:
            cell = row.cells[link_col]
            
            # Check if it already has a link
            has_link = False
            for p in cell.paragraphs:
                for child in p._p:
                    if child.tag.endswith('}hyperlink'):
                        has_link = True
                        break
                if has_link: break
                
            if not has_link:
                cell.text = "" 
                p = cell.paragraphs[0] if cell.paragraphs else cell.add_paragraph()
                add_hyperlink(p, web_view_link, "Link")
                updated = True
            break

    if not updated:
        raise ValueError("Matching row not found or it already had a link.")
        
    log.info("Uploading modified docx back to Drive...")
    out_fh = io.BytesIO()
    doc.save(out_fh)
    out_fh.seek(0)
    
    media = MediaIoBaseUpload(out_fh, mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document', resumable=True)
    drive.files().update(fileId=file_id, media_body=media, supportsAllDrives=True).execute()
    log.info("Docx update successful.")
