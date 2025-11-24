import os
import json
import base64
import re
import traceback
import html
from datetime import datetime, timedelta
from io import BytesIO
from django.utils import timezone
from rest_framework.views import APIView
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from django.http import HttpResponse, JsonResponse
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload
from utils.decorators import user_token_auth
from utils.response import ResponseView
from utils.constant import SUCCESS, FAIL
from image_gen.db_models.user import Users
from image_gen.models import EmailAccount, ProcessedEmail


class EmailAutomationView(APIView):
    """Set up Gmail watch for email automation"""
    
    @user_token_auth
    def post(self, request):
        print("=" * 80)
        print("EMAIL AUTOMATION SETUP - START")
        print("=" * 80)
        print(f"Request method: {request.method}")
        print(f"Request path: {request.path}")
        print(f"Request headers: {dict(request.headers)}")
        print(f"Has auth_user: {hasattr(request, 'auth_user')}")
        if hasattr(request, 'auth_user'):
            print(f"Auth user: {request.auth_user.email if request.auth_user else 'None'}")
        try:
            user = request.auth_user
            print(f"User: {user.email if user else 'None'}")
            print(f"Request data type: {type(request.data)}")
            print(f"Request data: {request.data}")
            
            data = request.data
            print(f"Request data keys: {list(data.keys()) if isinstance(data, dict) else 'Not a dict'}")
            
            email = data.get('email')
            credentials_json = data.get('credentials')  # OAuth credentials from frontend
            
            # Debug logging
            try:
                print(f"Received email: {email}")
                print(f"Credentials type: {type(credentials_json)}")
                if isinstance(credentials_json, dict):
                    print(f"Credentials keys: {list(credentials_json.keys())}")
                    # Don't print sensitive data, just keys
                else:
                    print(f"Credentials is not a dict: {credentials_json}")
            except Exception as debug_error:
                print(f"Debug logging error: {debug_error}")
                print(traceback.format_exc())
            
            if not email or not credentials_json:
                return ResponseView.error_response_without_data(
                    message="Email and credentials are required",
                    code=FAIL
                )
            
            # Check if credentials contain Google Sign-In credential (not OAuth2 tokens)
            # This check must happen BEFORE any database operations
            if isinstance(credentials_json, dict) and 'credential' in credentials_json:
                return ResponseView.error_response_without_data(
                    message="Google Sign-In credential detected. Please use OAuth2 flow with Gmail API scopes. The current implementation requires OAuth2 access_token and refresh_token.",
                    code=FAIL
                )
            
            # Validate credentials format
            if not isinstance(credentials_json, dict):
                return ResponseView.error_response_without_data(
                    message="Invalid credentials format. Expected a dictionary with OAuth2 tokens.",
                    code=FAIL
                )
            
            # Check if credentials have required OAuth2 fields
            # Note: 'token' might be 'access_token' in some OAuth implementations
            required_fields = ['token', 'refresh_token', 'token_uri', 'client_id', 'client_secret']
            # Also check for 'access_token' as alternative to 'token'
            if 'access_token' in credentials_json and 'token' not in credentials_json:
                credentials_json['token'] = credentials_json['access_token']
            
            missing_fields = [field for field in required_fields if field not in credentials_json]
            if missing_fields:
                return ResponseView.error_response_without_data(
                    message=f"Missing required OAuth2 credential fields: {', '.join(missing_fields)}. Please use OAuth2 flow to get proper tokens.",
                    code=FAIL
                )
            
            # Check if account already exists
            try:
                email_account, created = EmailAccount.objects.get_or_create(
                    user=user,
                    email=email,
                    defaults={
                        'credentials': credentials_json,
                        'is_active': True
                    }
                )
                
                if not created:
                    # Update credentials if account exists
                    email_account.credentials = credentials_json
                    email_account.is_active = True
                    email_account.save()
            except Exception as db_error:
                print(f"Database error: {db_error}")
                print(f"Traceback: {traceback.format_exc()}")
                return ResponseView.error_response_without_data(
                    message=f"Database error: {str(db_error)}",
                    code=FAIL
                )
            
            # Set up Gmail watch
            try:
                print(f"Setting up Gmail watch for {email}...")
                watch_result = self._setup_gmail_watch(email, credentials_json)
                print(f"Gmail watch setup successful: {watch_result}")
                
                # Store the OLD history ID before updating (to process emails since last check)
                old_history_id = email_account.watch_history_id
                
                # Update with NEW history ID from watch response
                email_account.watch_history_id = watch_result.get('historyId')
                email_account.watch_expiration = timezone.now() + timedelta(days=7)
                email_account.is_automated = True
                email_account.save()
                
                # Process existing emails immediately after setting up watch
                # Use the OLD history ID to get emails that arrived since last processing
                # This ensures we process emails that arrived between the last check and now
                print(f"\n🔄 Processing existing emails immediately...")
                print(f"   Old history ID: {old_history_id}")
                print(f"   New history ID: {email_account.watch_history_id}")
                
                try:
                    service = EmailAutomationService()
                    # Use OLD history ID to get emails since last processing
                    # If old_history_id is None, it will process all unread emails
                    service.process_new_emails(
                        email_account,
                        old_history_id  # Use OLD history ID, not the new one!
                    )
                    print(f"✅ Immediate email processing completed")
                except Exception as process_error:
                    # Don't fail the whole setup if immediate processing fails
                    print(f"⚠️ Immediate email processing failed (but watch is set up): {process_error}")
                    print(f"   Traceback: {traceback.format_exc()}")
                    print(f"   Emails will still be processed via webhook when new emails arrive")
                
                # Reload account to get updated history ID after processing
                email_account.refresh_from_db()
                
                return ResponseView.success_response_data(
                    data={
                        'email': email,
                        'watch_history_id': email_account.watch_history_id,
                        'expiration': email_account.watch_expiration.isoformat()
                    },
                    message="Email automation set up successfully. Existing emails processed.",
                    code=SUCCESS
                )
            except ValueError as e:
                error_msg = str(e)
                print(f"Gmail watch setup ValueError: {error_msg}")
                print(f"Traceback: {traceback.format_exc()}")
                return ResponseView.error_response_without_data(
                    message=error_msg,
                    code=FAIL
                )
            except HttpError as e:
                error_details = e.error_details if hasattr(e, 'error_details') else str(e)
                error_code = e.resp.status if hasattr(e, 'resp') else 'Unknown'
                
                # Check for specific permission errors
                error_msg = str(error_details)
                if 'not authorized' in error_msg.lower() or 'forbidden' in error_msg.lower() or error_code == 403:
                    # Use the email from the request (this is the authenticated Gmail account)
                    authenticated_email = email if email else "the authenticated user"
                    
                    # Get project ID from env for the error message
                    project_id_for_error = os.getenv('GOOGLE_CLOUD_PROJECT_ID', 'automationspecialist')
                    project_number_for_error = os.getenv('GOOGLE_CLOUD_PROJECT_NUMBER', '37019452145')
                    gmail_service_account_for_error = f"service-{project_number_for_error}@gcp-sa-gmail.iam.gserviceaccount.com"
                    
                    detailed_msg = (
                        f"⚠️ Gmail API Permission Error (403 Forbidden)\n\n"
                        f"❌ Gmail watch setup failed due to missing Pub/Sub permissions.\n\n"
                        f"🔑 CRITICAL: Gmail Watch uses a SERVICE ACCOUNT, NOT your user email!\n"
                        f"   The identity that needs permission is:\n"
                        f"   {gmail_service_account_for_error}\n\n"
                        f"🔍 FIX THIS NOW:\n\n"
                        f"1️⃣ GRANT PERMISSION TO GMAIL SERVICE ACCOUNT:\n"
                        f"   • Topic Level: https://console.cloud.google.com/cloudpubsub/topic/detail/gmail-notifs?project={project_id_for_error}\n"
                        f"     → PERMISSIONS tab → ADD PRINCIPAL\n"
                        f"     → Principal: {gmail_service_account_for_error}\n"
                        f"     → Role: Pub/Sub Publisher\n"
                        f"     → SAVE\n\n"
                        f"   • Project Level: https://console.cloud.google.com/iam-admin/iam?project={project_id_for_error}\n"
                        f"     → GRANT ACCESS\n"
                        f"     → New principals: {gmail_service_account_for_error}\n"
                        f"     → Role: Pub/Sub Publisher\n"
                        f"     → SAVE\n\n"
                        f"2️⃣ WAIT 5-10 MINUTES for permission propagation\n\n"
                        f"3️⃣ TRY AGAIN\n\n"
                        f"📋 SUMMARY:\n"
                        f"   • Service Account: {gmail_service_account_for_error}\n"
                        f"   • Permission needed: Pub/Sub Publisher\n"
                        f"   • Grant at: Topic level AND Project level\n"
                        f"   • Wait: 5-10 minutes after granting\n"
                    )
                else:
                    detailed_msg = f"Gmail API error (Code {error_code}): {error_msg}"
                
                print(f"Gmail watch setup HttpError: {detailed_msg}")
                print(f"Traceback: {traceback.format_exc()}")
                return ResponseView.error_response_without_data(
                    message=detailed_msg,
                    code=FAIL
                )
            except Exception as e:
                error_msg = str(e)
                error_type = type(e).__name__
                print(f"Gmail watch setup error ({error_type}): {error_msg}")
                print(f"Full traceback: {traceback.format_exc()}")
                return ResponseView.error_response_without_data(
                    message=f"Failed to set up Gmail watch: {error_msg}",
                    code=FAIL
                )
                
        except Exception as e:
            error_type = type(e).__name__
            error_msg = str(e)
            full_traceback = traceback.format_exc()
            
            # Print detailed error information
            print("=" * 80)
            print("EMAIL AUTOMATION SETUP ERROR")
            print("=" * 80)
            print(f"Error Type: {error_type}")
            print(f"Error Message: {error_msg}")
            print(f"Full Traceback:")
            print(full_traceback)
            print("=" * 80)
            
            return ResponseView.error_response_without_data(
                message=f"Internal server error: {error_msg} (Type: {error_type})",
                code=FAIL
            )
    
    def _setup_gmail_watch(self, email, credentials_json):
        """Set up Gmail watch for push notifications"""
        print(f"_setup_gmail_watch called for {email}")
        
        # Handle different credential formats
        if isinstance(credentials_json, dict):
            # If it's a dict with credential JWT, we need OAuth2 tokens
            # For production: implement OAuth2 flow to exchange JWT for access/refresh tokens
            if 'credential' in credentials_json:
                # This is a Google Sign-In credential - need OAuth2 flow
                raise ValueError(
                    "Google Sign-In credential provided. Please use OAuth2 flow with scopes: "
                    "https://www.googleapis.com/auth/gmail.readonly, "
                    "https://www.googleapis.com/auth/gmail.modify, "
                    "https://www.googleapis.com/auth/drive.file"
                )
            
            # Assume it's proper OAuth2 credentials
            try:
                print("Creating credentials from authorized user info...")
                creds = Credentials.from_authorized_user_info(credentials_json)
                print("Credentials created successfully")
            except Exception as cred_error:
                print(f"Error creating credentials: {cred_error}")
                print(f"Credentials keys: {list(credentials_json.keys()) if isinstance(credentials_json, dict) else 'N/A'}")
                print(f"Traceback: {traceback.format_exc()}")
                raise ValueError(f"Invalid credentials format: {str(cred_error)}")
        else:
            raise ValueError("Invalid credentials format - expected a dictionary")
        
        # Refresh token if expired
        if creds.expired and creds.refresh_token:
            print("Credentials expired, refreshing...")
            try:
                creds.refresh(Request())
                print("Credentials refreshed successfully")
            except Exception as refresh_error:
                print(f"Error refreshing credentials: {refresh_error}")
                raise ValueError(f"Failed to refresh credentials: {str(refresh_error)}")
        
        # Build Gmail service
        try:
            print("Building Gmail service...")
            service = build('gmail', 'v1', credentials=creds)
            print("Gmail service built successfully")
        except Exception as build_error:
            print(f"Error building Gmail service: {build_error}")
            print(f"Traceback: {traceback.format_exc()}")
            raise ValueError(f"Failed to build Gmail service: {str(build_error)}")
        
        # Get the authenticated user's email from Gmail API
        try:
            print("Fetching authenticated user's email from Gmail API...")
            profile = service.users().getProfile(userId='me').execute()
            authenticated_email = profile.get('emailAddress', '')
            print(f"✓ Authenticated Gmail account: {authenticated_email}")
            print(f"  (This is the email that needs Pub/Sub Publisher permission)")
        except Exception as profile_error:
            print(f"⚠ Could not fetch user profile: {profile_error}")
            authenticated_email = email  # Fallback to provided email
            print(f"  Using provided email: {authenticated_email}")
        
        # Get Pub/Sub topic name from environment
        project_id = os.getenv('GOOGLE_CLOUD_PROJECT_ID')
        project_number = os.getenv('GOOGLE_CLOUD_PROJECT_NUMBER', '37019452145')  # Default to known project number
        topic_name = os.getenv('GMAIL_PUBSUB_TOPIC')
        
        # Gmail Watch API uses a special Gmail service account, NOT the user's email
        # Format: service-{project-number}@gcp-sa-gmail.iam.gserviceaccount.com
        gmail_service_account = f"service-{project_number}@gcp-sa-gmail.iam.gserviceaccount.com"
        
        print(f"Project ID: {project_id}")
        print(f"Project Number: {project_number}")
        print(f"Topic name from env: {topic_name}")
        print(f"Authenticated Gmail user: {authenticated_email}")
        print(f"🔑 Gmail Service Account (needs Pub/Sub Publisher permission): {gmail_service_account}")
        
        if not project_id:
            raise ValueError("GOOGLE_CLOUD_PROJECT_ID not configured. Set it in your .env file.")
        
        # Construct topic name if not provided
        if not topic_name:
            topic_name = f'projects/{project_id}/topics/gmail-notifs'
        elif not topic_name.startswith('projects/'):
            # If just topic ID is provided, construct full path
            topic_name = f'projects/{project_id}/topics/{topic_name}'
        
        print(f"Using topic name: {topic_name}")
        
        # Set up Gmail watch (following Google Cloud documentation)
        request_body = {
            'labelIds': ['INBOX'],
            'topicName': topic_name,
            'labelFilterBehavior': 'INCLUDE'  # Include only messages with these labels
        }
        
        print(f"Calling Gmail watch API with body: {request_body}")
        print(f"🔑 CRITICAL: Gmail Watch uses a SERVICE ACCOUNT, not the user's email!")
        print(f"⚠️ The Gmail Service Account '{gmail_service_account}' MUST have 'Pub/Sub Publisher' role")
        print(f"⚠️ Permission should be granted at BOTH topic level AND project level")
        print(f"📋 Debug Info:")
        print(f"   - Authenticated Gmail user: {authenticated_email} (this is NOT the identity that needs permission)")
        print(f"   - Gmail Service Account: {gmail_service_account} (THIS needs Pub/Sub Publisher permission)")
        print(f"   - Project ID: {project_id}")
        print(f"   - Project Number: {project_number}")
        print(f"   - Topic: {topic_name}")
        print(f"   - Client ID: {credentials_json.get('client_id', 'N/A')[:50]}...")
        print(f"   - Scopes: {credentials_json.get('scopes', [])}")
        
        # Retry mechanism with delay (in case of propagation delay)
        max_retries = 5  # Increased retries
        retry_delay = 5  # Start with 5 seconds
        
        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                print(f"Attempt {attempt}/{max_retries} to set up Gmail watch...")
                response = service.users().watch(userId='me', body=request_body).execute()
                print(f"✅ Gmail watch response: {response}")
                # Response contains: historyId and expiration (timestamp in milliseconds)
                # Store these for later use (watch renewal, history tracking)
                if 'historyId' in response:
                    print(f"   History ID: {response['historyId']}")
                if 'expiration' in response:
                    # Convert milliseconds to datetime
                    expiration_timestamp = int(response['expiration']) / 1000  # Convert to seconds
                    expiration_dt = datetime.fromtimestamp(expiration_timestamp)
                    print(f"   Watch expiration: {expiration_dt}")
                    print(f"   ⚠️ IMPORTANT: Watch must be renewed before expiration (recommended: once per day)")
                    print(f"   ⚠️ Watch must be renewed at least every 7 days to continue receiving notifications")
                return response
            except HttpError as e:
                error_details = e.error_details if hasattr(e, 'error_details') else str(e)
                error_code = e.resp.status if hasattr(e, 'resp') else 'Unknown'
                last_error = e
                
                # If it's a permission error and we have retries left, wait and retry
                if ('not authorized' in str(error_details).lower() or 'forbidden' in str(error_details).lower() or error_code == 403) and attempt < max_retries:
                    print(f"⚠️ Permission error on attempt {attempt}/{max_retries}")
                    print(f"   Error: {error_details}")
                    print(f"   Waiting {retry_delay} seconds before retry (permissions may still be propagating)...")
                    import time
                    time.sleep(retry_delay)
                    retry_delay = min(retry_delay * 1.5, 30)  # Exponential backoff, max 30 seconds
                    continue
                else:
                    # All retries exhausted or non-403 error, break and handle below
                    break
            except Exception as e:
                # Non-HttpError exception, re-raise immediately
                print(f"Unexpected error calling Gmail watch: {e}")
                print(f"Traceback: {traceback.format_exc()}")
                raise
        
        # If we get here, all retries failed
        if last_error:
            error_details = last_error.error_details if hasattr(last_error, 'error_details') else str(last_error)
            error_code = last_error.resp.status if hasattr(last_error, 'resp') else 'Unknown'
            print(f"Gmail API HttpError after {max_retries} attempts: {error_details}")
            print(f"Error code: {error_code}")
            print(f"🔑 CRITICAL: The Gmail Service Account '{gmail_service_account}' needs 'Pub/Sub Publisher' permission")
            print(f"   NOT the user email '{authenticated_email}'!")
            print(f"Topic that needs permission: {topic_name}")
            
            # Enhanced error message with correct service account
            if error_code == 403:
                detailed_msg = (
                    f"⚠️ Gmail API Permission Error (403 Forbidden)\n\n"
                    f"❌ Gmail watch setup failed due to missing Pub/Sub permissions.\n\n"
                    f"🔑 CRITICAL DISCOVERY:\n"
                    f"   Gmail Watch API uses a SERVICE ACCOUNT, NOT your user email!\n"
                    f"   The identity that needs permission is:\n"
                    f"   {gmail_service_account}\n\n"
                    f"🔍 FIX THIS NOW:\n\n"
                    f"1️⃣ GRANT PERMISSION TO GMAIL SERVICE ACCOUNT:\n"
                    f"   • Topic Level: https://console.cloud.google.com/cloudpubsub/topic/detail/gmail-notifs?project={project_id}\n"
                    f"     → PERMISSIONS tab → ADD PRINCIPAL\n"
                    f"     → Principal: {gmail_service_account}\n"
                    f"     → Role: Pub/Sub Publisher\n"
                    f"     → SAVE\n\n"
                    f"   • Project Level: https://console.cloud.google.com/iam-admin/iam?project={project_id}\n"
                    f"     → GRANT ACCESS\n"
                    f"     → New principals: {gmail_service_account}\n"
                    f"     → Role: Pub/Sub Publisher\n"
                    f"     → SAVE\n\n"
                    f"2️⃣ WAIT 5-10 MINUTES for permission propagation\n\n"
                    f"3️⃣ TRY AGAIN\n\n"
                    f"📋 SUMMARY:\n"
                    f"   • Service Account: {gmail_service_account}\n"
                    f"   • Permission needed: Pub/Sub Publisher\n"
                    f"   • Grant at: Topic level AND Project level\n"
                    f"   • Wait: 5-10 minutes after granting\n"
                )
                raise ValueError(detailed_msg)
            else:
                raise ValueError(f"Gmail API error: {error_details}")


@method_decorator(csrf_exempt, name='dispatch')
class GmailPushWebhookView(APIView):
    """Receive Gmail push notifications from Pub/Sub"""
    
    def post(self, request):
        print("\n" + "=" * 80)
        print("📬 GMAIL PUSH WEBHOOK RECEIVED")
        print("=" * 80)
        print(f"Request method: {request.method}")
        print(f"Request path: {request.path}")
        print(f"Request headers: {dict(request.headers)}")
        print(f"Request body length: {len(request.body) if request.body else 0}")
        
        try:
            # Pub/Sub sends data in specific format
            envelope = json.loads(request.body)
            print(f"Envelope keys: {list(envelope.keys())}")
            print(f"Message keys: {list(envelope.get('message', {}).keys())}")
            
            # Verify the message came from Pub/Sub (optional but recommended)
            # You can add verification logic here
            
            # Decode the message (Google uses base64url encoding, not standard base64)
            # base64url is URL-safe base64 encoding (uses - and _ instead of + and /)
            message_data_encoded = envelope['message']['data']
            print(f"Encoded message data (first 50 chars): {message_data_encoded[:50]}...")
            
            # Add padding if needed (base64 requires length to be multiple of 4)
            padding = len(message_data_encoded) % 4
            if padding:
                message_data_encoded += '=' * (4 - padding)
            message_data = base64.urlsafe_b64decode(message_data_encoded).decode('utf-8')
            print(f"Decoded message data: {message_data}")
            
            data = json.loads(message_data)
            print(f"Parsed data: {data}")
            
            # Extract email address and historyId
            email_address = data.get('emailAddress')
            history_id = data.get('historyId')
            message_id = envelope.get('message', {}).get('messageId')  # Pub/Sub message ID for deduplication
            
            print(f"📧 Email address: {email_address}")
            print(f"📜 History ID: {history_id}")
            print(f"📨 Pub/Sub Message ID: {message_id}")
            
            if not email_address:
                print("❌ No email address in notification")
                return HttpResponse(status=400)
            
            # Get email account
            email_account = EmailAccount.objects.filter(
                email=email_address,
                is_active=True,
                is_automated=True
            ).first()
            
            if not email_account:
                print(f"⚠️ Email account not found or not automated: {email_address}")
                return HttpResponse(status=200)  # Account not found, but return 200 to Pub/Sub
            
            print(f"✅ Found email account: {email_account.email}")
            print(f"   Current watch_history_id: {email_account.watch_history_id}")
            print(f"   New history_id from notification: {history_id}")
            
            # Skip if notification's history_id is same or older than stored history_id
            # This prevents processing duplicate notifications or notifications for changes we've already processed
            if email_account.watch_history_id and history_id:
                try:
                    stored_history_id_int = int(email_account.watch_history_id)
                    notification_history_id_int = int(history_id)
                    
                    if notification_history_id_int <= stored_history_id_int:
                        print(f"⏭️  Skipping notification: history_id ({history_id}) is same or older than stored ({email_account.watch_history_id})")
                        print(f"   This notification is for changes we've already processed.")
                        return HttpResponse(status=200)  # Return 200 to acknowledge receipt
                except (ValueError, TypeError):
                    # If history IDs can't be compared as integers, proceed with processing
                    pass
            
            # Process new emails
            # Use the STORED watch_history_id as starting point, not the notification's history_id
            # The notification's history_id is the current state, but we need to query from the last known state
            print("🔄 Starting to process new emails...")
            print(f"   Using stored history_id ({email_account.watch_history_id}) as starting point")
            EmailAutomationService().process_new_emails(
                email_account, 
                email_account.watch_history_id  # Use STORED history ID, not notification's history ID!
            )
            
            print("✅ Email processing completed successfully")
            print("=" * 80 + "\n")
            return HttpResponse(status=200)
            
        except Exception as e:
            print(f"❌ GmailPushWebhookView error: {e}")
            print(f"Traceback: {traceback.format_exc()}")
            # Return 200 to Pub/Sub even on error to prevent retries
            return HttpResponse(status=200)


class EmailAutomationService:
    """Service class for email automation logic"""
    
    def process_new_emails(self, email_account, history_id=None):
        """Process new emails and save invoice-related ones to Drive"""
        try:
            creds = Credentials.from_authorized_user_info(email_account.credentials)
            
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                # Update stored credentials
                email_account.credentials = json.loads(creds.to_json())
                email_account.save()
            
            gmail_service = build('gmail', 'v1', credentials=creds)
            drive_service = build('drive', 'v3', credentials=creds)
            
            # Get messages since last historyId
            # Use broader query to catch all emails, not just unread ones
            # (in case email was already read but not processed)
            query = "in:inbox"  # Removed "is:unread" to catch all inbox emails
            
            if history_id:
                # Get history to find new messages since the stored history_id
                print(f"🔍 Querying Gmail history API from history_id: {history_id}")
                try:
                    history = gmail_service.users().history().list(
                        userId='me',
                        startHistoryId=history_id,
                        historyTypes=['messageAdded']
                    ).execute()
                    
                    message_ids = []
                    history_records = history.get('history', [])
                    print(f"📋 History API returned {len(history_records)} history record(s)")
                    
                    for record in history_records:
                        messages_added = record.get('messagesAdded', [])
                        print(f"   Record has {len(messages_added)} message(s) added")
                        for msg in messages_added:
                            message_ids.append(msg['message']['id'])
                    
                    print(f"📬 Found {len(message_ids)} message(s) from history API")
                    
                    # If history API returns 0 messages, try fallback query
                    if len(message_ids) == 0:
                        print(f"⚠️ History API returned 0 messages. Trying fallback query: '{query}'")
                        results = gmail_service.users().messages().list(
                            userId='me',
                            q=query,
                            maxResults=20  # Increased from 10 to catch more emails
                        ).execute()
                        fallback_messages = results.get('messages', [])
                        message_ids = [msg['id'] for msg in fallback_messages]
                        print(f"📬 Fallback query found {len(message_ids)} message(s)")
                except HttpError as e:
                    # If historyId is too old, fall back to query
                    print(f"❌ History API error: {e}")
                    print(f"   Falling back to query: '{query}'")
                    results = gmail_service.users().messages().list(
                        userId='me',
                        q=query,
                        maxResults=20  # Increased from 10
                    ).execute()
                    message_ids = [msg['id'] for msg in results.get('messages', [])]
                    print(f"📬 Fallback query found {len(message_ids)} message(s)")
            else:
                print(f"⚠️ No history_id provided. Using query: '{query}'")
                results = gmail_service.users().messages().list(
                    userId='me',
                    q=query,
                    maxResults=20  # Increased from 10
                ).execute()
                message_ids = [msg['id'] for msg in results.get('messages', [])]
                print(f"📬 Query found {len(message_ids)} message(s)")
            
            print(f"📬 Total message(s) to process: {len(message_ids)}")
            
            # Process each message
            processed_count = 0
            for msg_id in message_ids:
                try:
                    # Try to get the message - handle 404 errors gracefully
                    try:
                        message = gmail_service.users().messages().get(
                            userId='me',
                            id=msg_id,
                            format='full'
                        ).execute()
                    except HttpError as e:
                        if e.resp.status == 404:
                            # Message was deleted or moved - skip it
                            print(f"⏭️  Message {msg_id} not found (deleted/moved). Skipping.")
                            continue
                        else:
                            # Re-raise other HTTP errors
                            raise
                    
                    # Get subject for logging
                    subject = self._get_email_header(message, 'Subject')
                    
                    # Check if already processed
                    if ProcessedEmail.objects.filter(gmail_message_id=msg_id).exists():
                        print(f"⏭️  Skipping already processed: {subject[:50]}...")
                        continue
                    
                    # Check if email is invoice-related
                    if self._is_invoice_email(message):
                        print(f"\n🔍 Invoice email detected: {subject}")
                        self._save_to_drive(
                            message, 
                            drive_service,
                            gmail_service,  # Pass gmail_service for downloading attachments
                            email_account,
                            msg_id
                        )
                        
                        # Mark email as read and archive it (remove from INBOX)
                        try:
                            gmail_service.users().messages().modify(
                                userId='me',
                                id=msg_id,
                                body={'removeLabelIds': ['UNREAD', 'INBOX']}  # Mark as read and archive
                            ).execute()
                            print(f"✅ Email archived: {subject[:50]}...")
                        except Exception as archive_error:
                            print(f"⚠️ Saved to Drive but failed to archive: {archive_error}")
                        
                        processed_count += 1
                    else:
                        print(f"⏭️  Skipping non-invoice email: {subject[:50]}...")
                except Exception as e:
                    print(f"❌ Error processing message {msg_id}: {e}")
                    continue
            
            # Update history ID after processing
            try:
                profile = gmail_service.users().getProfile(userId='me').execute()
                latest_history_id = profile.get('historyId')
                if latest_history_id and str(latest_history_id) != str(email_account.watch_history_id):
                    email_account.watch_history_id = latest_history_id
                    email_account.save()
                    print(f"✅ Updated history ID to: {latest_history_id}")
            except Exception as e:
                print(f"⚠️ Failed to update history ID: {e}")
            
            print(f"✅ Processed {processed_count} invoice email(s)")
                    
        except Exception as e:
            print(f"❌ Error in process_new_emails: {e}")
            print(f"Traceback: {traceback.format_exc()}")
            raise
    
    def _is_invoice_email(self, message):
        """Check if email is invoice-related"""
        subject = ""
        sender = ""
        body_text = ""
        
        headers = message['payload'].get('headers', [])
        for header in headers:
            name = header['name'].lower()
            if name == 'subject':
                subject = header['value'].lower()
            elif name == 'from':
                sender = header['value'].lower()
        
        # Get email body
        body_text = self._get_email_body(message)
        
        # Keywords to detect invoices
        invoice_keywords = [
            'invoice', 'bill', 'payment', 'receipt', 
            'statement', 'billing', 'due', 'amount due',
            'payment due', 'invoice #', 'invoice number',
            'pay now', 'payment request'
        ]
        
        # Check subject
        if any(keyword in subject for keyword in invoice_keywords):
            return True
        
        # Check sender domain (common invoice senders)
        invoice_domains = [
            'quickbooks', 'xero', 'freshbooks', 'stripe', 
            'paypal', 'square', 'invoice', 'billing'
        ]
        if any(domain in sender for domain in invoice_domains):
            return True
        
        # Check body content
        if any(keyword in body_text.lower() for keyword in invoice_keywords):
            return True
        
        return False
    
    def _get_email_body(self, message):
        """Extract email body text, preferring plain text over HTML"""
        plain_text = ""
        html_text = ""
        payload = message.get('payload', {})
        
        def extract_body(part, mime_type=None):
            """Extract body from part, distinguishing between plain text and HTML"""
            text = ""
            part_mime = part.get('mimeType', mime_type or '')
            
            if part.get('body', {}).get('data'):
                data = part['body']['data']
                decoded_text = base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')
                
                # Check if this is HTML or plain text
                if 'text/html' in part_mime.lower():
                    return ('', decoded_text)  # (plain_text, html_text)
                elif 'text/plain' in part_mime.lower():
                    return (decoded_text, '')  # (plain_text, html_text)
                else:
                    # If mime type not specified, try to detect
                    if decoded_text.strip().startswith('<') and '>' in decoded_text:
                        return ('', decoded_text)  # Likely HTML
                    else:
                        return (decoded_text, '')  # Likely plain text
            
            elif part.get('parts'):
                plain = ""
                html = ""
                for subpart in part['parts']:
                    sub_plain, sub_html = extract_body(subpart, part_mime)
                    plain += sub_plain
                    html += sub_html
                return (plain, html)
            
            return ('', '')
        
        plain_text, html_text = extract_body(payload)
        
        # Prefer plain text, but if only HTML is available, strip HTML tags
        if plain_text.strip():
            return plain_text.strip()
        elif html_text.strip():
            # Strip HTML tags and decode HTML entities
            # Remove HTML tags using regex
            clean_text = re.sub(r'<[^>]+>', '', html_text)
            # Decode HTML entities (like &nbsp;, &lt;, etc.)
            clean_text = html.unescape(clean_text)
            # Clean up extra whitespace
            clean_text = re.sub(r'\n\s*\n', '\n\n', clean_text)  # Multiple newlines to double
            clean_text = re.sub(r'[ \t]+', ' ', clean_text)  # Multiple spaces to single
            return clean_text.strip()
        else:
            return ""
    
    def _get_attachments(self, message, gmail_service, msg_id):
        """Extract and download attachments from email message"""
        attachments = []
        payload = message.get('payload', {})
        
        def extract_attachments(part, part_id=''):
            """Recursively extract attachments from message parts"""
            if part.get('filename') and part.get('body', {}).get('attachmentId'):
                attachment_id = part['body']['attachmentId']
                filename = part['filename']
                mime_type = part.get('mimeType', 'application/octet-stream')
                size = part.get('body', {}).get('size', 0)
                
                try:
                    # Download attachment
                    attachment = gmail_service.users().messages().attachments().get(
                        userId='me',
                        messageId=msg_id,
                        id=attachment_id
                    ).execute()
                    
                    # Decode attachment data
                    file_data = base64.urlsafe_b64decode(attachment['data'])
                    
                    attachments.append({
                        'filename': filename,
                        'data': file_data,
                        'mime_type': mime_type,
                        'size': size
                    })
                    
                    print(f"   📎 Attachment found: {filename} ({size} bytes, {mime_type})")
                except Exception as e:
                    print(f"   ⚠️ Failed to download attachment {filename}: {e}")
            
            # Recursively check nested parts
            if part.get('parts'):
                for i, subpart in enumerate(part['parts']):
                    extract_attachments(subpart, f"{part_id}.{i}")
        
        extract_attachments(payload)
        return attachments
    
    def _save_to_drive(self, message, drive_service, gmail_service, email_account, msg_id):
        """Save invoice email and attachments to Google Drive in year/month folder"""
        try:
            # Extract email content
            subject = self._get_email_header(message, 'Subject')
            sender = self._get_email_header(message, 'From')
            date = self._get_email_header(message, 'Date')
            to_email = self._get_email_header(message, 'To')
            body = self._get_email_body(message)
            
            # Extract year and month from email date
            try:
                if date:
                    email_date = datetime.strptime(date, '%a, %d %b %Y %H:%M:%S %z')
                else:
                    email_date = datetime.now()
            except:
                email_date = datetime.now()
            
            year = email_date.year
            month_name = email_date.strftime('%B')  # November, December, etc.
            
            # Get year/month folder
            folder_id = self._get_or_create_month_folder(drive_service, month_name, year)
            
            # Create email file content
            email_content = f"""From: {sender}
To: {to_email}
Subject: {subject}
Date: {date}

{body}
"""
            
            # Create filename
            safe_subject = re.sub(r'[^\w\s-]', '', subject)[:50]
            filename = f"{safe_subject}_{msg_id[:10]}.txt"
            
            # Save email as text file
            file_metadata = {
                'name': filename,
                'parents': [folder_id]
            }
            
            media = MediaIoBaseUpload(
                BytesIO(email_content.encode('utf-8')),
                mimetype='text/plain',
                resumable=True
            )
            
            file = drive_service.files().create(
                body=file_metadata,
                media_body=media,
                fields='id'
            ).execute()
            
            # Extract and save attachments
            attachments = self._get_attachments(message, gmail_service, msg_id)
            attachment_files = []
            
            if attachments:
                print(f"   📎 Saving {len(attachments)} attachment(s) to Drive...")
                for attachment in attachments:
                    try:
                        # Create safe filename
                        safe_filename = re.sub(r'[^\w\s.-]', '', attachment['filename'])
                        attachment_metadata = {
                            'name': safe_filename,
                            'parents': [folder_id]
                        }
                        
                        attachment_media = MediaIoBaseUpload(
                            BytesIO(attachment['data']),
                            mimetype=attachment['mime_type'],
                            resumable=True
                        )
                        
                        attachment_file = drive_service.files().create(
                            body=attachment_metadata,
                            media_body=attachment_media,
                            fields='id'
                        ).execute()
                        
                        attachment_files.append({
                            'filename': attachment['filename'],
                            'file_id': attachment_file.get('id'),
                            'mime_type': attachment['mime_type']
                        })
                        
                        print(f"      ✅ Saved: {attachment['filename']} (ID: {attachment_file.get('id')})")
                    except Exception as e:
                        print(f"      ❌ Failed to save attachment {attachment['filename']}: {e}")
            
            # Save to database
            try:
                received_date = datetime.strptime(date, '%a, %d %b %Y %H:%M:%S %z') if date else timezone.now()
            except:
                received_date = timezone.now()
            
            ProcessedEmail.objects.create(
                email_account=email_account,
                gmail_message_id=msg_id,
                subject=subject,
                sender=sender,
                received_date=received_date,
                drive_file_id=file.get('id'),
                drive_folder_name=f"{year}/{month_name}",
                attachments=attachment_files,  # Store attachment information
                is_invoice=True
            )
            
            # Print success message with email subject prominently displayed
            print("\n" + "=" * 80)
            print(f"✅ SUCCESSFULLY SAVED TO GOOGLE DRIVE")
            print("=" * 80)
            print(f"📧 EMAIL SUBJECT: {subject}")
            print(f"📁 Drive Folder: {year}/{month_name}")
            print(f"📄 Email File ID: {file.get('id')}")
            if attachment_files:
                print(f"📎 Attachments ({len(attachment_files)}):")
                for att in attachment_files:
                    print(f"   • {att['filename']} (ID: {att['file_id']})")
            else:
                print(f"📎 Attachments: None")
            print(f"📧 From: {sender}")
            print(f"💾 Message ID: {msg_id}")
            print(f"📅 Date: {date}")
            print("=" * 80 + "\n")
            
        except Exception as e:
            print(f"Error saving to Drive: {e}")
            raise
    
    def _get_email_header(self, message, header_name):
        """Extract email header value"""
        headers = message['payload'].get('headers', [])
        for header in headers:
            if header['name'].lower() == header_name.lower():
                return header['value']
        return ""
    
    def _get_or_create_invoice_folder(self, drive_service):
        """Get existing 'Adam Pearson Invoice' folder or create new one in root"""
        try:
            # Check if folder exists in root
            # Escape single quote by doubling it for Google Drive API query
            folder_name_escaped = "Adam Pearson Invoice".replace("'", "''")
            query = f"name='{folder_name_escaped}' and mimeType='application/vnd.google-apps.folder' and trashed=false and 'root' in parents"
            results = drive_service.files().list(q=query).execute()
            
            folders = results.get('files', [])
            if folders:
                return folders[0]['id']
            
            # Create new folder in root
            folder_metadata = {
                'name': "Adam Pearson Invoice",
                'mimeType': 'application/vnd.google-apps.folder'
            }
            folder = drive_service.files().create(
                body=folder_metadata,
                fields='id'
            ).execute()
            
            return folder.get('id')
            
        except Exception as e:
            print(f"Error getting/creating 'Adam Pearson Invoice' folder: {e}")
            raise
    
    def _get_or_create_year_folder(self, drive_service, year):
        """Get existing year folder or create new one inside 'Adam Pearson Invoice' folder"""
        try:
            # First, get or create the parent "Adam Pearson Invoice" folder
            parent_folder_id = self._get_or_create_invoice_folder(drive_service)
            
            # Check if year folder exists inside parent folder
            query = f"name='{year}' and mimeType='application/vnd.google-apps.folder' and trashed=false and '{parent_folder_id}' in parents"
            results = drive_service.files().list(q=query).execute()
            
            folders = results.get('files', [])
            if folders:
                return folders[0]['id']
            
            # Create new year folder inside parent folder
            folder_metadata = {
                'name': str(year),
                'mimeType': 'application/vnd.google-apps.folder',
                'parents': [parent_folder_id]
            }
            folder = drive_service.files().create(
                body=folder_metadata,
                fields='id'
            ).execute()
            
            return folder.get('id')
            
        except Exception as e:
            print(f"Error getting/creating year folder: {e}")
            raise
    
    def _get_or_create_month_folder(self, drive_service, month_name, year):
        """Get existing month folder or create new one inside year folder"""
        try:
            # First, get or create the year folder
            year_folder_id = self._get_or_create_year_folder(drive_service, year)
            
            # Check if month folder exists inside year folder
            query = f"name='{month_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false and '{year_folder_id}' in parents"
            results = drive_service.files().list(q=query).execute()
            
            folders = results.get('files', [])
            if folders:
                return folders[0]['id']
            
            # Create new month folder inside year folder
            folder_metadata = {
                'name': month_name,
                'mimeType': 'application/vnd.google-apps.folder',
                'parents': [year_folder_id]
            }
            folder = drive_service.files().create(
                body=folder_metadata,
                fields='id'
            ).execute()
            
            return folder.get('id')
            
        except Exception as e:
            print(f"Error getting/creating month folder: {e}")
            raise


class EmailAccountListView(APIView):
    """Get list of all email accounts for the current user"""
    
    @user_token_auth
    def get(self, request):
        try:
            user = request.auth_user
            if not user:
                return ResponseView.error_response_without_data(
                    "Authentication required",
                    code=FAIL
                )
            
            # Get email accounts for the current user
            email_accounts = EmailAccount.objects.filter(user=user, is_active=True)
            
            accounts_list = []
            for account in email_accounts:
                account_data = {
                    "account_id": str(account.account_id),
                    "email": account.email,
                    "is_automated": account.is_automated,
                    "is_active": account.is_active,
                    "created_at": account.created_at.isoformat() if account.created_at else None,
                    "updated_at": account.updated_at.isoformat() if account.updated_at else None,
                    "watch_expiration": account.watch_expiration.isoformat() if account.watch_expiration else None,
                }
                accounts_list.append(account_data)
            
            return ResponseView.success_response_data(
                data=accounts_list,
                message="Email accounts retrieved successfully",
                code=SUCCESS
            )
            
        except Exception as e:
            print(f"❌ Error in EmailAccountListView: {str(e)}")
            import traceback
            print(traceback.format_exc())
            return ResponseView.error_response_without_data(
                f"Error retrieving email accounts: {str(e)}",
                code=FAIL
            )


class EmailAccountDeleteView(APIView):
    """Delete an email account"""
    
    @user_token_auth
    def delete(self, request, account_id):
        """Delete an email account from the database"""
        try:
            user = request.auth_user
            if not user:
                return ResponseView.error_response_without_data(
                    "Authentication required",
                    code=FAIL
                )
            
            try:
                # Get the email account from database
                account = EmailAccount.objects.get(account_id=account_id, user=user)
            except EmailAccount.DoesNotExist:
                return ResponseView.error_response_without_data(
                    "Email account not found or access denied",
                    code=FAIL
                )
            
            # Delete the account record from database
            account.delete()
            
            print(f"✅ Email account {account_id} deleted successfully from database")
            
            return ResponseView.success_response_without_data(
                message="Email account deleted successfully",
                code=SUCCESS
            )
            
        except Exception as e:
            print(f"❌ Error deleting email account {account_id}: {str(e)}")
            import traceback
            print(traceback.format_exc())
            return ResponseView.error_response_without_data(
                f"Failed to delete email account: {str(e)}",
                code=FAIL
            )


class ProcessedEmailListView(APIView):
    """Get list of all processed emails for the current user"""
    
    @user_token_auth
    def get(self, request):
        try:
            user = request.auth_user
            if not user:
                return ResponseView.error_response_without_data(
                    "Authentication required",
                    code=FAIL
                )
            
            # Get processed emails for all email accounts belonging to the user
            email_accounts = EmailAccount.objects.filter(user=user, is_active=True)
            processed_emails = ProcessedEmail.objects.filter(email_account__in=email_accounts)
            
            emails_list = []
            for email in processed_emails:
                # Construct Google Drive URL
                drive_url = None
                if email.drive_file_id:
                    drive_url = f"https://drive.google.com/file/d/{email.drive_file_id}/view"
                
                # Process attachments to include Drive URLs
                attachments_with_urls = []
                if email.attachments:
                    for attachment in email.attachments:
                        attachment_drive_url = None
                        if attachment.get('file_id'):
                            attachment_drive_url = f"https://drive.google.com/file/d/{attachment['file_id']}/view"
                        attachments_with_urls.append({
                            "filename": attachment.get('filename', 'Unknown'),
                            "file_id": attachment.get('file_id'),
                            "mime_type": attachment.get('mime_type', ''),
                            "drive_url": attachment_drive_url
                        })
                
                email_data = {
                    "email_id": str(email.email_id),
                    "gmail_message_id": email.gmail_message_id,
                    "subject": email.subject,
                    "sender": email.sender,
                    "received_date": email.received_date.isoformat() if email.received_date else None,
                    "drive_file_id": email.drive_file_id,
                    "drive_url": drive_url,
                    "drive_folder_name": email.drive_folder_name,
                    "attachments": attachments_with_urls,
                    "is_invoice": email.is_invoice,
                    "created_at": email.created_at.isoformat() if email.created_at else None,
                    "email_account": email.email_account.email,
                }
                emails_list.append(email_data)
            
            return ResponseView.success_response_data(
                data=emails_list,
                message="Processed emails retrieved successfully",
                code=SUCCESS
            )
            
        except Exception as e:
            print(f"❌ Error in ProcessedEmailListView: {str(e)}")
            import traceback
            print(traceback.format_exc())
            return ResponseView.error_response_without_data(
                f"Error retrieving processed emails: {str(e)}",
                code=FAIL
            )


class ProcessedEmailDeleteView(APIView):
    """Delete a processed email"""
    
    @user_token_auth
    def delete(self, request, email_id):
        """Delete a processed email from the database"""
        try:
            user = request.auth_user
            if not user:
                return ResponseView.error_response_without_data(
                    "Authentication required",
                    code=FAIL
                )
            
            try:
                # Get the processed email from database
                # Ensure it belongs to an email account owned by the user
                processed_email = ProcessedEmail.objects.get(
                    email_id=email_id,
                    email_account__user=user
                )
            except ProcessedEmail.DoesNotExist:
                return ResponseView.error_response_without_data(
                    "Processed email not found or access denied",
                    code=FAIL
                )
            
            # Store drive URL before deletion for response
            drive_url = None
            if processed_email.drive_file_id:
                drive_url = f"https://drive.google.com/file/d/{processed_email.drive_file_id}/view"
            
            # Delete the processed email record from database
            processed_email.delete()
            
            print(f"✅ Processed email {email_id} deleted successfully from database")
            
            return ResponseView.success_response_data(
                data={"drive_url": drive_url},
                message="Processed email deleted successfully",
                code=SUCCESS
            )
            
        except Exception as e:
            print(f"❌ Error deleting processed email {email_id}: {str(e)}")
            import traceback
            print(traceback.format_exc())
            return ResponseView.error_response_without_data(
                f"Failed to delete processed email: {str(e)}",
                code=FAIL
            )

