import os
from pathlib import Path

# 1. Reset processed.json so the script offers the video again
state_file = Path("processed.json")
if state_file.exists():
    state_file.unlink()
    print("Reset processed.json (ready to re-test).")

# 2. Complete main.py loop update
with open('main.py', 'r', encoding='utf-8') as f:
    code = f.read()

target = "folder = config.FOLDERS[decision.folder_key]"

replacement = '''folder = config.FOLDERS[decision.folder_key]
            auto_s = get_next_session_number(drive, folder["id"])
            code = folder.get("code", "Course")
            
            if rec.start_time:
                d_str = rec.start_time.astimezone(timezone.utc).strftime("%Y%m%d")
            else:
                d_str = datetime.now(timezone.utc).strftime("%Y%m%d")

            print(f"\\n  Folder: {folder['name']} ({code})")
            print(f"  Auto-detected next session: S{auto_s}")
            print(f"  Filename preview: GMT{d_str}_{code}_S{auto_s}.mp4")
            
            s_input = input(f"  Enter session number [Press Enter for S{auto_s}]: ").strip()
            if s_input.lower().startswith("s"):
                s_input = s_input[1:]
            session_num = int(s_input) if s_input.isdigit() else auto_s

            final_name = build_drive_filename(rec, code, session_num)
            print(f"  Target File: {final_name}")

            if args.dry_run:
                print(f"  [dry-run] Would upload {final_name} to {folder['name']}")
                uploaded += 1
                continue

            print(f"  Uploading {final_name} to: {folder['name']}...")
            try:
                result = upload_recording(drive, zoom, rec, folder, session_num)'''

if target in code and "auto_s = get_next_session_number" not in code:
    # Find start of target up to the upload call
    start_idx = code.find(target)
    end_str = "result = upload_recording(drive, zoom, rec, folder, session_num)"
    end_idx = code.find(end_str, start_idx) + len(end_str)
    
    code = code[:start_idx] + replacement + code[end_idx:]
    with open('main.py', 'w', encoding='utf-8') as f:
        f.write(code)
    print("Successfully updated main.py with the interactive session prompt!")
else:
    print("main.py is already updated!")
