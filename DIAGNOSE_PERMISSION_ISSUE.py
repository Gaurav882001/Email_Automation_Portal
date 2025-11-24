#!/usr/bin/env python3
"""
Comprehensive diagnostic script for Gmail API Pub/Sub permission issues
This will help identify the exact problem
"""

import os
import sys
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.auth.transport.requests import Request
import json

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

def check_environment():
    """Check if all required environment variables are set"""
    print("=" * 80)
    print("1. CHECKING ENVIRONMENT VARIABLES")
    print("=" * 80)
    
    required_vars = {
        'GOOGLE_CLOUD_PROJECT_ID': os.getenv('GOOGLE_CLOUD_PROJECT_ID'),
        'GMAIL_PUBSUB_TOPIC': os.getenv('GMAIL_PUBSUB_TOPIC'),
        'GOOGLE_CLIENT_ID': os.getenv('GOOGLE_CLIENT_ID'),
        'GOOGLE_CLIENT_SECRET': os.getenv('GOOGLE_CLIENT_SECRET'),
    }
    
    all_set = True
    for var, value in required_vars.items():
        if value:
            print(f"   ✅ {var}: {value}")
        else:
            print(f"   ❌ {var}: NOT SET")
            all_set = False
    
    if not all_set:
        print("\n   ⚠️  Some environment variables are missing!")
        print("   Please check your .env file.")
        return False
    
    return True

def test_gmail_api_access(credentials_dict):
    """Test if we can access Gmail API"""
    print("\n" + "=" * 80)
    print("2. TESTING GMAIL API ACCESS")
    print("=" * 80)
    
    try:
        creds = Credentials.from_authorized_user_info(credentials_dict)
        
        if creds.expired and creds.refresh_token:
            print("   Credentials expired, refreshing...")
            creds.refresh(Request())
            print("   ✅ Credentials refreshed")
        
        service = build('gmail', 'v1', credentials=creds)
        profile = service.users().getProfile(userId='me').execute()
        email = profile.get('emailAddress', '')
        
        print(f"   ✅ Gmail API access successful")
        print(f"   ✅ Authenticated as: {email}")
        return email, service
    except Exception as e:
        print(f"   ❌ Gmail API access failed: {e}")
        return None, None

def test_pubsub_permission(service, email, project_id, topic_name):
    """Test Pub/Sub permission by attempting Gmail watch"""
    print("\n" + "=" * 80)
    print("3. TESTING PUB/SUB PERMISSION")
    print("=" * 80)
    
    print(f"   Email: {email}")
    print(f"   Project ID: {project_id}")
    print(f"   Topic: {topic_name}")
    print()
    
    request_body = {
        'labelIds': ['INBOX'],
        'topicName': topic_name
    }
    
    try:
        print("   Attempting to set up Gmail watch...")
        response = service.users().watch(userId='me', body=request_body).execute()
        print("   ✅ SUCCESS! Gmail watch set up successfully!")
        print(f"   Response: {json.dumps(response, indent=2)}")
        return True
    except HttpError as e:
        print(f"   ❌ FAILED with HttpError")
        print(f"   Status Code: {e.resp.status}")
        print(f"   Error Details: {e.error_details}")
        print()
        
        # Detailed analysis
        if e.resp.status == 403:
            print("   🔍 ANALYSIS:")
            print("   This is a 403 Forbidden error, which means:")
            print("   1. Gmail API is accessible (we got this far)")
            print("   2. But Pub/Sub permission is missing or not propagated")
            print()
            print("   📋 CHECKLIST:")
            print(f"   [ ] Verify '{email}' has 'Pub/Sub Publisher' at topic level")
            print(f"       → https://console.cloud.google.com/cloudpubsub/topic/detail/gmail-notifs?project={project_id}")
            print(f"   [ ] Verify '{email}' has 'Pub/Sub Publisher' at project level")
            print(f"       → https://console.cloud.google.com/iam-admin/iam?project={project_id}")
            print(f"   [ ] Wait 15-30 minutes after granting permissions")
            print(f"   [ ] Re-authenticate (get new OAuth tokens) after permissions are set")
            print(f"   [ ] Check Pub/Sub API is enabled")
            print(f"       → https://console.cloud.google.com/apis/library/pubsub.googleapis.com?project={project_id}")
            print(f"   [ ] Check Gmail API is enabled")
            print(f"       → https://console.cloud.google.com/apis/library/gmail.googleapis.com?project={project_id}")
            print()
            print("   💡 COMMON ISSUES:")
            print("   • Permissions set but not propagated (wait 15-30 minutes)")
            print("   • OAuth tokens obtained before permissions were set (re-authenticate)")
            print("   • Project ID case mismatch (check exact project ID)")
            print("   • APIs not enabled")
        
        return False
    except Exception as e:
        print(f"   ❌ FAILED with unexpected error: {e}")
        import traceback
        print(traceback.format_exc())
        return False

def check_project_id_case(project_id):
    """Check if project ID case might be an issue"""
    print("\n" + "=" * 80)
    print("4. CHECKING PROJECT ID")
    print("=" * 80)
    
    print(f"   Current Project ID: {project_id}")
    print(f"   Lowercase: {project_id.lower()}")
    print(f"   Uppercase: {project_id.upper()}")
    print()
    print("   ⚠️  NOTE: Google Cloud Project IDs are case-insensitive,")
    print("      but make sure you're using the exact ID from Google Cloud Console")
    print()
    print("   Check your project ID here:")
    print(f"   → https://console.cloud.google.com/home/dashboard?project={project_id}")

def main():
    """Main diagnostic function"""
    print("\n" + "=" * 80)
    print("GMAIL API PUB/SUB PERMISSION DIAGNOSTIC")
    print("=" * 80)
    print()
    
    # Check environment
    if not check_environment():
        print("\n❌ Environment check failed. Please fix environment variables first.")
        sys.exit(1)
    
    project_id = os.getenv('GOOGLE_CLOUD_PROJECT_ID')
    topic_name = os.getenv('GMAIL_PUBSUB_TOPIC')
    
    if not topic_name.startswith('projects/'):
        topic_name = f'projects/{project_id}/topics/{topic_name}'
    
    # Test credentials (you'll need to provide these)
    print("\n" + "=" * 80)
    print("⚠️  CREDENTIALS REQUIRED")
    print("=" * 80)
    print("To run this diagnostic, you need to provide OAuth credentials.")
    print("These should be the same credentials from your OAuth flow.")
    print()
    print("You can get them from:")
    print("1. Browser console (after OAuth login)")
    print("2. Backend logs (when you click 'Automate')")
    print()
    
    # For now, just check environment and provide instructions
    check_project_id_case(project_id)
    
    print("\n" + "=" * 80)
    print("NEXT STEPS")
    print("=" * 80)
    print("1. Run this script with actual credentials to test")
    print("2. Or manually check the items in the checklist above")
    print("3. Most common fix: Wait 15-30 minutes + Re-authenticate")
    print()

if __name__ == "__main__":
    main()

