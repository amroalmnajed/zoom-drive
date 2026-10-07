import re

# 1. Update config.py with course codes
with open('config.py', 'r', encoding='utf-8') as f:
    config_text = f.read()

config_replacements = [
    ('"name": "Main-Revision",', '"name": "Main-Revision",\n        "code": "Main",'),
    ('"name": "Combine Crash",', '"name": "Combine Crash",\n        "code": "Crash",'),
    ('"name": "IGCSE",', '"name": "IGCSE",\n        "code": "G10",'),
    ('"name": "Pre-igcse",', '"name": "Pre-igcse",\n        "code": "G9",'),
]

for old, new in config_replacements:
    if old in config_text and new not in config_text:
        config_text = config_text.replace(old, new)

with open('config.py', 'w', encoding='utf-8') as f:
    f.write(config_text)

print("Updated config.py with Course Codes.")

# 2. Update main.py logic
with open('main.py', 'r', encoding='utf-8') as f:
    main_text = f.read()

# Add get_next_session_number function
session_func = '''
def get_next_session_number(drive, folder_id: str) -> int:
    """Find highest _S<number>.mp4 in the folder and return highest + 1."""
    if not drive:
        return 1
    query = f"'{folder_id}' in parents and trashed = false"
    try:
        result = drive.files().list(
            q=query,
            fields="files(name)",
            pageSize=1000,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute(num_retries=3)
        numbers = []
        for f in result.get("files", []):
            match = re.search(r"_S(\d+)\.mp4$", f.get("name", ""), re.IGNORECASE)
            if match:
                numbers.append(int(match.group(1)))
        return max(numbers, default=0) + 1
    except Exception:
        return 1
'''

if "def get_next_session_number" not in main_text:
    main_text = main_text.replace("def build_drive_filename", session_func + "\ndef build_drive_filename")

# Replace build_drive_filename implementation
old_build = '''def build_drive_filename(rec: Recording) -> str:
    start = rec.local_start()
    stamp = start.strftime("%Y-%m-%d %H-%M") if start else "unknown-date"
    return f"{stamp} - {safe_filename(rec.topic)} ({rec.meeting_id}).mp4"'''

new_build = '''def build_drive_filename(rec: Recording, course_code: str, session_num: int) -> str:
    if rec.start_time:
        date_str = rec.start_time.astimezone(timezone.utc).strftime("%Y%m%d")
    else:
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"GMT{date_str}_{course_code}_S{session_num}.mp4"'''

if old_build in main_text:
    main_text = main_text.replace(old_build, new_build)

# Update upload call inside main loop
old_upload_call = "result = upload_recording(drive, zoom, rec, folder['id'])"
new_upload_call = "result = upload_recording(drive, zoom, rec, folder, session_num)"

if old_upload_call in main_text:
    main_text = main_text.replace(old_upload_call, new_upload_call)

old_upload_def = "def upload_recording(drive, zoom: ZoomClient, rec: Recording, folder_id: str) -> Optional[dict]:"
new_upload_def = "def upload_recording(drive, zoom: ZoomClient, rec: Recording, folder: dict, session_num: int) -> Optional[dict]:"

if old_upload_def in main_text:
    main_text = main_text.replace(old_upload_def, new_upload_def)

old_name_call = "name = build_drive_filename(rec)"
new_name_call = "name = build_drive_filename(rec, folder.get('code', 'Course'), session_num)\n    folder_id = folder['id']"

if old_name_call in main_text:
    main_text = main_text.replace(old_name_call, new_name_call)

with open('main.py', 'w', encoding='utf-8') as f:
    f.write(main_text)

print("Updated main.py with naming logic.")
