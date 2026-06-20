"""
One-time script to get a Gmail OAuth refresh token for Railway.
Run this locally (needs a browser). You only need to do this once.

Steps:
  1. Go to https://console.cloud.google.com and create a project (or use an existing one)
  2. Enable the Gmail API for that project
  3. Create OAuth 2.0 credentials → Desktop app → download the client ID and secret
  4. Install the extra auth library:  pip install google-auth-oauthlib
  5. Set env vars and run:
       $env:GOOGLE_CLIENT_ID="your-client-id"
       $env:GOOGLE_CLIENT_SECRET="your-client-secret"
       python auth_gmail.py
  6. A browser window will open — sign in and grant access
  7. Copy the printed GOOGLE_REFRESH_TOKEN value into Railway as an environment variable
"""

import os
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

flow = InstalledAppFlow.from_client_config(
    {
        "installed": {
            "client_id": os.environ["GOOGLE_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob", "http://localhost"],
        }
    },
    scopes=SCOPES,
)
creds = flow.run_local_server(port=0)

print("\n✅ Authentication successful!")
print(f"\nGOOGLE_REFRESH_TOKEN={creds.refresh_token}")
print("\nAdd that value to Railway → your reply-checker service → Variables.")
