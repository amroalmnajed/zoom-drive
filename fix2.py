with open('main.py', 'r', encoding='utf-8') as f:
    text = f.read()

old_import = "import httplib2"
new_import = """import httplib2
httplib2.REDIRECT_CODES = {301, 302, 303, 307}"""

if old_import in text and "REDIRECT_CODES" not in text:
    text = text.replace(old_import, new_import)
    with open('main.py', 'w', encoding='utf-8') as f:
        f.write(text)
    print("Successfully patched Google Drive uploader!")
else:
    print("Already patched or import line not found.")
