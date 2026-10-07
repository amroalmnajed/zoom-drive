import config
from drive_api import get_drive_service

def authenticate_all():
    print("--- Multi-Account Authenticator ---")
    print("This will prompt you to log into Google for each class folder.")
    print("Make sure you select the correct email for each prompt:\n")
    
    # Show user the emails to prepare them
    emails = {
        "Main-Revision": "maincombined26@gmail.com",
        "Combine Crash": "Yamanigcse2027@gmail.com",
        "IGCSE": "igcsechem2027@gmail.com",
        "Pre-igcse": "chemcore2027@gmail.com"
    }
    
    for key, folder in config.FOLDERS.items():
        expected_email = emails.get(folder["name"], "Unknown")
        print(f"\n> Authenticating for folder: {folder['name']}")
        print(f"  Expected Email: {expected_email}")
        
        # get_drive_service automatically triggers the OAuth flow if the token is missing
        try:
            get_drive_service(folder)
            print(f"  Success! Saved token for {folder['name']}")
        except Exception as e:
            print(f"  Error authenticating {folder['name']}: {e}")

if __name__ == "__main__":
    authenticate_all()
