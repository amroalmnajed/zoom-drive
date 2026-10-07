with open('main.py', 'r', encoding='utf-8') as f:
    text = f.read()

old_loop = '''            folder = config.FOLDERS[decision.folder_key]  # type: ignore[index]
            if args.dry_run:
                print(f"  [dry-run] Would upload to: {folder['name']}")
                uploaded += 1
                continue

            print(f"  Uploading to: {folder['name']}")
            try:
                result = upload_recording(drive, zoom, rec, folder, session_num)'''

new_loop = '''            folder = config.FOLDERS[decision.folder_key]  # type: ignore[index]
            auto_s = get_next_session_number(drive, folder["id"])
            code = folder.get("code", "Course")
            
            # Date preview
            if rec.start_time:
                d_str = rec.start_time.astimezone(timezone.utc).strftime("%Y%m%d")
            else:
                d_str = datetime.now(timezone.utc).strftime("%Y%m%d")

            print(f"\n  Folder: {folder['name']} ({code})")
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

if old_loop in text:
    text = text.replace(old_loop, new_loop)
    with open('main.py', 'w', encoding='utf-8') as f:
        f.write(text)
    print("Interactive prompt updated successfully!")
else:
    print("Prompt loop already updated or pattern not found.")
