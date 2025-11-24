#!/usr/bin/env python3
"""
Test script to verify Pub/Sub permissions for Gmail watch
This will help diagnose why permissions aren't working
"""

import os
import sys
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import json

# Test credentials (you'll need to provide these)
# This should be the same credentials from your OAuth flow
TEST_CREDENTIALS = {
    'token': 'ya29.a0ATi6K2tF0Rp9NhteRPq5emV8NjjCCfdcFl8p9tuZv9iE3MbvQQrBvUOdpMx3JzGj-4jgwSUSPdsMhgE_GM9t6vCQcp3lbJXXbO5lKut_4EQhv1L-DQiCaNdq3HhdRFZ0zbvX0qdQdp5xVqRsmvdKJynCBLg7zrYwUe3j-6xoevj8C6izU50TTeQn527m8K4bPzpLsiYaCgYKATASARMSFQHGX2MiRb9rgtPlcUo6RXPUCrwTXQ0206',
    'refresh_token': '1//0gEqCxMkPLuywCgYIARAAGBASNwF-L9IrFwc8HC6VxKNRz3n8_ZAubJvOKNEfKGv5IMAowu9qAjCGv8TnQp9BgTQ1BoNGqIeeOqo',
    'token_uri': 'https://oauth2.googleapis.com/token',
    'client_id': '37019452145-gfmdneb9o2vps2sap5mlnbv07u4scc3t.apps.googleusercontent.com',
    'client_secret': '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',
    'scopes': [
        'https://www.googleapis.com/auth/gmail.readonly',
        'https://www.googleapis.com/auth/gmail.modify',
        'https://www.googleapis.com/auth/drive.file'
    ]
}

PROJECT_ID = "automationspecialist"
TOPIC_NAME = "projects/automationspecialist/topics/gmail-notifs"

def test_gmail_watch():
    """Test Gmail watch setup with detailed error reporting"""
    
    print("=" * 80)
    print("Testing Gmail Watch Setup")
    print("=" * 80)
    print(f"Project ID: {PROJECT_ID}")
    print(f"Topic: {TOPIC_NAME}")
    print()
    
    # Create credentials
    print("1. Creating credentials...")
    try:
        creds = Credentials(
            token=TEST_CREDENTIALS['token'],
            refresh_token=TEST_CREDENTIALS['refresh_token'],
            token_uri=TEST_CREDENTIALS['token_uri'],
            client_id=TEST_CREDENTIALS['client_id'],
            client_secret=TEST_CREDENTIALS['client_secret'],
            scopes=TEST_CREDENTIALS['scopes']
        )
        
        # Refresh if needed
        if creds.expired:
            print("   Credentials expired, refreshing...")
            creds.refresh(None)
            print("   ✅ Credentials refreshed")
        else:
            print("   ✅ Credentials valid")
    except Exception as e:
        print(f"   ❌ Error creating credentials: {e}")
        return
    
    # Build Gmail service
    print("\n2. Building Gmail service...")
    try:
        service = build('gmail', 'v1', credentials=creds)
        print("   ✅ Gmail service built")
    except Exception as e:
        print(f"   ❌ Error building service: {e}")
        return
    
    # Get authenticated user's email
    print("\n3. Getting authenticated user's email...")
    try:
        profile = service.users().getProfile(userId='me').execute()
        email = profile.get('emailAddress')
        print(f"   ✅ Authenticated as: {email}")
    except Exception as e:
        print(f"   ❌ Error getting profile: {e}")
        return
    
    # Try to set up watch
    print("\n4. Attempting to set up Gmail watch...")
    print(f"   Topic: {TOPIC_NAME}")
    print(f"   Email: {email}")
    print()
    
    request_body = {
        'labelIds': ['INBOX'],
        'topicName': TOPIC_NAME
    }
    
    try:
        response = service.users().watch(userId='me', body=request_body).execute()
        print("   ✅ SUCCESS! Gmail watch set up successfully!")
        print(f"   Response: {json.dumps(response, indent=2)}")
        return True
    except HttpError as e:
        print(f"   ❌ FAILED with HttpError")
        print(f"   Status: {e.resp.status}")
        print(f"   Error details: {e.error_details}")
        print()
        print("   🔍 DIAGNOSIS:")
        print(f"   - The email '{email}' needs 'Pub/Sub Publisher' permission")
        print(f"   - Permission must be at BOTH topic and project levels")
        print(f"   - Even if permissions are set, they may take 10-15 minutes to propagate")
        print()
        print("   📋 CHECKLIST:")
        print(f"   [ ] Verify '{email}' has 'Pub/Sub Publisher' at topic: {TOPIC_NAME}")
        print(f"   [ ] Verify '{email}' has 'Pub/Sub Publisher' at project: {PROJECT_ID}")
        print(f"   [ ] Wait 10-15 minutes after granting permissions")
        print(f"   [ ] Check Pub/Sub API is enabled")
        print(f"   [ ] Check Gmail API is enabled")
        return False
    except Exception as e:
        print(f"   ❌ FAILED with unexpected error: {e}")
        import traceback
        print(traceback.format_exc())
        return False

if __name__ == "__main__":
    print("\n⚠️  NOTE: This script uses hardcoded credentials from your error log.")
    print("   For security, update TEST_CREDENTIALS with fresh credentials.\n")
    
    success = test_gmail_watch()
    
    if not success:
        print("\n" + "=" * 80)
        print("TROUBLESHOOTING STEPS:")
        print("=" * 80)
        print("1. Verify permissions are set correctly:")
        print("   - Topic: https://console.cloud.google.com/cloudpubsub/topic/detail/gmail-notifs?project=automationspecialist")
        print("   - Project: https://console.cloud.google.com/iam-admin/iam?project=automationspecialist")
        print()
        print("2. Wait 10-15 minutes for permission propagation")
        print()
        print("3. Try removing and re-adding the permission (sometimes helps)")
        print()
        print("4. Check if you need to re-authenticate (get new OAuth tokens)")
        print()
        print("5. Verify the OAuth client has correct scopes")
        print()
        sys.exit(1)
    else:
        print("\n✅ All tests passed!")
        sys.exit(0)


