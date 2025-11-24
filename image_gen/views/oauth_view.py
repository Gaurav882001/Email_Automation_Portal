import os
import json
from rest_framework.views import APIView
from django.http import HttpResponse
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from utils.response import ResponseView
from utils.constant import SUCCESS, FAIL


class GoogleOAuthCallbackView(APIView):
    """Handle OAuth callback and exchange code for tokens"""
    
    def post(self, request):
        try:
            data = request.data
            code = data.get('code')
            redirect_uri = data.get('redirect_uri')
            
            if not code or not redirect_uri:
                return ResponseView.error_response_without_data(
                    message="Code and redirect_uri are required",
                    code=FAIL
                )
            
            # Get OAuth credentials from environment
            client_id = os.getenv('GOOGLE_CLIENT_ID')
            client_secret = os.getenv('GOOGLE_CLIENT_SECRET')
            
            if not client_id or not client_secret:
                return ResponseView.error_response_without_data(
                    message="Google OAuth credentials not configured",
                    code=FAIL
                )
            
            # Debug logging
            print(f"OAuth callback - Code received: {code[:20] if code else 'None'}...")
            print(f"OAuth callback - Code length: {len(code) if code else 0}")
            print(f"OAuth callback - Redirect URI: {redirect_uri}")
            print(f"OAuth callback - Client ID: {client_id}")
            print(f"OAuth callback - Client Secret: {'*' * 10 if client_secret else 'None'}")
            
            # Validate redirect_uri format
            if not redirect_uri.startswith('http://') and not redirect_uri.startswith('https://'):
                return ResponseView.error_response_without_data(
                    message=f"Invalid redirect_uri format: {redirect_uri}. Must start with http:// or https://",
                    code=FAIL
                )
            
            # Create OAuth flow
            # IMPORTANT: redirect_uri MUST match exactly what was used in authorization request
            flow = Flow.from_client_config(
                {
                    "web": {
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token",
                        "redirect_uris": [redirect_uri]  # List of allowed redirect URIs
                    }
                },
                scopes=[
                    'https://www.googleapis.com/auth/gmail.readonly',
                    'https://www.googleapis.com/auth/gmail.modify',
                    'https://www.googleapis.com/auth/drive.file',
                ]
            )
            
            # Set the redirect_uri on the Flow object
            # This ensures it matches what was used in the authorization request
            flow.redirect_uri = redirect_uri
            
            # Exchange code for tokens
            # Do NOT pass redirect_uri to fetch_token - it's already set on the Flow object
            try:
                flow.fetch_token(code=code)
            except Exception as token_error:
                error_str = str(token_error)
                error_type = type(token_error).__name__
                print(f"Token exchange error: {token_error}")
                print(f"Error type: {error_type}")
                print(f"Error string: {error_str}")
                print(f"Code length: {len(code) if code else 0}")
                print(f"Code preview: {code[:30] if code else 'None'}...")
                print(f"Redirect URI used: {redirect_uri}")
                print(f"Client ID: {client_id}")
                
                # Check if it's an invalid_grant error (code expired or already used)
                if 'invalid_grant' in error_str.lower() or error_type == 'InvalidGrantError':
                    raise ValueError(
                        "Authorization code expired or already used. "
                        "Please try the OAuth flow again from the beginning. "
                        "Authorization codes expire quickly (usually within 1 minute). "
                        "Make sure to complete the OAuth flow immediately after authorization."
                    )
                elif 'redirect_uri_mismatch' in error_str.lower() or 'redirect_uri' in error_str.lower():
                    raise ValueError(
                        f"Redirect URI mismatch. The redirect URI '{redirect_uri}' "
                        "does not match what was used in the authorization request. "
                        f"Make sure this exact URI is added to Google Cloud Console. "
                        f"Error details: {error_str}"
                    )
                else:
                    # Re-raise with more context
                    raise ValueError(f"Token exchange failed: {error_str} (Type: {error_type})")
            
            # Get credentials
            creds = flow.credentials
            
            # Get user email from Gmail API
            service = build('gmail', 'v1', credentials=creds)
            profile = service.users().getProfile(userId='me').execute()
            email = profile.get('emailAddress')
            
            # Prepare credentials for frontend
            credentials_data = {
                'token': creds.token,
                'refresh_token': creds.refresh_token,
                'token_uri': creds.token_uri,
                'client_id': client_id,
                'client_secret': client_secret,
                'scopes': creds.scopes,
            }
            
            return ResponseView.success_response_data(
                data={
                    'email': email,
                    'credentials': credentials_data
                },
                message="OAuth tokens obtained successfully",
                code=SUCCESS
            )
            
        except Exception as e:
            import traceback
            error_msg = str(e)
            print(f"OAuth callback error: {e}")
            print(f"Traceback: {traceback.format_exc()}")
            
            # Provide more helpful error messages
            if 'invalid_grant' in error_msg.lower():
                return ResponseView.error_response_without_data(
                    message="Authorization code expired or already used. Please try the OAuth flow again. Authorization codes expire quickly (within minutes).",
                    code=FAIL
                )
            elif 'redirect_uri_mismatch' in error_msg.lower():
                return ResponseView.error_response_without_data(
                    message=f"Redirect URI mismatch. Used: {redirect_uri}. Make sure this exact URI is added to Google Cloud Console.",
                    code=FAIL
                )
            else:
                return ResponseView.error_response_without_data(
                    message=f"Failed to exchange code for tokens: {error_msg}",
                    code=FAIL
                )

