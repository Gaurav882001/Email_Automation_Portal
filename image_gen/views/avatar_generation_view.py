import os
import uuid
import base64
import warnings
import threading
import requests
import time
import openai
import re
import json
from django.conf import settings
from django.utils import timezone
from django.db import transaction, models
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.parsers import MultiPartParser, FormParser
from dotenv import load_dotenv

from utils.response import ResponseInfo
from utils.jwt_utils import verify_jwt_token
from image_gen.models import AvatarGenerationJob, AvatarReferenceImage
from image_gen.db_models.user import Users

# Load environment variables
load_dotenv()

# Disable SSL warnings
warnings.filterwarnings('ignore', message='Unverified HTTPS request')


def get_current_user(request):
    """Get current user from JWT token in Authorization header"""
    try:
        auth_header = request.META.get('HTTP_AUTHORIZATION', '')
        if not auth_header.startswith('Bearer '):
            return None
        
        token = auth_header.split(' ')[1]
        payload = verify_jwt_token(token)
        
        if not payload or 'user_id' not in payload:
            return None
        
        user = Users.objects.get(id=payload['user_id'])
        return user
    except (Users.DoesNotExist, IndexError, AttributeError):
        return None


def check_avatar_generation_status(job_id, generation_id, api_key):
    """Check the status of avatar generation using HeyGen API (standalone function for use by multiple classes)"""
    max_attempts = 60  # Check for up to 5 minutes (60 * 5 seconds)
    attempt = 0
    
    while attempt < max_attempts:
        try:
            # Get job and check if already completed
            job = AvatarGenerationJob.objects.get(job_id=job_id)
            
            # Early exit if job is already completed or failed
            if job.status == 'completed':
                print(f"✅ Job {job_id} already completed, stopping status checks")
                return
            if job.status == 'error':
                print(f"❌ Job {job_id} already failed, stopping status checks")
                return
            
            # Determine if this is an avatar_group job (has image_key) or photo/generate job
            is_avatar_group = bool(job.image_key)
            print(f"🔍 check_avatar_generation_status: Job {job_id} - image_key: {job.image_key}, is_avatar_group: {is_avatar_group}")
            
            if is_avatar_group:
                # Use avatar_group.list endpoint for avatar_group jobs (as per HeyGen documentation)
                status_url = "https://api.heygen.com/v2/avatar_group.list"
                headers = {
                    'accept': 'application/json',
                    'x-api-key': api_key
                }
                
                print(f"🔗 check_avatar_generation_status: Using avatar_group.list endpoint for job {job_id}")
                # Get list of avatar groups and find the one matching our generation_id
                status_response = requests.get(status_url, headers=headers, params={'include_public': 'false'}, timeout=30)
                
                if status_response.status_code == 200:
                    status_data = status_response.json()
                    print(f"📊 Status check {attempt + 1} (avatar_group): {status_data}")
                    
                    # Find the avatar group with matching id (response uses 'avatar_group_list')
                    avatar_groups = (
                        status_data.get('data', {}).get('avatar_group_list', []) or 
                        status_data.get('data', {}).get('items', []) or 
                        status_data.get('avatar_group_list', []) or 
                        status_data.get('items', []) or 
                        []
                    )
                    matching_group = None
                    
                    print(f"🔍 check_avatar_generation_status: Looking for generation_id: {generation_id} in {len(avatar_groups)} avatar groups")
                    
                    for group in avatar_groups:
                        group_id = group.get('id') or group.get('group_id')
                        print(f"🔍 check_avatar_generation_status: Comparing group_id: {group_id} with generation_id: {generation_id}")
                        if str(group_id) == str(generation_id):
                            matching_group = group
                            print(f"✅ check_avatar_generation_status: Found matching group: {group_id}")
                            break
                    
                    if matching_group:
                        # Extract status from avatar_group - check train_status and preview_image
                        train_status = matching_group.get('train_status', '')
                        preview_image = matching_group.get('preview_image')
                        
                        print(f"📊 check_avatar_generation_status: Group train_status: {train_status}, preview_image: {bool(preview_image)}")
                        
                        # Avatar is ready when preview_image exists (even if train_status is 'empty')
                        # preview_image indicates the avatar is generated and ready
                        is_completed = (preview_image is not None and preview_image)
                        
                        avatar_url = preview_image or matching_group.get('image_url') or matching_group.get('url')
                        
                        if is_completed:
                            # Avatar generation completed
                            try:
                                job.status = "completed"
                                job.progress = 100
                                job.completed_at = timezone.now()
                                if avatar_url:
                                    # Truncate URL if it's too long (though URLField should handle it)
                                    job.avatar_url = avatar_url[:2000] if len(avatar_url) > 2000 else avatar_url
                                
                                # Extract and save avatar_id if available (truncate to 255 chars)
                                avatar_id_value = matching_group.get('avatar_id') or matching_group.get('id')
                                if avatar_id_value:
                                    job.avatar_id = str(avatar_id_value)[:255]
                                
                                job.save()
                                print(f"✅ Job {job_id} completed successfully! Avatar URL: {avatar_url}")
                                # Break out of the loop since job is completed
                                break
                            except Exception as save_error:
                                print(f"❌ Error saving completed job {job_id}: {str(save_error)}")
                                import traceback
                                print(traceback.format_exc())
                                # Try to save with minimal fields to at least mark it as completed
                                try:
                                    job.status = "completed"
                                    job.progress = 100
                                    job.completed_at = timezone.now()
                                    if avatar_url:
                                        job.avatar_url = avatar_url[:500]  # Truncate to safe length
                                    job.save(update_fields=['status', 'progress', 'completed_at', 'avatar_url'])
                                    print(f"✅ Job {job_id} marked as completed (with truncated URL)")
                                except Exception as retry_error:
                                    print(f"❌ Failed to save job even with minimal fields: {str(retry_error)}")
                                # Break out of loop even if save failed
                                break
                        elif train_status == 'failed' or train_status == 'error':
                            # Generation failed
                            error_msg = matching_group.get('message') or matching_group.get('error_message', 'Avatar generation failed')
                            job.status = "error"
                            job.progress = 0
                            job.completed_at = timezone.now()
                            job.error_message = error_msg
                            job.save()
                            print(f"❌ Job {job_id} failed: {error_msg}")
                            # Break out of loop since job failed
                            break
                        else:
                            # Still processing
                            job.status = "processing"
                            job.progress = min(50 + (attempt * 2), 95)  # Gradually increase progress
                            job.save()
                            print(f"⏳ Job {job_id} still processing... (train_status: {train_status}, attempt {attempt + 1}/{max_attempts})")
                    else:
                        # Group not found yet, might still be processing
                        job.status = "processing"
                        job.progress = min(50 + (attempt * 2), 95)
                        job.save()
                        print(f"⏳ Job {job_id} group not found yet, still processing... (attempt {attempt + 1}/{max_attempts})")
                else:
                    print(f"⚠️ Status check failed: {status_response.status_code} - {status_response.text[:200]}")
            else:
                # Use photo/generate status endpoint for prompt-based jobs
                status_url = f"https://api.heygen.com/v2/photo_avatar/generation/{generation_id}"
                headers = {
                    'accept': 'application/json',
                    'x-api-key': api_key
                }
                
                status_response = requests.get(status_url, headers=headers, timeout=30)
                
                if status_response.status_code == 200:
                    status_data = status_response.json()
                    print(f"📊 Status check {attempt + 1}: {status_data}")
                    
                    # Extract status from response - check multiple possible locations
                    generation_status = (
                        status_data.get('data', {}).get('status') or 
                        status_data.get('status') or 
                        status_data.get('data', {}).get('generation_status')
                    )
                    avatar_url = status_data.get('data', {}).get('url') or status_data.get('data', {}).get('avatar_url') or status_data.get('url')
                    # Also check for image_url_list - use first image if available
                    image_url_list = status_data.get('data', {}).get('image_url_list') or []
                    if image_url_list and not avatar_url:
                        avatar_url = image_url_list[0] if image_url_list else None
                    
                    # Check for completion - also check if image_url_list exists as that indicates completion
                    is_completed = (
                        generation_status == 'completed' or 
                        generation_status == 'done' or 
                        generation_status == 'success' or
                        (image_url_list and len(image_url_list) > 0) or
                        (avatar_url is not None)
                    )
                    
                    if is_completed:
                        # Avatar generation completed - but don't mark as completed yet
                        # Wait until avatar_group creation succeeds (if needed)
                        try:
                            # Use database-level locking to prevent concurrent uploads/avatar_group creation
                            # Lock the job record to ensure only one thread processes the upload
                            should_upload = False
                            with transaction.atomic():
                                # Re-fetch job with lock to prevent race conditions
                                locked_job = AvatarGenerationJob.objects.select_for_update().get(job_id=job_id)
                                
                                # Check if job is already completed or has error - if so, skip processing
                                if locked_job.status in ['completed', 'error']:
                                    print(f"ℹ️ Job {job_id} is already {locked_job.status}, skipping upload/avatar_group creation")
                                    break
                                
                                # Save avatar URL and avatar_id first (while we have the lock)
                                if avatar_url:
                                    locked_job.avatar_url = avatar_url[:2000] if len(avatar_url) > 2000 else avatar_url
                                
                                # Extract and save avatar_id if available (truncate to 255 chars)
                                avatar_id_value = status_data.get('data', {}).get('avatar_id') or status_data.get('avatar_id')
                                if avatar_id_value:
                                    locked_job.avatar_id = str(avatar_id_value)[:255]
                                
                                # Check if image_key is already set (upload already done) or is being uploaded
                                # Only one thread will pass this check due to the lock
                                marker_cleared = False
                                if locked_job.image_key:
                                    # Check if it's a processing marker or a real image_key
                                    if locked_job.image_key.startswith('UPLOADING_'):
                                        # Check if processing marker is stale (older than 5 minutes)
                                        try:
                                            marker_timestamp = int(locked_job.image_key.split('_')[1])
                                            current_timestamp = int(time.time() * 1000)
                                            if current_timestamp - marker_timestamp > 300000:  # 5 minutes in milliseconds
                                                print(f"⚠️ Job {job_id} processing marker is stale (older than 5 minutes), clearing and retrying upload")
                                                locked_job.image_key = None
                                                locked_job.save(update_fields=['image_key'])
                                                marker_cleared = True
                                                # Will fall through to upload logic below
                                            else:
                                                print(f"ℹ️ Job {job_id} is already being uploaded by another thread, skipping")
                                                avatar_group_creation_needed = False
                                                job = locked_job
                                        except (ValueError, IndexError):
                                            # Invalid marker format, clear it and retry
                                            print(f"⚠️ Job {job_id} has invalid processing marker, clearing and retrying upload")
                                            locked_job.image_key = None
                                            locked_job.save(update_fields=['image_key'])
                                            marker_cleared = True
                                            # Will fall through to upload logic below
                                    else:
                                        print(f"ℹ️ Job {job_id} already has image_key: {locked_job.image_key}, skipping upload")
                                        avatar_group_creation_needed = False
                                        job = locked_job
                                
                                # If marker was cleared or image_key is None, proceed with upload
                                if (not locked_job.image_key or marker_cleared) and avatar_url:
                                    # This thread will handle the upload - mark as processing IMMEDIATELY while lock is held
                                    # This prevents other threads from starting upload
                                    processing_marker = f"UPLOADING_{int(time.time() * 1000)}"
                                    locked_job.image_key = processing_marker
                                    locked_job.save(update_fields=['image_key', 'avatar_url', 'avatar_id'])
                                    should_upload = True
                                    job = locked_job
                                    print(f"🔒 Job {job_id} marked as uploading, proceeding with upload...")
                                else:
                                    # No avatar_url or already processed
                                    avatar_group_creation_needed = False
                                    job = locked_job
                            
                            # Do upload/avatar_group creation OUTSIDE the transaction to avoid holding lock during HTTP requests
                            if should_upload:
                                # Need to upload and create avatar_group
                                avatar_group_creation_needed = True
                                print(f"🔄 Uploading generated avatar to HeyGen to save to account...")
                                try:
                                    # Download the image from avatar_url
                                    img_response = requests.get(avatar_url, timeout=30)
                                    if img_response.status_code == 200:
                                        # Determine content type from URL or response headers
                                        content_type = img_response.headers.get('Content-Type', 'image/jpeg')
                                        if not content_type.startswith('image/'):
                                            # Try to determine from URL extension
                                            if avatar_url.lower().endswith('.png'):
                                                content_type = 'image/png'
                                            elif avatar_url.lower().endswith('.jpg') or avatar_url.lower().endswith('.jpeg'):
                                                content_type = 'image/jpeg'
                                            else:
                                                content_type = 'image/jpeg'  # Default
                                        
                                        # Upload to HeyGen using the asset upload endpoint
                                        upload_url = "https://upload.heygen.com/v1/asset"
                                        upload_headers = {
                                            'Content-Type': content_type,
                                            'x-api-key': api_key
                                        }
                                        
                                        # Upload the image data directly (binary)
                                        upload_response = requests.post(
                                            upload_url,
                                            headers=upload_headers,
                                            data=img_response.content,
                                            timeout=30
                                        )
                                        
                                        if upload_response.status_code == 200:
                                            upload_data = upload_response.json()
                                            image_key = (
                                                upload_data.get('image_key') or
                                                upload_data.get('data', {}).get('image_key') or
                                                upload_data.get('data', {}).get('key')
                                            )
                                            
                                            if image_key:
                                                print(f"✅ Successfully uploaded avatar to HeyGen! Image key: {image_key}")
                                                
                                                # Replace processing marker with real image_key
                                                # Use lock to atomically update image_key
                                                with transaction.atomic():
                                                    job_check = AvatarGenerationJob.objects.select_for_update().get(job_id=job_id)
                                                    # Check if it's still our processing marker or was already set by another thread
                                                    if job_check.image_key and job_check.image_key.startswith('UPLOADING_'):
                                                        # This is our processing marker, replace it with real image_key
                                                        job_check.image_key = image_key
                                                        job_check.save(update_fields=['image_key'])
                                                        job = job_check
                                                        print(f"✅ Job {job_id} image_key updated from processing marker to: {image_key}")
                                                    elif job_check.image_key and not job_check.image_key.startswith('UPLOADING_'):
                                                        # Another thread already set a real image_key (shouldn't happen, but handle it)
                                                        print(f"ℹ️ Job {job_id} image_key was already set by another thread: {job_check.image_key}")
                                                        image_key = job_check.image_key  # Use the existing one
                                                        job = job_check
                                                        avatar_group_creation_needed = False  # Skip avatar_group creation
                                                    else:
                                                        # No image_key set (unlikely, but handle it)
                                                        job_check.image_key = image_key
                                                        job_check.save(update_fields=['image_key'])
                                                        job = job_check
                                                        print(f"✅ Job {job_id} image_key set to: {image_key}")
                                                
                                                # Create avatar_group only if we just set the image_key
                                                if avatar_group_creation_needed:
                                                    # Create avatar_group using the uploaded image_key
                                                    avatar_group_url = "https://api.heygen.com/v2/photo_avatar/avatar_group/create"
                                                    avatar_group_headers = {
                                                        'accept': 'application/json',
                                                        'content-type': 'application/json',
                                                        'x-api-key': api_key
                                                    }
                                                    avatar_group_payload = {
                                                        "name": job.name if job.name else "Generated Avatar",
                                                        "image_key": image_key
                                                    }
                                                    
                                                    print(f"📤 Creating avatar_group with payload: {avatar_group_payload}")
                                                    avatar_group_response = requests.post(
                                                        avatar_group_url,
                                                        headers=avatar_group_headers,
                                                        json=avatar_group_payload,
                                                        timeout=30
                                                    )
                                                    
                                                    if avatar_group_response.status_code == 200:
                                                        avatar_group_data = avatar_group_response.json()
                                                        print(f"📥 Avatar group response: {avatar_group_data}")
                                                        group_id = (
                                                            avatar_group_data.get('data', {}).get('id') or
                                                            avatar_group_data.get('data', {}).get('group_id') or
                                                            avatar_group_data.get('id') or
                                                            avatar_group_data.get('group_id')
                                                        )
                                                        
                                                        if group_id:
                                                            print(f"✅ Successfully created avatar_group with ID: {group_id} (saved to account)")
                                                            # Update job with avatar_group details
                                                            with transaction.atomic():
                                                                job_update = AvatarGenerationJob.objects.select_for_update().get(job_id=job_id)
                                                                job_update.generation_id = str(group_id)
                                                                job_update.save(update_fields=['generation_id'])
                                                                job = job_update
                                                            # Keep the original avatar_url for display
                                                            avatar_group_creation_needed = False  # Successfully created
                                                        else:
                                                            print(f"⚠️ Failed to get group_id from avatar_group response: {avatar_group_response.text[:200]}")
                                                            # Don't mark as error if group_id is missing - might still be processing
                                                            # Keep avatar_group_creation_needed = True to continue checking
                                                    else:
                                                        # Avatar group creation failed - extract error message and mark job as error
                                                        try:
                                                            error_response = avatar_group_response.json() if avatar_group_response.text else {}
                                                            error_data = error_response.get('error', {}) or error_response.get('data', {})
                                                            error_msg = (
                                                                error_data.get('message') or 
                                                                error_response.get('message') or 
                                                                f"Failed to create avatar_group: {avatar_group_response.status_code}"
                                                            )
                                                        except:
                                                            error_msg = f"Failed to create avatar_group: {avatar_group_response.status_code} - {avatar_group_response.text[:200]}"
                                                        
                                                        print(f"❌ Failed to create avatar_group: {avatar_group_response.status_code} - {error_msg}")
                                                        
                                                        # Mark job as error since avatar_group creation failed
                                                        # But preserve avatar_url so user can still access the generated avatar
                                                        with transaction.atomic():
                                                            job_error = AvatarGenerationJob.objects.select_for_update().get(job_id=job_id)
                                                            job_error.status = "error"
                                                            job_error.progress = 0
                                                            job_error.completed_at = timezone.now()
                                                            job_error.error_message = error_msg
                                                            # Ensure avatar_url is preserved (it should already be set, but be explicit)
                                                            if avatar_url and not job_error.avatar_url:
                                                                job_error.avatar_url = avatar_url[:2000] if len(avatar_url) > 2000 else avatar_url
                                                            job_error.save()
                                                            job = job_error
                                                        print(f"❌ Job {job_id} marked as error: {error_msg} (avatar_url preserved: {bool(job.avatar_url)})")
                                                        avatar_group_creation_needed = False
                                            else:
                                                print(f"⚠️ No image_key in upload response: {upload_data}")
                                                # Upload failed - clear processing marker
                                                with transaction.atomic():
                                                    job_fail = AvatarGenerationJob.objects.select_for_update().get(job_id=job_id)
                                                    if job_fail.image_key and job_fail.image_key.startswith('UPLOADING_'):
                                                        job_fail.image_key = None  # Clear processing marker
                                                        job_fail.save(update_fields=['image_key'])
                                                # Upload failed but we have avatar_url - mark as completed anyway
                                                avatar_group_creation_needed = False
                                        else:
                                            print(f"⚠️ Failed to upload image to HeyGen: {upload_response.status_code} - {upload_response.text[:200]}")
                                            # Upload failed - clear processing marker
                                            with transaction.atomic():
                                                job_fail = AvatarGenerationJob.objects.select_for_update().get(job_id=job_id)
                                                if job_fail.image_key and job_fail.image_key.startswith('UPLOADING_'):
                                                    job_fail.image_key = None  # Clear processing marker
                                                    job_fail.save(update_fields=['image_key'])
                                            # Upload failed but we have avatar_url - mark as completed anyway
                                            avatar_group_creation_needed = False
                                    else:
                                        print(f"⚠️ Failed to download image from {avatar_url}: {img_response.status_code}")
                                        # Download failed - clear processing marker
                                        with transaction.atomic():
                                            job_fail = AvatarGenerationJob.objects.select_for_update().get(job_id=job_id)
                                            if job_fail.image_key and job_fail.image_key.startswith('UPLOADING_'):
                                                job_fail.image_key = None  # Clear processing marker
                                                job_fail.save(update_fields=['image_key'])
                                        # Download failed but we have avatar_url - mark as completed anyway
                                        avatar_group_creation_needed = False
                                except Exception as upload_error:
                                    print(f"⚠️ Error uploading generated avatar to HeyGen: {str(upload_error)}")
                                    import traceback
                                    print(traceback.format_exc())
                                    # Upload error - clear processing marker
                                    try:
                                        with transaction.atomic():
                                            job_fail = AvatarGenerationJob.objects.select_for_update().get(job_id=job_id)
                                            if job_fail.image_key and job_fail.image_key.startswith('UPLOADING_'):
                                                job_fail.image_key = None  # Clear processing marker
                                                job_fail.save(update_fields=['image_key'])
                                    except Exception as clear_error:
                                        print(f"⚠️ Failed to clear processing marker: {str(clear_error)}")
                                    # Upload error but we have avatar_url - mark as completed anyway
                                    avatar_group_creation_needed = False
                            
                            # Mark as completed only if avatar_group creation succeeded or wasn't needed
                            # Re-fetch job to get latest state
                            job = AvatarGenerationJob.objects.get(job_id=job_id)
                            if not avatar_group_creation_needed and job.status != "error":
                                job.status = "completed"
                                job.progress = 100
                                job.completed_at = timezone.now()
                                job.save()
                                print(f"✅ Job {job_id} completed successfully! Avatar URL: {avatar_url}")
                                # Break out of the loop since job is completed
                                break
                            elif job.status == "error":
                                # Already marked as error, break out
                                break
                            # Otherwise, continue checking (avatar_group_creation_needed is still True)
                        except Exception as save_error:
                            print(f"❌ Error saving completed job {job_id}: {str(save_error)}")
                            import traceback
                            print(traceback.format_exc())
                            # Try to save with minimal fields to at least mark it as completed
                            try:
                                job.status = "completed"
                                job.progress = 100
                                job.completed_at = timezone.now()
                                if avatar_url:
                                    job.avatar_url = avatar_url[:500]  # Truncate to safe length
                                job.save(update_fields=['status', 'progress', 'completed_at', 'avatar_url'])
                                print(f"✅ Job {job_id} marked as completed (with truncated URL)")
                            except Exception as retry_error:
                                print(f"❌ Failed to save job even with minimal fields: {str(retry_error)}")
                            # Break out of loop even if save failed
                            break
                    elif generation_status == 'failed' or generation_status == 'error':
                        # Generation failed
                        error_msg = status_data.get('data', {}).get('message') or status_data.get('message', 'Avatar generation failed')
                        job.status = "error"
                        job.progress = 0
                        job.completed_at = timezone.now()
                        job.error_message = error_msg
                        job.save()
                        print(f"❌ Job {job_id} failed: {error_msg}")
                        # Break out of loop since job failed
                        break
                    elif generation_status == 'pending' or generation_status == 'processing':
                        # Still processing
                        job.status = "processing"
                        job.progress = min(50 + (attempt * 2), 95)  # Gradually increase progress
                        job.save()
                        print(f"⏳ Job {job_id} still processing... (attempt {attempt + 1}/{max_attempts})")
            
            attempt += 1
            time.sleep(5)  # Wait 5 seconds before next check
            
        except AvatarGenerationJob.DoesNotExist:
            print(f"Job {job_id} not found")
            return
        except Exception as e:
            print(f"Error checking status for job {job_id}: {str(e)}")
            import traceback
            print(traceback.format_exc())
            # Check if it's a database error - if so, try to continue with next attempt
            # Otherwise, increment and continue
            attempt += 1
            if attempt >= max_attempts:
                break
            time.sleep(5)
    
    # Timeout - mark as error
    try:
        job = AvatarGenerationJob.objects.get(job_id=job_id)
        job.status = "error"
        job.error_message = "Avatar generation failed"
        job.completed_at = timezone.now()
        job.save()
        print(f"⏱️ Job {job_id} timed out")
    except AvatarGenerationJob.DoesNotExist:
        pass


class AvatarGenerationView(APIView):
    parser_classes = [MultiPartParser, FormParser]
    
    def post(self, request):
        """Queue an avatar generation job using HeyGen API"""
        try:
            # Get current user from JWT token
            user = get_current_user(request)
            if not user:
                return Response(
                    ResponseInfo.error("Authentication required"),
                    status=status.HTTP_401_UNAUTHORIZED
                )
            
            # Check if this is a video generation request
            video_payload_str = request.data.get('video_payload', '').strip()
            is_video_generation = bool(video_payload_str)
            
            if is_video_generation:
                # Handle video generation
                import json
                try:
                    video_payload = json.loads(video_payload_str)
                except json.JSONDecodeError as e:
                    return Response(
                        ResponseInfo.error(f"Invalid video payload JSON: {str(e)}"),
                        status=status.HTTP_400_BAD_REQUEST
                    )
                
                # Check API version and validate accordingly
                api_version = video_payload.get('api_version', 'v4')
                
                if api_version == 'v2':
                    # V2 API validation
                    if not video_payload.get('voice_id'):
                        return Response(
                            ResponseInfo.error("voice_id is required for video generation"),
                            status=status.HTTP_400_BAD_REQUEST
                        )
                    
                    if not video_payload.get('input_text'):
                        return Response(
                            ResponseInfo.error("input_text is required for video generation"),
                            status=status.HTTP_400_BAD_REQUEST
                        )
                    
                    # Check for avatar_id or talking_photo_id based on type
                    avatar_type = video_payload.get('type', 'avatar')
                    if avatar_type == 'avatar' and not video_payload.get('avatar_id'):
                        return Response(
                            ResponseInfo.error("avatar_id is required for video generation"),
                            status=status.HTTP_400_BAD_REQUEST
                        )
                    elif avatar_type == 'talking_photo' and not video_payload.get('talking_photo_id'):
                        return Response(
                            ResponseInfo.error("talking_photo_id is required for video generation"),
                            status=status.HTTP_400_BAD_REQUEST
                        )
                    
                    # Validate text display fields
                    text_type = video_payload.get('text_type', 'text')
                    if text_type != 'text':
                        return Response(
                            ResponseInfo.error("text_type must be 'text'"),
                            status=status.HTTP_400_BAD_REQUEST
                        )
                    
                    text_content = video_payload.get('text_content', '')
                    if not text_content or len(str(text_content)) == 0:
                        return Response(
                            ResponseInfo.error("text_content is required for video generation"),
                            status=status.HTTP_400_BAD_REQUEST
                        )
                    
                    # Validate line_height
                    line_height = video_payload.get('line_height')
                    if line_height is None:
                        return Response(
                            ResponseInfo.error("line_height is required for video generation"),
                            status=status.HTTP_400_BAD_REQUEST
                        )
                    try:
                        line_height_float = float(line_height)
                        if line_height_float <= 0.0:
                            return Response(
                                ResponseInfo.error("line_height must be greater than 0.0"),
                                status=status.HTTP_400_BAD_REQUEST
                            )
                    except (ValueError, TypeError):
                        return Response(
                            ResponseInfo.error("line_height must be a valid number greater than 0.0"),
                            status=status.HTTP_400_BAD_REQUEST
                        )
                else:
                    # V4 API validation
                    if not video_payload.get('image_key'):
                        return Response(
                            ResponseInfo.error("image_key is required for video generation"),
                            status=status.HTTP_400_BAD_REQUEST
                        )
                    
                    if not video_payload.get('script'):
                        return Response(
                            ResponseInfo.error("script is required for video generation"),
                            status=status.HTTP_400_BAD_REQUEST
                        )
                    
                    if not video_payload.get('voice_id'):
                        return Response(
                            ResponseInfo.error("voice_id is required for video generation"),
                            status=status.HTTP_400_BAD_REQUEST
                        )
                    
                    if not video_payload.get('video_orientation'):
                        return Response(
                            ResponseInfo.error("video_orientation is required for video generation"),
                            status=status.HTTP_400_BAD_REQUEST
                        )
                
                # Get HeyGen API key
                heygen_api_key = os.getenv('HEYGEN_API_KEY')
                if not heygen_api_key:
                    return Response(
                        ResponseInfo.error("HeyGen API key not configured"),
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR
                    )
                
                # Create a unique job ID
                job_id = str(uuid.uuid4())
                
                # Create job in database for video generation
                job = AvatarGenerationJob.objects.create(
                    job_id=job_id,
                    user=user,
                    prompt="",  # Not used for video generation
                    name=video_payload.get('video_title', 'Avatar Video')[:100],
                    status="queued",
                    progress=0,
                    provider="heygen_video"
                )
                
                # Start background video generation
                thread = threading.Thread(
                    target=self._process_heygen_video_generation,
                    args=(job_id, video_payload, heygen_api_key)
                )
                thread.daemon = True
                thread.start()
                
                # Return job info immediately
                response_data = {
                    "job_id": job_id,
                    "status": "queued",
                    "message": "Avatar video generation job queued successfully",
                    "created_at": job.created_at.isoformat(),
                    "check_status_url": f"/api/v1/avatar-status/{job_id}/"
                }
                
                return Response(
                    ResponseInfo.success(response_data, "Avatar video generation job started"),
                    status=status.HTTP_202_ACCEPTED
                )
            
            # Original avatar image generation logic
            # Extract data from request
            prompt = request.data.get('prompt', '').strip()  # This is the appearance/description field
            name = request.data.get('name', '').strip()
            age = request.data.get('age', '').strip()
            gender = request.data.get('gender', '').strip()
            ethnicity = request.data.get('ethnicity', '').strip()
            orientation = request.data.get('orientation', '').strip()
            pose = request.data.get('pose', '').strip()
            style = request.data.get('style', '').strip()
            
            # Prepare reference images if any (same pattern as image_generation_view - optional)
            reference_images = []
            for key, file in request.FILES.items():
                if key.startswith('reference_image_'):
                    try:
                        if not file.content_type.startswith('image/'):
                            continue
                        file_content = file.read()
                        base64_image = base64.b64encode(file_content).decode('utf-8')
                        reference_images.append({
                            "image": base64_image,
                            "image_type": file.content_type,
                            "filename": file.name
                        })
                    except Exception as e:
                        print(f"Error processing reference image {key}: {str(e)}")
                        import traceback
                        print(traceback.format_exc())
                        continue
            
            # Validate: prompt is required only if no reference images are provided
            if not reference_images:
                if not prompt:
                    return Response(
                        ResponseInfo.error("Either a prompt (appearance/description) or a reference image is required for avatar generation"),
                        status=status.HTTP_400_BAD_REQUEST
                    )
                print("📝 No reference images provided - creating avatar from prompt only")
            else:
                print(f"🖼️  {len(reference_images)} reference image(s) detected - will be used for avatar generation")
            
            # Get HeyGen API key from environment
            heygen_api_key = os.getenv('HEYGEN_API_KEY')
            if not heygen_api_key:
                return Response(
                    ResponseInfo.error("HeyGen API key not configured"),
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
            
            # Create a unique job ID
            job_id = str(uuid.uuid4())
            
            # Use provided name or generate from prompt or default (truncate if too long)
            if name:
                avatar_name = name
            elif prompt:
                avatar_name = prompt[:100] if len(prompt) > 100 else prompt
            else:
                avatar_name = "Generated Avatar"  # Default name when using only reference image
            
            # Create job in database
            # Use empty string for prompt if only reference images are provided
            job_prompt = prompt if prompt else ""
            job = AvatarGenerationJob.objects.create(
                job_id=job_id,
                user=user,
                prompt=job_prompt,
                name=avatar_name,
                age=age if age else None,
                gender=gender if gender else None,
                ethnicity=ethnicity if ethnicity else None,
                orientation=orientation if orientation else None,
                pose=pose if pose else None,
                style=style if style else None,
                status="queued",
                progress=0
            )
            
            # Store reference images in database (same pattern as image_generation_view)
            for ref_img in reference_images:
                AvatarReferenceImage.objects.create(
                    job=job,
                    image_data=ref_img["image"],
                    filename=ref_img.get("filename", "reference.jpg"),
                    content_type=ref_img.get("image_type", "image/jpeg")
                )
            
            # Start background processing (pass only job_id and api_key, retrieve images from DB)
            thread = threading.Thread(
                target=self._process_heygen_generation,
                args=(job_id, heygen_api_key)
            )
            thread.daemon = True
            thread.start()
            
            # Return job info immediately (include image_key if reference image was uploaded)
            response_data = {
                "job_id": job_id,
                "status": "queued",
                "message": "Avatar generation job queued successfully",
                "prompt": prompt,
                "name": avatar_name,
                "created_at": job.created_at.isoformat(),
                "check_status_url": f"/api/v1/avatar-status/{job_id}/"
            }
            
            # Add image_key if reference image exists (will be set after upload)
            if job.image_key:
                response_data["image_key"] = job.image_key
            
            return Response(
                ResponseInfo.success(response_data, "Avatar generation job started"),
                status=status.HTTP_202_ACCEPTED
            )
            
        except Exception as e:
            print(f"❌ Error starting avatar generation: {str(e)}")
            import traceback
            print(traceback.format_exc())
            return Response(
                ResponseInfo.error(f"Failed to start avatar generation: {str(e)}"),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    def _process_heygen_generation(self, job_id, api_key):
        """Process avatar generation using HeyGen API (same pattern as image_generation_view)"""
        try:
            # Get job from database
            try:
                job = AvatarGenerationJob.objects.get(job_id=job_id)
            except AvatarGenerationJob.DoesNotExist:
                print(f"Job {job_id} not found in database")
                return
            
            print(f"🚀 Starting HeyGen avatar generation for job {job_id}")
            print(f"🔑 API Key: {'Present' if api_key else 'Missing'}")
            
            # Update status to processing
            job.status = "processing"
            job.progress = 10
            job.started_at = timezone.now()
            job.save()
            
            # Get reference images from database (same pattern as image_generation_view)
            reference_images = []
            for ref_img in job.reference_images.all():
                reference_images.append({
                    "image": ref_img.image_data,
                    "filename": ref_img.filename,
                    "content_type": ref_img.content_type
                })
            
            job.progress = 40
            job.save()
            
            # Determine which endpoint and payload to use based on reference image presence
            if reference_images:
                # Step 1: Upload reference image to HeyGen to get image_key
                print(f"🖼️  Found {len(reference_images)} reference image(s) - uploading to HeyGen first...")
                reference_image = reference_images[0]
                image_name = reference_image['filename'] or "reference.jpg"
                
                # Decode base64 image data
                try:
                    file_content = base64.b64decode(reference_image["image"])
                except Exception as e:
                    error_msg = f"Failed to decode reference image: {str(e)}"
                    print(f"❌ {error_msg}")
                    job.status = "error"
                    job.progress = 0
                    job.completed_at = timezone.now()
                    job.error_message = error_msg
                    job.save()
                    return
                
                # Use correct HeyGen upload endpoint: https://upload.heygen.com/v1/asset
                upload_url = "https://upload.heygen.com/v1/asset"
                
                # Determine Content-Type from image content type
                content_type = reference_image.get('content_type', 'image/jpeg')
                if not content_type or not content_type.startswith('image/'):
                    content_type = 'image/jpeg'
                
                upload_headers = {
                    'Content-Type': content_type,
                    'x-api-key': api_key
                }
                
                # Upload image to HeyGen (using data parameter with file content)
                try:
                    print(f"📤 Uploading image to HeyGen: {upload_url}")
                    upload_response = requests.post(
                        upload_url, 
                        headers=upload_headers, 
                        data=file_content, 
                        timeout=30
                    )
                    
                    if upload_response.status_code == 200:
                        upload_data = upload_response.json()
                        print(f"📥 Upload response: {upload_data}")
                        
                        # Extract image_key from response
                        image_key = (
                            upload_data.get('image_key') or
                            upload_data.get('data', {}).get('image_key') or
                            upload_data.get('data', {}).get('key')
                        )
                        
                        if image_key:
                            print(f"✅ Image uploaded successfully!")
                            print(f"🔑 Received image_key: {image_key}")
                            job.image_key = image_key
                            job.save()
                        else:
                            error_msg = f"Upload succeeded but no image_key found in response: {upload_data}"
                            print(f"❌ {error_msg}")
                            job.status = "error"
                            job.progress = 0
                            job.completed_at = timezone.now()
                            job.error_message = error_msg
                            job.save()
                            return
                    else:
                        error_msg = f"Failed to upload image: {upload_response.status_code} - {upload_response.text[:500]}"
                        print(f"❌ {error_msg}")
                        job.status = "error"
                        job.progress = 0
                        job.completed_at = timezone.now()
                        job.error_message = error_msg
                        job.save()
                        return
                        
                except Exception as e:
                    error_msg = f"Error uploading image to HeyGen: {str(e)}"
                    print(f"❌ {error_msg}")
                    job.status = "error"
                    job.progress = 0
                    job.completed_at = timezone.now()
                    job.error_message = error_msg
                    job.save()
                    return
                
                # Step 2: Use avatar_group/create endpoint with the uploaded image_key
                print(f"📝 Using reference image: {image_name}")
                create_avatar_url = "https://api.heygen.com/v2/photo_avatar/avatar_group/create"
                
                # Build payload for avatar_group/create endpoint
                # Use job.name if provided, otherwise use image_name
                avatar_name = job.name if job.name else image_name
                payload = {
                    "name": avatar_name,
                    "image_key": image_key
                }
                
            else:
                # Use photo/generate endpoint for prompt-based generation
                print("📝 No reference images provided - using photo/generate endpoint (prompt-based)")
                create_avatar_url = "https://api.heygen.com/v2/photo_avatar/photo/generate"
                
                # Build payload with all required HeyGen API parameters  
                payload = {
                    "appearance": job.prompt[:1000] if job.prompt else ""  # Max 1000 characters
                }
                
                # Add optional fields if provided  
                if job.name:
                    payload["name"] = job.name
                if job.age:
                    payload["age"] = job.age
                if job.gender:
                    payload["gender"] = job.gender
                if job.ethnicity:
                    payload["ethnicity"] = job.ethnicity
                if job.orientation:
                    payload["orientation"] = job.orientation
                if job.pose:
                    payload["pose"] = job.pose
                if job.style:
                    payload["style"] = job.style
            
            job.progress = 60
            job.save()
            
            print(f"🎭 Creating avatar with HeyGen...")
            print(f"🔗 Create Avatar URL: {create_avatar_url}")
            create_headers = {
                'accept': 'application/json',
                'content-type': 'application/json',
                'x-api-key': api_key
            }
            
            print(f"📤 Payload: {payload}")
            
            try:
                create_response = requests.post(
                    create_avatar_url,
                    headers=create_headers,
                    json=payload,
                    timeout=30
                )
            except requests.exceptions.ConnectionError as e:
                error_msg = f"Connection error: Failed to connect to HeyGen API."
                print(f"❌ {error_msg}")
                print(f"💡 Attempted URL: {create_avatar_url}")
                print(f"💡 Error details: {str(e)}")
                job.status = "error"
                job.progress = 0
                job.completed_at = timezone.now()
                job.error_message = f"Connection failed: {str(e)}"
                job.save()
                return
            except requests.exceptions.RequestException as e:
                error_msg = f"Request error: {str(e)}"
                print(f"❌ {error_msg}")
                print(f"💡 Attempted URL: {create_avatar_url}")
                job.status = "error"
                job.progress = 0
                job.completed_at = timezone.now()
                job.error_message = error_msg
                job.save()
                return
            
            if create_response.status_code != 200:
                error_msg = f"Failed to create avatar: {create_response.status_code} - {create_response.text[:500]}"
                print(f"❌ {error_msg}")
                print(f"💡 Used URL: {create_avatar_url}")
                print(f"💡 Response: {create_response.text[:500]}")
                job.status = "error"
                job.progress = 0
                job.completed_at = timezone.now()
                job.error_message = error_msg
                job.save()
                return
            
            create_data = create_response.json()
            print(f"📥 HeyGen API Response: {create_data}")
            
            # Extract generation_id/group_id from response
            # For avatar_group/create, it returns 'id' or 'group_id'
            # For photo/generate, it returns 'generation_id'
            generation_id = None
            if "avatar_group" in create_avatar_url:
                # avatar_group/create returns 'id' or 'group_id'
                generation_id = (
                    create_data.get('data', {}).get('id') or
                    create_data.get('data', {}).get('group_id') or
                    create_data.get('id') or
                    create_data.get('group_id')
                )
            else:
                # photo/generate returns 'generation_id'
                generation_id = (
                    create_data.get('data', {}).get('generation_id') or
                    create_data.get('generation_id')
                )
            
            if not generation_id:
                error_msg = f"Generation ID/Group ID not found in HeyGen response: {create_data}"
                print(f"❌ {error_msg}")
                job.status = "error"
                job.progress = 0
                job.completed_at = timezone.now()
                job.error_message = error_msg
                job.save()
                return
            
            print(f"✅ Avatar generation started. Generation ID: {generation_id}")
            
            # Save generation_id and set status to processing (will check status periodically)
            job.generation_id = str(generation_id)
            job.status = "processing"
            job.progress = 50
            job.save()
            
            # Start polling for status in background thread
            status_thread = threading.Thread(
                target=check_avatar_generation_status,
                args=(job_id, generation_id, api_key)
            )
            status_thread.daemon = True
            status_thread.start()
            
        except Exception as e:
            print(f"❌ Job {job_id} failed: {str(e)}")
            import traceback
            print(traceback.format_exc())
            # Update job as failed
            try:
                job = AvatarGenerationJob.objects.get(job_id=job_id)
                job.status = "error"
                job.progress = 0
                job.completed_at = timezone.now()
                job.error_message = str(e)
                job.save()
            except AvatarGenerationJob.DoesNotExist:
                print(f"Job {job_id} not found for error update")

    def _process_heygen_video_generation(self, job_id, video_payload, api_key):
        """Process avatar video generation using HeyGen API"""
        try:
            # Get job from database
            try:
                job = AvatarGenerationJob.objects.get(job_id=job_id)
            except AvatarGenerationJob.DoesNotExist:
                print(f"Job {job_id} not found in database")
                return
            
            print(f"🎬 Starting HeyGen video generation for job {job_id}")
            print(f"🔑 API Key: {'Present' if api_key else 'Missing'}")
            
            # Update status to processing
            job.status = "processing"
            job.progress = 10
            job.started_at = timezone.now()
            job.save()
            
            # Check API version from payload
            api_version = video_payload.get('api_version', 'v4')
            
            if api_version == 'v2':
                # V2 API endpoint
                video_url = "https://api.heygen.com/v2/video/generate"
                headers = {
                    "accept": "application/json",
                    "content-type": "application/json",
                    "x-api-key": api_key
                }
                
                # Get dimension values
                dimension_width = video_payload.get('dimension', {}).get('width', 1280) if isinstance(video_payload.get('dimension'), dict) else video_payload.get('dimension_width', 1280)
                dimension_height = video_payload.get('dimension', {}).get('height', 720) if isinstance(video_payload.get('dimension'), dict) else video_payload.get('dimension_height', 720)
                
                # Get avatar type and ID
                avatar_type = video_payload.get('type', 'avatar')
                avatar_id = None
                if avatar_type == 'avatar' and video_payload.get('avatar_id'):
                    avatar_id = video_payload.get('avatar_id')
                elif avatar_type == 'talking_photo' and video_payload.get('talking_photo_id'):
                    avatar_id = video_payload.get('talking_photo_id')
                
                # Get voice settings
                voice_id = video_payload.get('voice_id', '')
                input_text = video_payload.get('input_text', '')
                
                # Get speed (0.5 to 1.5, default 1)
                speed_value = "1"
                if video_payload.get('speed'):
                    speed_float = float(video_payload.get('speed'))
                    speed_float = max(0.5, min(1.5, speed_float))
                    speed_value = str(speed_float)
                
                # Build character object
                character_obj = {
                    "type": avatar_type,
                    "scale": 1,
                    "avatar_style": "normal",
                    "talking_style": "stable"
                }
                
                # Add avatar_id to character if available
                if avatar_id:
                    if avatar_type == 'avatar':
                        character_obj["avatar_id"] = avatar_id
                    elif avatar_type == 'talking_photo':
                        character_obj["talking_photo_id"] = avatar_id
                
                # Build voice object - input_text should be directly in voice object, not nested
                voice_obj = {
                    "type": "text",
                    "speed": speed_value,
                    "pitch": "0",
                    "duration": "1"
                }
                
                # Add voice_id if provided
                if voice_id:
                    voice_obj["voice_id"] = voice_id
                
                # Add input_text directly to voice object (required by API)
                # Ensure input_text is a string
                input_text_value = str(input_text) if input_text else ""
                voice_obj["input_text"] = input_text_value
                
                # Get text display settings (for on-screen text)
                text_type = video_payload.get('text_type', 'text')
                text_content = video_payload.get('text_content', '')
                line_height = video_payload.get('line_height', 1.0)
                
                # Validate line_height
                try:
                    line_height_float = float(line_height)
                    if line_height_float <= 0.0:
                        line_height_float = 1.0  # Default to 1.0 if invalid
                except (ValueError, TypeError):
                    line_height_float = 1.0  # Default to 1.0 if invalid
                
                # Build text object for on-screen display
                text_obj = {
                    "type": text_type if text_type == "text" else "text",  # Ensure it's "text"
                    "text": text_content,
                    "line_height": line_height_float
                }
                
                # Build video_inputs array
                video_input = {
                    "character": character_obj,
                    "voice": voice_obj,
                    "background": {
                        "type": "color",
                        "value": "#FFFFFF",
                        "play_style": "freeze",
                        "fit": "cover"
                    },
                    "text": text_obj
                }
                
                # Build final payload
                new_payload = {
                    "caption": "false",
                    "video_inputs": [video_input],
                    "dimension": {
                        "width": dimension_width,
                        "height": dimension_height
                    }
                }
                
                # Add title if provided
                if video_payload.get('title'):
                    new_payload["title"] = video_payload.get('title')
                
                print(f"📤 Calling HeyGen video generation API (v2)...")
            else:
                # V4 API endpoint (default)
                video_url = "https://api.heygen.com/v2/video/av4/generate"
                headers = {
                    "accept": "application/json",
                    "content-type": "application/json",
                    "x-api-key": api_key
                }
                
                # Build payload for V4 API format
                new_payload = {
                    "video_orientation": video_payload.get('video_orientation', 'portrait'),
                    "image_key": video_payload.get('image_key'),
                    "video_title": video_payload.get('video_title', 'Avatar Video'),
                    "script": video_payload.get('script', ''),
                    "voice_id": video_payload.get('voice_id')
                }
                
                # Add custom_motion_prompt if provided
                custom_motion_prompt = video_payload.get('custom_motion_prompt', '').strip()
                if custom_motion_prompt:
                    new_payload["custom_motion_prompt"] = custom_motion_prompt
                
                print(f"📤 Calling HeyGen video generation API (v4)...")
            
            print(f"📋 Payload: {new_payload}")
            print(f"📋 Payload JSON: {json.dumps(new_payload, indent=2)}")
            
            try:
                response = requests.post(video_url, json=new_payload, headers=headers, timeout=60)
                
                if response.status_code == 200:
                    response_data = response.json()
                    print(f"✅ Video generation response: {response_data}")
                    
                    # Extract video_id or video_url from response
                    video_id = response_data.get('data', {}).get('video_id') or response_data.get('video_id')
                    video_url_response = response_data.get('data', {}).get('video_url') or response_data.get('video_url')
                    
                    if video_id:
                        job.generation_id = str(video_id)[:255]  # Store video_id as generation_id
                        job.progress = 50
                        job.save()
                        print(f"✅ Video generation started! Video ID: {video_id}")
                        
                        # Start polling for video status
                        self._poll_video_status(job_id, video_id, api_key)
                    elif video_url_response:
                        # If video is immediately available
                        job.status = "completed"
                        job.progress = 100
                        job.completed_at = timezone.now()
                        job.avatar_url = video_url_response[:2000] if len(video_url_response) > 2000 else video_url_response
                        job.save()
                        print(f"✅ Video generation completed immediately! Video URL: {video_url_response}")
                    else:
                        error_msg = f"Video generation response missing video_id or video_url: {response_data}"
                        print(f"❌ {error_msg}")
                        job.status = "error"
                        job.progress = 0
                        job.completed_at = timezone.now()
                        job.error_message = error_msg
                        job.save()
                else:
                    # Try to extract error message from response
                    error_msg = None
                    try:
                        error_data = response.json()
                        # Try to extract error message from various locations
                        error_obj = error_data.get('error') or error_data.get('data', {}).get('error')
                        if error_obj:
                            if isinstance(error_obj, dict):
                                error_msg = (
                                    error_obj.get('message') or 
                                    error_obj.get('msg') or 
                                    error_obj.get('error') or
                                    str(error_obj)
                                )
                            else:
                                error_msg = str(error_obj)
                        
                        if not error_msg:
                            msg = error_data.get('data', {}).get('message') or error_data.get('message')
                            if msg and msg.lower() != 'success':
                                error_msg = msg
                        
                        if not error_msg:
                            error_msg = (
                                error_data.get('data', {}).get('error_message') or
                                error_data.get('error_message') or
                                error_data.get('data', {}).get('msg') or
                                error_data.get('msg')
                            )
                    except:
                        pass
                    
                    # If no error message extracted, use default
                    if not error_msg:
                        error_msg = f"HeyGen API returned status {response.status_code}: {response.text[:500]}"
                    
                    # Final safety check: never use "Success" as an error message
                    if error_msg and error_msg.lower() == 'success':
                        error_msg = f"HeyGen API returned status {response.status_code}"
                    
                    print(f"❌ {error_msg}")
                    job.status = "error"
                    job.progress = 0
                    job.completed_at = timezone.now()
                    job.error_message = error_msg
                    job.save()
                    
            except requests.exceptions.RequestException as e:
                error_msg = f"Error calling HeyGen video API: {str(e)}"
                print(f"❌ {error_msg}")
                job.status = "error"
                job.progress = 0
                job.completed_at = timezone.now()
                job.error_message = error_msg
                job.save()
                
        except Exception as e:
            print(f"❌ Error in video generation process: {str(e)}")
            import traceback
            print(traceback.format_exc())
            try:
                job = AvatarGenerationJob.objects.get(job_id=job_id)
                job.status = "error"
                job.progress = 0
                job.completed_at = timezone.now()
                job.error_message = f"Video generation error: {str(e)}"
                job.save()
            except AvatarGenerationJob.DoesNotExist:
                pass
    
    def _poll_video_status(self, job_id, video_id, api_key):
        """Poll HeyGen API for video generation status"""
        attempt = 0
        
        while True:  # Poll indefinitely until video completes or fails
            try:
                job = AvatarGenerationJob.objects.get(job_id=job_id)
                
                # Early exit if job is already completed or failed
                if job.status == 'completed':
                    print(f"✅ Job {job_id} already completed, stopping status checks")
                    return
                if job.status == 'error':
                    print(f"❌ Job {job_id} already failed, stopping status checks")
                    return
                
                # Check video status using correct HeyGen API endpoint
                status_url = "https://api.heygen.com/v1/video_status.get"
                headers = {
                    "accept": "application/json",
                    "x-api-key": api_key
                }
                params = {
                    "video_id": video_id
                }
                
                status_response = requests.get(status_url, headers=headers, params=params, timeout=30)
                
                if status_response.status_code == 200:
                    status_data = status_response.json()
                    print(f"📊 Video status check {attempt + 1} response: {status_data}")
                    
                    # Parse response - try different possible structures
                    video_status = (
                        status_data.get('data', {}).get('status') or 
                        status_data.get('status') or
                        status_data.get('data', {}).get('video_status') or
                        status_data.get('video_status')
                    )
                    
                    video_url = (
                        status_data.get('data', {}).get('video_url') or 
                        status_data.get('video_url') or
                        status_data.get('data', {}).get('url') or
                        status_data.get('url')
                    )
                    
                    # Extract thumbnail URL from response
                    thumbnail_url = (
                        status_data.get('data', {}).get('thumbnail_url') or
                        status_data.get('thumbnail_url') or
                        status_data.get('data', {}).get('thumbnail') or
                        status_data.get('thumbnail') or
                        status_data.get('data', {}).get('preview_url') or
                        status_data.get('preview_url')
                    )
                    
                    print(f"📊 Video status: {video_status}, Video URL: {video_url}, Thumbnail URL: {thumbnail_url}")
                    
                    if video_status == 'completed' or video_status == 'done' or video_status == 'success' or video_url:
                        # Video completed
                        job.status = "completed"
                        job.progress = 100
                        job.completed_at = timezone.now()
                        if video_url:
                            job.avatar_url = video_url[:2000] if len(video_url) > 2000 else video_url
                        # Store thumbnail URL in note field (we can add a proper field later if needed)
                        if thumbnail_url:
                            job.note = thumbnail_url[:2000] if len(thumbnail_url) > 2000 else thumbnail_url
                        job.save()
                        print(f"✅ Video generation completed! Video URL: {video_url}, Thumbnail URL: {thumbnail_url}")
                        return
                    elif video_status == 'failed' or video_status == 'error':
                        # Video failed - extract error message from various possible locations
                        error_msg = None
                        
                        # Try to get error message from error object first
                        error_obj = status_data.get('error') or status_data.get('data', {}).get('error')
                        if error_obj:
                            if isinstance(error_obj, dict):
                                error_msg = (
                                    error_obj.get('message') or 
                                    error_obj.get('msg') or 
                                    error_obj.get('error') or
                                    str(error_obj)
                                )
                            else:
                                error_msg = str(error_obj)
                        
                        # If no error object, try message field (but skip if it says "Success")
                        if not error_msg:
                            msg = status_data.get('data', {}).get('message') or status_data.get('message')
                            if msg and msg.lower() != 'success':
                                error_msg = msg
                        
                        # Try other error fields
                        if not error_msg:
                            error_msg = (
                                status_data.get('data', {}).get('error_message') or
                                status_data.get('error_message') or
                                status_data.get('data', {}).get('msg') or
                                status_data.get('msg')
                            )
                        
                        # Check meta field for error message (common in some API responses)
                        if not error_msg:
                            meta = status_data.get('meta') or status_data.get('data', {}).get('meta')
                            if meta and isinstance(meta, dict):
                                meta_msg = meta.get('message') or meta.get('msg')
                                if meta_msg and meta_msg.lower() != 'success':
                                    error_msg = meta_msg
                        
                        # If still no error message, use a descriptive default
                        if not error_msg:
                            error_msg = f"Video generation failed with status: {video_status}"
                        
                        # Final safety check: never use "Success" as an error message
                        if error_msg and error_msg.lower() == 'success':
                            error_msg = f"Video generation failed with status: {video_status}"
                        
                        job.status = "error"
                        job.progress = 0
                        job.completed_at = timezone.now()
                        job.error_message = error_msg
                        job.save()
                        print(f"❌ Video generation failed: {error_msg}")
                        return
                    else:
                        # Still processing
                        job.status = "processing"
                        job.progress = min(50 + (attempt * 1), 95)
                        job.save()
                        print(f"⏳ Video still processing... (attempt {attempt + 1})")
                
                attempt += 1
                time.sleep(5)  # Wait 5 seconds before next check
                
            except AvatarGenerationJob.DoesNotExist:
                print(f"Job {job_id} not found")
                return
            except Exception as e:
                print(f"Error checking video status for job {job_id}: {str(e)}")
                import traceback
                print(traceback.format_exc())
                attempt += 1
                # Continue polling even on error - don't break the loop
                time.sleep(5)


class AvatarStatusView(APIView):
    """Check the status of an avatar generation job"""
    
    def get(self, request, job_id):
        try:
            # Get current user from JWT token
            user = get_current_user(request)
            if not user:
                return Response(
                    ResponseInfo.error("Authentication required"),
                    status=status.HTTP_401_UNAUTHORIZED
                )
            
            try:
                job = AvatarGenerationJob.objects.get(job_id=job_id, user=user)
            except AvatarGenerationJob.DoesNotExist:
                return Response(
                    ResponseInfo.error("Job not found or access denied"),
                    status=status.HTTP_404_NOT_FOUND
                )
            
            # If job has generation_id, always check status from HeyGen API to get latest status
            # This ensures we detect completion even if background thread stopped
            if job.generation_id:
                heygen_api_key = os.getenv('HEYGEN_API_KEY')
                if heygen_api_key:
                    try:
                        # Check if this is a video generation job
                        is_video_job = job.provider == "heygen_video"
                        print(f"🔍 AvatarStatusView: Job {job_id} - provider: {job.provider}, is_video_job: {is_video_job}")
                        
                        if is_video_job:
                            # Use video_status.get endpoint for video jobs
                            status_url = "https://api.heygen.com/v1/video_status.get"
                            headers = {
                                'accept': 'application/json',
                                'x-api-key': heygen_api_key
                            }
                            params = {
                                'video_id': job.generation_id
                            }
                            
                            print(f"🔗 AvatarStatusView: Using video_status.get endpoint for video job {job_id}")
                            status_response = requests.get(status_url, headers=headers, params=params, timeout=10)
                            
                            if status_response.status_code == 200:
                                status_data = status_response.json()
                                print(f"📊 AvatarStatusView: Video status response: {status_data}")
                                
                                # Parse video status
                                video_status = (
                                    status_data.get('data', {}).get('status') or 
                                    status_data.get('status') or
                                    status_data.get('data', {}).get('video_status') or
                                    status_data.get('video_status')
                                )
                                
                                video_url = (
                                    status_data.get('data', {}).get('video_url') or 
                                    status_data.get('video_url') or
                                    status_data.get('data', {}).get('url') or
                                    status_data.get('url')
                                )
                                
                                # Extract thumbnail URL from response
                                thumbnail_url = (
                                    status_data.get('data', {}).get('thumbnail_url') or
                                    status_data.get('thumbnail_url') or
                                    status_data.get('data', {}).get('thumbnail') or
                                    status_data.get('thumbnail') or
                                    status_data.get('data', {}).get('preview_url') or
                                    status_data.get('preview_url')
                                )
                                
                                print(f"📊 AvatarStatusView: Video status: {video_status}, Video URL: {video_url}, Thumbnail URL: {thumbnail_url}")
                                
                                if video_status == 'completed' or video_status == 'done' or video_status == 'success' or video_url:
                                    # Video completed
                                    job.status = "completed"
                                    job.progress = 100
                                    if video_url:
                                        job.avatar_url = video_url[:2000] if len(video_url) > 2000 else video_url
                                    # Store thumbnail URL in note field
                                    if thumbnail_url:
                                        job.note = thumbnail_url[:2000] if len(thumbnail_url) > 2000 else thumbnail_url
                                    if not job.completed_at:
                                        job.completed_at = timezone.now()
                                    job.save()
                                    print(f"✅ AvatarStatusView: Video job {job_id} updated to completed! Video URL: {video_url}, Thumbnail URL: {thumbnail_url}")
                                elif video_status == 'failed' or video_status == 'error':
                                    # Video failed - extract error message from various possible locations
                                    error_msg = None
                                    
                                    # Try to get error message from error object first
                                    error_obj = status_data.get('error') or status_data.get('data', {}).get('error')
                                    if error_obj:
                                        if isinstance(error_obj, dict):
                                            error_msg = (
                                                error_obj.get('message') or 
                                                error_obj.get('msg') or 
                                                error_obj.get('error') or
                                                str(error_obj)
                                            )
                                        else:
                                            error_msg = str(error_obj)
                                    
                                    # If no error object, try message field (but skip if it says "Success")
                                    if not error_msg:
                                        msg = status_data.get('data', {}).get('message') or status_data.get('message')
                                        if msg and msg.lower() != 'success':
                                            error_msg = msg
                                    
                                    # Try other error fields
                                    if not error_msg:
                                        error_msg = (
                                            status_data.get('data', {}).get('error_message') or
                                            status_data.get('error_message') or
                                            status_data.get('data', {}).get('msg') or
                                            status_data.get('msg')
                                        )
                                    
                                    # Check meta field for error message (common in some API responses)
                                    if not error_msg:
                                        meta = status_data.get('meta') or status_data.get('data', {}).get('meta')
                                        if meta and isinstance(meta, dict):
                                            meta_msg = meta.get('message') or meta.get('msg')
                                            if meta_msg and meta_msg.lower() != 'success':
                                                error_msg = meta_msg
                                    
                                    # If still no error message, use a descriptive default
                                    if not error_msg:
                                        error_msg = f"Video generation failed with status: {video_status}"
                                    
                                    # Final safety check: never use "Success" as an error message
                                    if error_msg and error_msg.lower() == 'success':
                                        error_msg = f"Video generation failed with status: {video_status}"
                                    
                                    job.status = "error"
                                    job.error_message = error_msg
                                    if not job.completed_at:
                                        job.completed_at = timezone.now()
                                    job.save()
                                    print(f"❌ AvatarStatusView: Video job {job_id} updated to error: {error_msg}")
                                else:
                                    # Still processing
                                    if job.status != "processing":
                                        job.status = "processing"
                                        job.save()
                                    print(f"⏳ AvatarStatusView: Video job {job_id} still processing... Status: {video_status}")
                            else:
                                print(f"⚠️ AvatarStatusView: Video status check returned status {status_response.status_code}: {status_response.text[:200]}")
                        
                        # Determine if this is an avatar_group job (has image_key)
                        elif job.image_key:
                            is_avatar_group = True
                            print(f"🔍 AvatarStatusView: Job {job_id} - image_key: {job.image_key}, is_avatar_group: {is_avatar_group}")
                            
                            # Use avatar_group.list endpoint for avatar_group jobs (as per HeyGen documentation)
                            status_url = "https://api.heygen.com/v2/avatar_group.list"
                            headers = {
                                'accept': 'application/json',
                                'x-api-key': heygen_api_key
                            }
                            
                            print(f"🔗 AvatarStatusView: Using avatar_group.list endpoint for job {job_id}")
                            status_response = requests.get(status_url, headers=headers, params={'include_public': 'false'}, timeout=10)
                            if status_response.status_code == 200:
                                status_data = status_response.json()
                                print(f"📊 AvatarStatusView: Checking HeyGen status (avatar_group) for job {job_id}: {status_data}")
                                
                                # Find the avatar group with matching id (response uses 'avatar_group_list')
                                avatar_groups = (
                                    status_data.get('data', {}).get('avatar_group_list', []) or 
                                    status_data.get('data', {}).get('items', []) or 
                                    status_data.get('avatar_group_list', []) or 
                                    status_data.get('items', []) or 
                                    []
                                )
                                matching_group = None
                                
                                print(f"🔍 AvatarStatusView: Looking for generation_id: {job.generation_id} in {len(avatar_groups)} avatar groups")
                                
                                for group in avatar_groups:
                                    group_id = group.get('id') or group.get('group_id')
                                    print(f"🔍 AvatarStatusView: Comparing group_id: {group_id} with generation_id: {job.generation_id}")
                                    if str(group_id) == str(job.generation_id):
                                        matching_group = group
                                        print(f"✅ AvatarStatusView: Found matching group: {group_id}")
                                        break
                                
                                if matching_group:
                                    # Extract status from avatar_group - check train_status and preview_image
                                    train_status = matching_group.get('train_status', '')
                                    preview_image = matching_group.get('preview_image')
                                    
                                    print(f"📊 AvatarStatusView: Group train_status: {train_status}, preview_image: {bool(preview_image)}")
                                    
                                    # Avatar is ready when preview_image exists (even if train_status is 'empty')
                                    # preview_image indicates the avatar is generated and ready
                                    is_completed = (preview_image is not None and preview_image)
                                    
                                    avatar_url = preview_image or matching_group.get('image_url') or matching_group.get('url')
                                    
                                    if is_completed:
                                        job.status = "completed"
                                        job.progress = 100
                                        if avatar_url:
                                            job.avatar_url = avatar_url
                                        if not job.completed_at:
                                            job.completed_at = timezone.now()
                                        job.save()
                                        print(f"✅ AvatarStatusView: Job {job_id} updated to completed! Avatar URL: {avatar_url}")
                                    elif train_status == 'failed' or train_status == 'error':
                                        job.status = "error"
                                        job.error_message = matching_group.get('message') or matching_group.get('error_message', 'Avatar generation failed')
                                        if not job.completed_at:
                                            job.completed_at = timezone.now()
                                        job.save()
                                        print(f"❌ AvatarStatusView: Job {job_id} updated to error!")
                                    else:
                                        if job.status != "processing":
                                            job.status = "processing"
                                            job.save()
                                        print(f"⏳ AvatarStatusView: Job {job_id} still processing... (train_status: {train_status})")
                        else:
                            # Use photo/generate status endpoint for prompt-based jobs
                            status_url = f"https://api.heygen.com/v2/photo_avatar/generation/{job.generation_id}"
                            headers = {
                                'accept': 'application/json',
                                'x-api-key': heygen_api_key
                            }
                            
                            status_response = requests.get(status_url, headers=headers, timeout=10)
                            if status_response.status_code == 200:
                                status_data = status_response.json()
                                print(f"📊 AvatarStatusView: Checking HeyGen status for job {job_id}: {status_data}")
                                
                                generation_status = (
                                    status_data.get('data', {}).get('status') or 
                                    status_data.get('status') or 
                                    status_data.get('data', {}).get('generation_status')
                                )
                                avatar_url = status_data.get('data', {}).get('url') or status_data.get('data', {}).get('avatar_url') or status_data.get('url')
                                # Also check for image_url_list - use first image if available
                                image_url_list = status_data.get('data', {}).get('image_url_list') or []
                                if image_url_list and not avatar_url:
                                    avatar_url = image_url_list[0] if image_url_list else None
                                
                                # Check for completion - also check if image_url_list exists as that indicates completion
                                is_completed = (
                                    generation_status == 'completed' or 
                                    generation_status == 'done' or 
                                    generation_status == 'success' or
                                    (image_url_list and len(image_url_list) > 0) or
                                    (avatar_url is not None)
                                )
                                
                                if is_completed:
                                    # Use database-level locking to prevent concurrent uploads/avatar_group creation
                                    should_upload = False
                                    with transaction.atomic():
                                        # Re-fetch job with lock to prevent race conditions
                                        locked_job = AvatarGenerationJob.objects.select_for_update().get(job_id=job_id)
                                        
                                        # Check if job is already completed or has error - if so, skip processing
                                        if locked_job.status in ['completed', 'error']:
                                            print(f"ℹ️ AvatarStatusView: Job {job_id} is already {locked_job.status}, skipping upload/avatar_group creation")
                                            job = locked_job
                                            avatar_group_creation_needed = False
                                        else:
                                            # Save avatar URL first (while we have the lock)
                                            if avatar_url:
                                                locked_job.avatar_url = avatar_url[:2000] if len(avatar_url) > 2000 else avatar_url
                                            
                                            # Check if image_key is already set (upload already done) or is being uploaded
                                            # Only one thread will pass this check due to the lock
                                            marker_cleared = False
                                            if locked_job.image_key:
                                                # Check if it's a processing marker or a real image_key
                                                if locked_job.image_key.startswith('UPLOADING_'):
                                                    # Check if processing marker is stale (older than 5 minutes)
                                                    try:
                                                        marker_timestamp = int(locked_job.image_key.split('_')[1])
                                                        current_timestamp = int(time.time() * 1000)
                                                        if current_timestamp - marker_timestamp > 300000:  # 5 minutes in milliseconds
                                                            print(f"⚠️ AvatarStatusView: Job {job_id} processing marker is stale (older than 5 minutes), clearing and retrying upload")
                                                            locked_job.image_key = None
                                                            locked_job.save(update_fields=['image_key'])
                                                            marker_cleared = True
                                                            # Will fall through to upload logic below
                                                        else:
                                                            print(f"ℹ️ AvatarStatusView: Job {job_id} is already being uploaded by another thread, skipping")
                                                            avatar_group_creation_needed = False
                                                            job = locked_job
                                                    except (ValueError, IndexError):
                                                        # Invalid marker format, clear it and retry
                                                        print(f"⚠️ AvatarStatusView: Job {job_id} has invalid processing marker, clearing and retrying upload")
                                                        locked_job.image_key = None
                                                        locked_job.save(update_fields=['image_key'])
                                                        marker_cleared = True
                                                        # Will fall through to upload logic below
                                                else:
                                                    print(f"ℹ️ AvatarStatusView: Job {job_id} already has image_key: {locked_job.image_key}, skipping upload")
                                                    avatar_group_creation_needed = False
                                                    job = locked_job
                                            
                                            # If marker was cleared or image_key is None, proceed with upload
                                            if (not locked_job.image_key or marker_cleared) and avatar_url:
                                                # This thread will handle the upload - mark as processing IMMEDIATELY while lock is held
                                                # This prevents other threads from starting upload
                                                processing_marker = f"UPLOADING_{int(time.time() * 1000)}"
                                                locked_job.image_key = processing_marker
                                                locked_job.save(update_fields=['image_key', 'avatar_url'])
                                                should_upload = True
                                                job = locked_job
                                                print(f"🔒 AvatarStatusView: Job {job_id} marked as uploading, proceeding with upload...")
                                            else:
                                                # No avatar_url or already processed
                                                avatar_group_creation_needed = False
                                                job = locked_job
                                    
                                    # Do upload/avatar_group creation OUTSIDE the transaction to avoid holding lock during HTTP requests
                                    if should_upload:
                                        # Need to upload and create avatar_group
                                        avatar_group_creation_needed = True
                                        print(f"🔄 AvatarStatusView: Uploading generated avatar to HeyGen to save to account...")
                                        try:
                                            # Download the image from avatar_url
                                            img_response = requests.get(avatar_url, timeout=30)
                                            if img_response.status_code == 200:
                                                # Determine content type from URL or response headers
                                                content_type = img_response.headers.get('Content-Type', 'image/jpeg')
                                                if not content_type.startswith('image/'):
                                                    # Try to determine from URL extension
                                                    if avatar_url.lower().endswith('.png'):
                                                        content_type = 'image/png'
                                                    elif avatar_url.lower().endswith('.jpg') or avatar_url.lower().endswith('.jpeg'):
                                                        content_type = 'image/jpeg'
                                                    else:
                                                        content_type = 'image/jpeg'  # Default
                                                
                                                # Upload to HeyGen using the asset upload endpoint
                                                upload_url = "https://upload.heygen.com/v1/asset"
                                                upload_headers = {
                                                    'Content-Type': content_type,
                                                    'x-api-key': heygen_api_key
                                                }
                                                
                                                # Upload the image data directly (binary)
                                                upload_response = requests.post(
                                                    upload_url,
                                                    headers=upload_headers,
                                                    data=img_response.content,
                                                    timeout=30
                                                )
                                                
                                                if upload_response.status_code == 200:
                                                    upload_data = upload_response.json()
                                                    image_key = (
                                                        upload_data.get('image_key') or
                                                        upload_data.get('data', {}).get('image_key') or
                                                        upload_data.get('data', {}).get('key')
                                                    )
                                                    
                                                    if image_key:
                                                        print(f"✅ AvatarStatusView: Successfully uploaded avatar to HeyGen! Image key: {image_key}")
                                                        
                                                        # Replace processing marker with real image_key
                                                        # Use lock to atomically update image_key
                                                        with transaction.atomic():
                                                            job_check = AvatarGenerationJob.objects.select_for_update().get(job_id=job_id)
                                                            # Check if it's still our processing marker or was already set by another thread
                                                            if job_check.image_key and job_check.image_key.startswith('UPLOADING_'):
                                                                # This is our processing marker, replace it with real image_key
                                                                job_check.image_key = image_key
                                                                job_check.save(update_fields=['image_key'])
                                                                job = job_check
                                                                print(f"✅ AvatarStatusView: Job {job_id} image_key updated from processing marker to: {image_key}")
                                                            elif job_check.image_key and not job_check.image_key.startswith('UPLOADING_'):
                                                                # Another thread already set a real image_key (shouldn't happen, but handle it)
                                                                print(f"ℹ️ AvatarStatusView: Job {job_id} image_key was already set by another thread: {job_check.image_key}")
                                                                image_key = job_check.image_key  # Use the existing one
                                                                job = job_check
                                                                avatar_group_creation_needed = False  # Skip avatar_group creation
                                                            else:
                                                                # No image_key set (unlikely, but handle it)
                                                                job_check.image_key = image_key
                                                                job_check.save(update_fields=['image_key'])
                                                                job = job_check
                                                                print(f"✅ AvatarStatusView: Job {job_id} image_key set to: {image_key}")
                                                        
                                                        # Create avatar_group only if we just set the image_key
                                                        if avatar_group_creation_needed:
                                                            # Create avatar_group using the uploaded image_key
                                                            avatar_group_url = "https://api.heygen.com/v2/photo_avatar/avatar_group/create"
                                                            avatar_group_headers = {
                                                                'accept': 'application/json',
                                                                'content-type': 'application/json',
                                                                'x-api-key': heygen_api_key
                                                            }
                                                            avatar_group_payload = {
                                                                "name": job.name if job.name else "Generated Avatar",
                                                                "image_key": image_key
                                                            }
                                                            
                                                            print(f"📤 AvatarStatusView: Creating avatar_group with payload: {avatar_group_payload}")
                                                            avatar_group_response = requests.post(
                                                                avatar_group_url,
                                                                headers=avatar_group_headers,
                                                                json=avatar_group_payload,
                                                                timeout=30
                                                            )
                                                            
                                                            if avatar_group_response.status_code == 200:
                                                                avatar_group_data = avatar_group_response.json()
                                                                print(f"📥 AvatarStatusView: Avatar group response: {avatar_group_data}")
                                                                group_id = (
                                                                    avatar_group_data.get('data', {}).get('id') or
                                                                    avatar_group_data.get('data', {}).get('group_id') or
                                                                    avatar_group_data.get('id') or
                                                                    avatar_group_data.get('group_id')
                                                                )
                                                                
                                                                if group_id:
                                                                    print(f"✅ AvatarStatusView: Successfully created avatar_group with ID: {group_id} (saved to account)")
                                                                    # Update job with avatar_group details
                                                                    with transaction.atomic():
                                                                        job_update = AvatarGenerationJob.objects.select_for_update().get(job_id=job_id)
                                                                        job_update.generation_id = str(group_id)
                                                                        job_update.save(update_fields=['generation_id'])
                                                                        job = job_update
                                                                    # Keep the original avatar_url for display
                                                                    avatar_group_creation_needed = False  # Successfully created
                                                                else:
                                                                    print(f"⚠️ AvatarStatusView: Failed to get group_id from avatar_group response: {avatar_group_response.text[:200]}")
                                                                    # Don't mark as error if group_id is missing - might still be processing
                                                                    # Keep avatar_group_creation_needed = True to continue checking
                                                            else:
                                                                # Avatar group creation failed - extract error message and mark job as error
                                                                try:
                                                                    error_response = avatar_group_response.json() if avatar_group_response.text else {}
                                                                    error_data = error_response.get('error', {}) or error_response.get('data', {})
                                                                    error_msg = (
                                                                        error_data.get('message') or 
                                                                        error_response.get('message') or 
                                                                        f"Failed to create avatar_group: {avatar_group_response.status_code}"
                                                                    )
                                                                except:
                                                                    error_msg = f"Failed to create avatar_group: {avatar_group_response.status_code} - {avatar_group_response.text[:200]}"
                                                                
                                                                print(f"❌ AvatarStatusView: Failed to create avatar_group: {avatar_group_response.status_code} - {error_msg}")
                                                                
                                                                # Mark job as error since avatar_group creation failed
                                                                # But preserve avatar_url so user can still access the generated avatar
                                                                with transaction.atomic():
                                                                    job_error = AvatarGenerationJob.objects.select_for_update().get(job_id=job_id)
                                                                    job_error.status = "error"
                                                                    job_error.progress = 0
                                                                    job_error.completed_at = timezone.now()
                                                                    job_error.error_message = error_msg
                                                                    # Ensure avatar_url is preserved (it should already be set, but be explicit)
                                                                    if avatar_url and not job_error.avatar_url:
                                                                        job_error.avatar_url = avatar_url[:2000] if len(avatar_url) > 2000 else avatar_url
                                                                    job_error.save()
                                                                    job = job_error
                                                                print(f"❌ AvatarStatusView: Job {job_id} marked as error: {error_msg} (avatar_url preserved: {bool(job.avatar_url)})")
                                                                avatar_group_creation_needed = False
                                                        else:
                                                            print(f"ℹ️ AvatarStatusView: Skipping avatar_group creation - image_key was already set by another thread")
                                                    else:
                                                        print(f"⚠️ AvatarStatusView: No image_key in upload response: {upload_data}")
                                                        # Upload failed - clear processing marker
                                                        with transaction.atomic():
                                                            job_fail = AvatarGenerationJob.objects.select_for_update().get(job_id=job_id)
                                                            if job_fail.image_key and job_fail.image_key.startswith('UPLOADING_'):
                                                                job_fail.image_key = None  # Clear processing marker
                                                                job_fail.save(update_fields=['image_key'])
                                                        # Upload failed but we have avatar_url - mark as completed anyway
                                                        avatar_group_creation_needed = False
                                                else:
                                                    print(f"⚠️ AvatarStatusView: Failed to upload image to HeyGen: {upload_response.status_code} - {upload_response.text[:200]}")
                                                    # Upload failed - clear processing marker
                                                    with transaction.atomic():
                                                        job_fail = AvatarGenerationJob.objects.select_for_update().get(job_id=job_id)
                                                        if job_fail.image_key and job_fail.image_key.startswith('UPLOADING_'):
                                                            job_fail.image_key = None  # Clear processing marker
                                                            job_fail.save(update_fields=['image_key'])
                                                    # Upload failed but we have avatar_url - mark as completed anyway
                                                    avatar_group_creation_needed = False
                                            else:
                                                print(f"⚠️ AvatarStatusView: Failed to download image from {avatar_url}: {img_response.status_code}")
                                                # Download failed - clear processing marker
                                                with transaction.atomic():
                                                    job_fail = AvatarGenerationJob.objects.select_for_update().get(job_id=job_id)
                                                    if job_fail.image_key and job_fail.image_key.startswith('UPLOADING_'):
                                                        job_fail.image_key = None  # Clear processing marker
                                                        job_fail.save(update_fields=['image_key'])
                                                # Download failed but we have avatar_url - mark as completed anyway
                                                avatar_group_creation_needed = False
                                        except Exception as upload_error:
                                            print(f"⚠️ AvatarStatusView: Error uploading generated avatar to HeyGen: {str(upload_error)}")
                                            import traceback
                                            print(traceback.format_exc())
                                            # Upload error - clear processing marker
                                            try:
                                                with transaction.atomic():
                                                    job_fail = AvatarGenerationJob.objects.select_for_update().get(job_id=job_id)
                                                    if job_fail.image_key and job_fail.image_key.startswith('UPLOADING_'):
                                                        job_fail.image_key = None  # Clear processing marker
                                                        job_fail.save(update_fields=['image_key'])
                                            except Exception as clear_error:
                                                print(f"⚠️ AvatarStatusView: Failed to clear processing marker: {str(clear_error)}")
                                            # Upload error but we have avatar_url - mark as completed anyway
                                            avatar_group_creation_needed = False
                                    
                                    # Re-fetch job to get latest state
                                    job = AvatarGenerationJob.objects.get(job_id=job_id)
                                    
                                    # Only mark as completed if avatar_group creation succeeded or wasn't needed, and job is not already marked as error
                                    if not avatar_group_creation_needed and job.status != "error":
                                        job.status = "completed"
                                        job.progress = 100
                                        if not job.completed_at:
                                            job.completed_at = timezone.now()
                                        job.save()
                                        print(f"✅ AvatarStatusView: Job {job_id} updated to completed!")
                                    # If job is marked as error, it will be returned with error status below
                                elif generation_status == 'failed' or generation_status == 'error':
                                    job.status = "error"
                                    job.error_message = status_data.get('data', {}).get('message') or status_data.get('message', 'Avatar generation failed')
                                    if not job.completed_at:
                                        job.completed_at = timezone.now()
                                    job.save()
                                    print(f"❌ AvatarStatusView: Job {job_id} updated to error!")
                                elif generation_status == 'pending' or generation_status == 'processing':
                                    # Still processing - update progress if needed
                                    if job.status != "processing":
                                        job.status = "processing"
                                        job.save()
                                    print(f"⏳ AvatarStatusView: Job {job_id} still processing...")
                    except Exception as e:
                        print(f"❌ Error checking HeyGen status in AvatarStatusView: {str(e)}")
                        import traceback
                        print(traceback.format_exc())
            
            response_data = {
                "job_id": str(job.job_id),
                "status": job.status,
                "progress": job.progress,
                "prompt": job.prompt,
                "name": job.name,
                "age": job.age,
                "gender": job.gender,
                "ethnicity": job.ethnicity,
                "orientation": job.orientation,
                "pose": job.pose,
                "style": job.style,
                "generation_id": job.generation_id,
                "image_key": job.image_key,  # Include image_key for display in UI
                "created_at": job.created_at.isoformat(),
                "started_at": job.started_at.isoformat() if job.started_at else None,
                "completed_at": job.completed_at.isoformat() if job.completed_at else None,
                "avatar_url": job.avatar_url,
                "avatar_id": job.avatar_id,
                "error_message": job.error_message,
                "provider": job.provider,
                "note": job.note,
                "thumbnail_url": (
                    job.thumbnail_url if job.thumbnail_url else (
                        job.note if job.note and (job.note.startswith('http') or job.note.startswith('https')) else None
                    )
                )  # Use thumbnail_url field if available, or note (contains preview_image_url for HeyGen dashboard avatars or thumbnail for video jobs)
            }
            
            return Response(
                ResponseInfo.success(response_data, "Job status retrieved successfully"),
                status=status.HTTP_200_OK
            )
            
        except Exception as e:
            return Response(
                ResponseInfo.error(f"Error retrieving job status: {str(e)}"),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class AvatarJobListView(APIView):
    """Get list of all avatar generation jobs for the current user"""
    
    def get(self, request):
        try:
            # Get current user from JWT token
            user = get_current_user(request)
            if not user:
                return Response(
                    ResponseInfo.error("Authentication required"),
                    status=status.HTTP_401_UNAUTHORIZED
                )
            
            # Get jobs only for the current user
            try:
                jobs = AvatarGenerationJob.objects.filter(user=user)
            except Exception as db_error:
                print(f"❌ Database error when querying AvatarGenerationJob: {str(db_error)}")
                print("💡 This might mean migrations haven't been run. Run: python manage.py makemigrations && python manage.py migrate")
                import traceback
                print(traceback.format_exc())
                return Response(
                    ResponseInfo.error(f"Database error: {str(db_error)}. Please ensure migrations have been run."),
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
            
            jobs_list = []
            
            for job in jobs:
                job_summary = {
                    "job_id": str(job.job_id),
                    "prompt": job.prompt,
                    "name": job.name,
                    "status": job.status,
                    "progress": job.progress if job.status != 'completed' else 100,  # Ensure completed jobs show 100%
                    "created_at": job.created_at.isoformat(),
                    "completed_at": job.completed_at.isoformat() if job.completed_at else None,
                    "avatar_url": job.avatar_url,
                    "avatar_id": job.avatar_id,
                    "generation_id": job.generation_id,
                    "image_key": job.image_key,  # Include image_key for display in UI
                    "error_message": job.error_message,
                    "provider": job.provider,
                    "thumbnail_url": (
                        job.thumbnail_url if job.thumbnail_url else (
                            job.note if job.note and (job.note.startswith('http') or job.note.startswith('https')) else None
                        )
                    )  # Use thumbnail_url field if available, or note (contains preview_image_url for HeyGen dashboard avatars or thumbnail for video jobs)
                }
                jobs_list.append(job_summary)
            
            return Response(
                ResponseInfo.success(jobs_list, "Jobs retrieved successfully"),
                status=status.HTTP_200_OK
            )
            
        except Exception as e:
            print(f"❌ Error in AvatarJobListView: {str(e)}")
            import traceback
            print(traceback.format_exc())
            return Response(
                ResponseInfo.error(f"Error retrieving jobs: {str(e)}"),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class AvatarRetryJobView(APIView):
    """Retry a failed avatar generation job"""
    
    def post(self, request, job_id):
        try:
            # Get current user from JWT token
            user = get_current_user(request)
            if not user:
                return Response(
                    ResponseInfo.error("Authentication required"),
                    status=status.HTTP_401_UNAUTHORIZED
                )
            
            # Get the job from database and verify ownership
            try:
                job = AvatarGenerationJob.objects.get(job_id=job_id, user=user)
            except AvatarGenerationJob.DoesNotExist:
                return Response(
                    ResponseInfo.error("Job not found or access denied"),
                    status=status.HTTP_404_NOT_FOUND
                )
            
            # Check if job is in error state
            if job.status != 'error':
                return Response(
                    ResponseInfo.error("Only failed jobs can be retried"),
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Get API key
            heygen_api_key = os.getenv('HEYGEN_API_KEY')
            if not heygen_api_key:
                return Response(
                    ResponseInfo.error("HeyGen API key not configured"),
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
            
            # Reference images are optional (same as new job creation)
            ref_images = job.reference_images.all()
            if ref_images:
                print(f"🖼️  Found {ref_images.count()} reference image(s) for retry")
            else:
                print("📝 No reference images found - will retry with prompt only")
            
            # Reset job status for retry
            job.status = "queued"
            job.progress = 0
            job.started_at = None
            job.completed_at = None
            job.avatar_url = None
            job.avatar_id = None
            job.image_key = None
            job.error_message = None
            job.note = None
            job.save()
            
            # Start background processing for retry (same pattern - retrieve from DB)
            thread = threading.Thread(
                target=self._process_heygen_generation,
                args=(str(job.job_id), heygen_api_key)
            )
            thread.daemon = True
            thread.start()
            
            return Response(
                ResponseInfo.success({
                    "job_id": str(job.job_id),
                    "status": "queued",
                    "message": "Job retry started successfully"
                }, "Job retry started"),
                status=status.HTTP_202_ACCEPTED
            )
            
        except Exception as e:
            return Response(
                ResponseInfo.error(f"Failed to retry job: {str(e)}"),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    def _process_heygen_generation(self, job_id, api_key):
        """Process avatar generation using HeyGen API - Retry version (same pattern as main method)"""
        try:
            try:
                job = AvatarGenerationJob.objects.get(job_id=job_id)
            except AvatarGenerationJob.DoesNotExist:
                print(f"Job {job_id} not found in database")
                return
            
            print(f"🔄 Retrying HeyGen avatar generation for job {job_id}")
            
            job.status = "processing"
            job.progress = 10
            job.started_at = timezone.now()
            job.save()
            
            # Get reference images from database (same pattern as main method)
            reference_images = []
            for ref_img in job.reference_images.all():
                reference_images.append({
                    "image": ref_img.image_data,
                    "filename": ref_img.filename,
                    "content_type": ref_img.content_type
                })
            
            job.progress = 40
            job.save()
            
            # Determine which endpoint and payload to use based on reference image presence
            if reference_images:
                # Step 1: Upload reference image to HeyGen to get image_key
                print(f"🖼️  Found {len(reference_images)} reference image(s) - uploading to HeyGen first...")
                reference_image = reference_images[0]
                image_name = reference_image['filename'] or "reference.jpg"
                
                # Decode base64 image data
                try:
                    file_content = base64.b64decode(reference_image["image"])
                except Exception as e:
                    error_msg = f"Failed to decode reference image: {str(e)}"
                    print(f"❌ {error_msg}")
                    job.status = "error"
                    job.progress = 0
                    job.completed_at = timezone.now()
                    job.error_message = error_msg
                    job.save()
                    return
                
                # Use correct HeyGen upload endpoint: https://upload.heygen.com/v1/asset
                upload_url = "https://upload.heygen.com/v1/asset"
                
                # Determine Content-Type from image content type
                content_type = reference_image.get('content_type', 'image/jpeg')
                if not content_type or not content_type.startswith('image/'):
                    content_type = 'image/jpeg'
                
                upload_headers = {
                    'Content-Type': content_type,
                    'x-api-key': api_key
                }
                
                # Upload image to HeyGen (using data parameter with file content)
                try:
                    print(f"📤 Uploading image to HeyGen: {upload_url}")
                    upload_response = requests.post(
                        upload_url, 
                        headers=upload_headers, 
                        data=file_content, 
                        timeout=30
                    )
                    
                    if upload_response.status_code == 200:
                        upload_data = upload_response.json()
                        print(f"📥 Upload response: {upload_data}")
                        
                        # Extract image_key from response
                        image_key = (
                            upload_data.get('image_key') or
                            upload_data.get('data', {}).get('image_key') or
                            upload_data.get('data', {}).get('key')
                        )
                        
                        if image_key:
                            print(f"✅ Image uploaded successfully!")
                            print(f"🔑 Received image_key: {image_key}")
                            job.image_key = image_key
                            job.save()
                        else:
                            error_msg = f"Upload succeeded but no image_key found in response: {upload_data}"
                            print(f"❌ {error_msg}")
                            job.status = "error"
                            job.progress = 0
                            job.completed_at = timezone.now()
                            job.error_message = error_msg
                            job.save()
                            return
                    else:
                        error_msg = f"Failed to upload image: {upload_response.status_code} - {upload_response.text[:500]}"
                        print(f"❌ {error_msg}")
                        job.status = "error"
                        job.progress = 0
                        job.completed_at = timezone.now()
                        job.error_message = error_msg
                        job.save()
                        return
                        
                except Exception as e:
                    error_msg = f"Error uploading image to HeyGen: {str(e)}"
                    print(f"❌ {error_msg}")
                    job.status = "error"
                    job.progress = 0
                    job.completed_at = timezone.now()
                    job.error_message = error_msg
                    job.save()
                    return
                
                # Step 2: Use avatar_group/create endpoint with the uploaded image_key
                print(f"📝 Using reference image: {image_name}")
                create_avatar_url = "https://api.heygen.com/v2/photo_avatar/avatar_group/create"
                
                # Build payload for avatar_group/create endpoint
                # Use job.name if provided, otherwise use image_name
                avatar_name = job.name if job.name else image_name
                payload = {
                    "name": avatar_name,
                    "image_key": image_key
                }
                
            else:
                # Use photo/generate endpoint for prompt-based generation
                print("📝 No reference images provided - using photo/generate endpoint (prompt-based)")
                create_avatar_url = "https://api.heygen.com/v2/photo_avatar/photo/generate"
                
                # Build payload with all required HeyGen API parameters
                payload = {
                    "appearance": job.prompt[:1000] if job.prompt else ""  # Max 1000 characters
                }
                
                # Add optional fields if provided
                if job.name:
                    payload["name"] = job.name
                if job.age:
                    payload["age"] = job.age
                if job.gender:
                    payload["gender"] = job.gender
                if job.ethnicity:
                    payload["ethnicity"] = job.ethnicity
                if job.orientation:
                    payload["orientation"] = job.orientation
                if job.pose:
                    payload["pose"] = job.pose
                if job.style:
                    payload["style"] = job.style
            
            job.progress = 60
            job.save()
            
            print(f"🎭 Creating avatar with HeyGen...")
            print(f"🔗 Create Avatar URL: {create_avatar_url}")
            create_headers = {
                'accept': 'application/json',
                'content-type': 'application/json',
                'x-api-key': api_key
            }
            
            print(f"📤 Payload: {payload}")
            
            try:
                create_response = requests.post(
                    create_avatar_url,
                    headers=create_headers,
                    json=payload,
                    timeout=30
                )
            except requests.exceptions.ConnectionError as e:
                error_msg = f"Connection error: Failed to connect to HeyGen API."
                print(f"❌ {error_msg}")
                print(f"💡 Attempted URL: {create_avatar_url}")
                print(f"💡 Error details: {str(e)}")
                job.status = "error"
                job.progress = 0
                job.completed_at = timezone.now()
                job.error_message = f"Connection failed: {str(e)}"
                job.save()
                return
            except requests.exceptions.RequestException as e:
                error_msg = f"Request error: {str(e)}"
                print(f"❌ {error_msg}")
                print(f"💡 Attempted URL: {create_avatar_url}")
                job.status = "error"
                job.progress = 0
                job.completed_at = timezone.now()
                job.error_message = error_msg
                job.save()
                return
            
            if create_response.status_code != 200:
                error_msg = f"Failed to create avatar: {create_response.status_code} - {create_response.text[:500]}"
                print(f"❌ {error_msg}")
                print(f"💡 Used URL: {create_avatar_url}")
                print(f"💡 Response: {create_response.text[:500]}")
                job.status = "error"
                job.progress = 0
                job.completed_at = timezone.now()
                job.error_message = error_msg
                job.save()
                return
            
            create_data = create_response.json()
            print(f"📥 HeyGen API Response: {create_data}")
            
            # Extract generation_id/group_id from response
            # For avatar_group/create, it returns 'id' or 'group_id'
            # For photo/generate, it returns 'generation_id'
            generation_id = None
            if "avatar_group" in create_avatar_url:
                # avatar_group/create returns 'id' or 'group_id'
                generation_id = (
                    create_data.get('data', {}).get('id') or
                    create_data.get('data', {}).get('group_id') or
                    create_data.get('id') or
                    create_data.get('group_id')
                )
            else:
                # photo/generate returns 'generation_id'
                generation_id = (
                    create_data.get('data', {}).get('generation_id') or
                    create_data.get('generation_id')
                )
            
            if not generation_id:
                error_msg = f"Generation ID/Group ID not found in HeyGen response: {create_data}"
                print(f"❌ {error_msg}")
                job.status = "error"
                job.progress = 0
                job.completed_at = timezone.now()
                job.error_message = error_msg
                job.save()
                return
            
            print(f"✅ Avatar generation started. Generation ID: {generation_id}")
            
            # Save generation_id and set status to processing (will check status periodically)
            job.generation_id = str(generation_id)
            job.status = "processing"
            job.progress = 50
            job.save()
            
            # Start polling for status in background thread
            status_thread = threading.Thread(
                target=check_avatar_generation_status,
                args=(job_id, generation_id, api_key)
            )
            status_thread.daemon = True
            status_thread.start()
            
        except Exception as e:
            print(f"❌ Job {job_id} retry failed: {str(e)}")
            import traceback
            print(traceback.format_exc())
            try:
                job = AvatarGenerationJob.objects.get(job_id=job_id)
                job.status = "error"
                job.progress = 0
                job.completed_at = timezone.now()
                job.error_message = str(e)
                job.save()
            except AvatarGenerationJob.DoesNotExist:
                print(f"Job {job_id} not found for error update")


class AvatarDeleteJobView(APIView):
    """Delete an avatar generation job"""
    
    def delete(self, request, job_id):
        """Delete a job and its associated files from the database"""
        try:
            # Get current user from JWT token
            user = get_current_user(request)
            if not user:
                return Response(
                    ResponseInfo.error("Authentication required"),
                    status=status.HTTP_401_UNAUTHORIZED
                )
            
            try:
                # Get the job from database
                job = AvatarGenerationJob.objects.get(job_id=job_id, user=user)
            except AvatarGenerationJob.DoesNotExist:
                return Response(
                    ResponseInfo.error("Job not found or access denied"),
                    status=status.HTTP_404_NOT_FOUND
                )
            
            # Delete reference images from database
            job.reference_images.all().delete()
            
            # Delete the job record from database
            job.delete()
            
            print(f"✅ Avatar job {job_id} deleted successfully from database")
            
            return Response(
                ResponseInfo.success("Job deleted successfully"),
                status=status.HTTP_200_OK
            )
            
        except Exception as e:
            print(f"❌ Error deleting avatar job {job_id}: {str(e)}")
            return Response(
                ResponseInfo.error(f"Failed to delete job: {str(e)}"),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class AvatarImageView(APIView):
    """Get avatar image URLs from HeyGen API using generation_id"""
    
    def get(self, request, generation_id):
        """Fetch avatar image URLs from HeyGen API"""
        try:
            # Get current user from JWT token
            user = get_current_user(request)
            if not user:
                return Response(
                    ResponseInfo.error("Authentication required"),
                    status=status.HTTP_401_UNAUTHORIZED
                )
            
            # Verify the job belongs to the user
            try:
                job = AvatarGenerationJob.objects.get(generation_id=generation_id, user=user)
            except AvatarGenerationJob.DoesNotExist:
                return Response(
                    ResponseInfo.error("Avatar generation job not found or access denied"),
                    status=status.HTTP_404_NOT_FOUND
                )
            
            # Get HeyGen API key
            api_key = os.getenv('HEYGEN_API_KEY')
            if not api_key:
                return Response(
                    ResponseInfo.error("HeyGen API key not configured"),
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
            
            # Fetch avatar images from HeyGen API
            status_url = f"https://api.heygen.com/v2/photo_avatar/generation/{generation_id}"
            headers = {
                'accept': 'application/json',
                'x-api-key': api_key
            }
            
            try:
                status_response = requests.get(status_url, headers=headers, timeout=30)
                
                if status_response.status_code == 200:
                    status_data = status_response.json()
                    print(f"📊 Fetched avatar images: {status_data}")
                    
                    # Extract image URLs from response
                    # HeyGen API may return image_url_list or url or avatar_url
                    image_url_list = status_data.get('data', {}).get('image_url_list') or []
                    avatar_url = status_data.get('data', {}).get('url') or status_data.get('data', {}).get('avatar_url') or status_data.get('url')
                    
                    # If we have a list, use it; otherwise use single URL
                    image_urls = image_url_list if image_url_list else ([avatar_url] if avatar_url else [])
                    
                    # Update job avatar_url if we got a single URL and job doesn't have it
                    if avatar_url and not job.avatar_url:
                        job.avatar_url = avatar_url
                        job.save()
                    
                    response_data = {
                        "generation_id": generation_id,
                        "image_url_list": image_urls,
                        "primary_url": avatar_url or (image_urls[0] if image_urls else None),
                        "status": status_data.get('data', {}).get('status') or status_data.get('status')
                    }
                    
                    return Response(
                        ResponseInfo.success(response_data, "Avatar images retrieved successfully"),
                        status=status.HTTP_200_OK
                    )
                else:
                    error_msg = f"Failed to fetch avatar images: {status_response.status_code} - {status_response.text[:500]}"
                    print(f"❌ {error_msg}")
                    return Response(
                        ResponseInfo.error(error_msg),
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR
                    )
                    
            except requests.exceptions.ConnectionError as e:
                error_msg = f"Connection error: Failed to connect to HeyGen API."
                print(f"❌ {error_msg}")
                return Response(
                    ResponseInfo.error(error_msg),
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
            except requests.exceptions.RequestException as e:
                error_msg = f"Request error: {str(e)}"
                print(f"❌ {error_msg}")
                return Response(
                    ResponseInfo.error(error_msg),
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
                
        except Exception as e:
            print(f"❌ Error fetching avatar images: {str(e)}")
            import traceback
            print(traceback.format_exc())
            return Response(
                ResponseInfo.error(f"Error fetching avatar images: {str(e)}"),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class AvatarImageFromHeyGenView(APIView):
    """Get avatar image URLs directly from HeyGen API (without requiring database entry)"""
    
    def get(self, request, avatar_id):
        """Fetch avatar image URLs directly from HeyGen API"""
        try:
            user = get_current_user(request)
            if not user:
                return Response(
                    ResponseInfo.error("Authentication required"),
                    status=status.HTTP_401_UNAUTHORIZED
                )
            
            # Get HeyGen API key
            api_key = os.getenv('HEYGEN_API_KEY')
            if not api_key:
                return Response(
                    ResponseInfo.error("HeyGen API key not configured"),
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
            
            # Try to fetch from avatar_group endpoint first (for avatar_group IDs)
            # Then try generation endpoint (for generation IDs)
            status_urls = [
                f"https://api.heygen.com/v2/avatar_group.list",
                f"https://api.heygen.com/v2/photo_avatar/generation/{avatar_id}"
            ]
            
            headers = {
                'accept': 'application/json',
                'x-api-key': api_key
            }
            
            # First try to get from avatar_group list
            try:
                list_response = requests.get(status_urls[0], headers=headers, params={"include_public": "false"}, timeout=30)
                if list_response.status_code == 200:
                    list_data = list_response.json()
                    print(f"📥 Avatar group list response: {list_data}")
                    
                    # Find the avatar with matching ID
                    avatar_list = []
                    if isinstance(list_data, dict):
                        if 'data' in list_data:
                            data = list_data['data']
                            if isinstance(data, dict):
                                avatar_list = data.get('avatar_group_list', data.get('avatars', data.get('list', [])))
                            elif isinstance(data, list):
                                avatar_list = data
                        elif 'avatar_group_list' in list_data:
                            avatar_list = list_data['avatar_group_list']
                        elif 'avatars' in list_data:
                            avatar_list = list_data['avatars']
                        elif 'list' in list_data:
                            avatar_list = list_data['list']
                    elif isinstance(list_data, list):
                        avatar_list = list_data
                    
                    # Find matching avatar
                    for avatar in avatar_list:
                        avatar_dict = avatar if isinstance(avatar, dict) else avatar.__dict__
                        # Based on actual HeyGen API response: id field contains the avatar ID
                        current_avatar_id = avatar_dict.get('id') or avatar_dict.get('avatar_id') or avatar_dict.get('avatar_group_id')
                        
                        if current_avatar_id == avatar_id:
                            # preview_image is the primary field for the avatar image URL in HeyGen response
                            avatar_url = (
                                avatar_dict.get('preview_image') or 
                                avatar_dict.get('avatar_url') or 
                                avatar_dict.get('url') or 
                                avatar_dict.get('image_url') or
                                avatar_dict.get('thumbnail_url') or
                                avatar_dict.get('preview_url') or
                                avatar_dict.get('image')
                            )
                            
                            print(f"✅ Found avatar {avatar_id} in list, preview_image: {avatar_url[:100] if avatar_url else 'None'}...")
                            
                            if avatar_url:
                                return Response(
                                    ResponseInfo.success({
                                        "generation_id": avatar_id,
                                        "image_url_list": [avatar_url],
                                        "primary_url": avatar_url,
                                        "status": avatar_dict.get('train_status', avatar_dict.get('status', 'active'))
                                    }, "Avatar image retrieved successfully"),
                                    status=status.HTTP_200_OK
                                )
                            else:
                                # Avatar found but no URL - might still be processing
                                print(f"⚠️ Avatar {avatar_id} found but no preview_image available. Available keys: {list(avatar_dict.keys())}")
                                return Response(
                                    ResponseInfo.error("Avatar image URL not available. The avatar may still be processing."),
                                    status=status.HTTP_404_NOT_FOUND
                                )
                    
                    print(f"⚠️ Avatar {avatar_id} not found in avatar_group list")
            except Exception as e:
                print(f"⚠️ Could not fetch from avatar_group list: {str(e)}")
                import traceback
                print(traceback.format_exc())
            
            # If not found in list, try generation endpoint (only for generation IDs, not avatar_group IDs)
            # Note: avatar_group IDs won't work with the generation endpoint
            try:
                status_response = requests.get(status_urls[1], headers=headers, timeout=30)
                
                if status_response.status_code == 200:
                    status_data = status_response.json()
                    print(f"📊 Fetched avatar images directly from HeyGen generation endpoint: {status_data}")
                    
                    # Extract image URLs from response
                    image_url_list = status_data.get('data', {}).get('image_url_list') or []
                    avatar_url = (
                        status_data.get('data', {}).get('url') or 
                        status_data.get('data', {}).get('avatar_url') or 
                        status_data.get('url') or
                        status_data.get('data', {}).get('image_url')
                    )
                    
                    # If we have a list, use it; otherwise use single URL
                    image_urls = image_url_list if image_url_list else ([avatar_url] if avatar_url else [])
                    
                    if not image_urls:
                        return Response(
                            ResponseInfo.error("Avatar image URL not available. The avatar may still be processing."),
                            status=status.HTTP_404_NOT_FOUND
                        )
                    
                    response_data = {
                        "generation_id": avatar_id,
                        "image_url_list": image_urls,
                        "primary_url": avatar_url or (image_urls[0] if image_urls else None),
                        "status": status_data.get('data', {}).get('status') or status_data.get('status')
                    }
                    
                    return Response(
                        ResponseInfo.success(response_data, "Avatar images retrieved successfully"),
                        status=status.HTTP_200_OK
                    )
                elif status_response.status_code == 404:
                    # 404 means this is likely an avatar_group_id, not a generation_id
                    error_msg = f"Avatar not found. This might be an avatar_group ID, not a generation ID. Please check the avatar list."
                    print(f"❌ {error_msg}")
                    return Response(
                        ResponseInfo.error(error_msg),
                        status=status.HTTP_404_NOT_FOUND
                    )
                else:
                    error_msg = f"Failed to fetch avatar images: {status_response.status_code} - {status_response.text[:500]}"
                    print(f"❌ {error_msg}")
                    return Response(
                        ResponseInfo.error(error_msg),
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR
                    )
            except requests.exceptions.ConnectionError as e:
                error_msg = f"Connection error: Failed to connect to HeyGen API."
                print(f"❌ {error_msg}")
                return Response(
                    ResponseInfo.error(error_msg),
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
            except requests.exceptions.RequestException as e:
                error_msg = f"Request error: {str(e)}"
                print(f"❌ {error_msg}")
                return Response(
                    ResponseInfo.error(error_msg),
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
                
        except Exception as e:
            print(f"❌ Error fetching avatar images from HeyGen: {str(e)}")
            import traceback
            print(traceback.format_exc())
            return Response(
                ResponseInfo.error(f"Error fetching avatar images: {str(e)}"),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class AvatarVoicesView(APIView):
    """Get list of available voices from HeyGen API"""
    
    def get(self, request):
        """Fetch available voices from HeyGen API"""
        try:
            # Get HeyGen API key from environment
            heygen_api_key = os.getenv('HEYGEN_API_KEY')
            if not heygen_api_key:
                return Response(
                    ResponseInfo.error("HeyGen API key not configured"),
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
            
            # Call HeyGen API to get voices
            url = "https://api.heygen.com/v2/voices"
            headers = {
                "accept": "application/json",
                "x-api-key": heygen_api_key
            }
            
            print(f"🔊 Fetching voices from HeyGen API...")
            response = requests.get(url, headers=headers)
            
            if response.status_code == 200:
                voices_data = response.json()
                print(f"✅ Successfully fetched {len(voices_data.get('data', []))} voices from HeyGen")
                
                return Response(
                    ResponseInfo.success(voices_data, "Voices fetched successfully"),
                    status=status.HTTP_200_OK
                )
            else:
                error_msg = f"HeyGen API returned status {response.status_code}"
                print(f"❌ {error_msg}: {response.text}")
                return Response(
                    ResponseInfo.error(error_msg),
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
                
        except Exception as e:
            print(f"❌ Error fetching voices: {str(e)}")
            import traceback
            print(traceback.format_exc())
            return Response(
                ResponseInfo.error(f"Error fetching voices: {str(e)}"),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class AvatarListFromHeyGenView(APIView):
    """Get list of avatars from HeyGen dashboard"""
    
    def get(self, request):
        """Fetch avatars from HeyGen API"""
        try:
            user = get_current_user(request)
            if not user:
                return Response(
                    ResponseInfo.error("Authentication required"),
                    status=status.HTTP_401_UNAUTHORIZED
                )
            
            # Get HeyGen API key from environment
            heygen_api_key = os.getenv('HEYGEN_API_KEY')
            if not heygen_api_key:
                return Response(
                    ResponseInfo.error("HeyGen API key not configured"),
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
            
            # Call HeyGen API to get avatar list
            url = "https://api.heygen.com/v2/avatars"
            headers = {
                "accept": "application/json",
                "x-api-key": heygen_api_key
            }
            
            print(f"🔄 Fetching avatars from HeyGen API...")
            print(f"🔗 URL: {url}")
            print(f"🔑 API Key: {'Present' if heygen_api_key else 'Missing'}")
            
            response = requests.get(url, headers=headers, timeout=30)
            
            print(f"📡 Response Status Code: {response.status_code}")
            
            if response.status_code == 200:
                try:
                    avatars_data = response.json()
                    print(f"📥 HeyGen API Response type: {type(avatars_data)}")
                    if isinstance(avatars_data, dict):
                        print(f"📥 HeyGen API Response keys: {list(avatars_data.keys())}")
                        # Print first few items for debugging
                        for key in list(avatars_data.keys())[:3]:
                            value = avatars_data[key]
                            if isinstance(value, list) and len(value) > 0:
                                print(f"📥 Sample item from '{key}': {str(value[0])[:200]}")
                    elif isinstance(avatars_data, list):
                        print(f"📥 HeyGen API Response is a list with {len(avatars_data)} items")
                        if len(avatars_data) > 0:
                            print(f"📥 Sample item: {str(avatars_data[0])[:200]}")
                except Exception as json_error:
                    print(f"❌ Error parsing JSON response: {str(json_error)}")
                    print(f"📥 Raw response text (first 500 chars): {response.text[:500]}")
                    return Response(
                        ResponseInfo.error(f"Invalid JSON response from HeyGen API: {str(json_error)}"),
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR
                    )
                
                # Handle different possible response structures
                # Based on actual HeyGen API, response structure is:
                # { "error": null, "data": { "avatars": [...] } }
                avatar_list = []
                if isinstance(avatars_data, dict):
                    # Try different possible keys
                    if 'data' in avatars_data:
                        data = avatars_data['data']
                        if isinstance(data, dict):
                            # Check for 'avatars' key first (actual API structure)
                            avatar_list = data.get('avatars', data.get('avatar_group_list', data.get('list', [])))
                        elif isinstance(data, list):
                            avatar_list = data
                    elif 'avatar_group_list' in avatars_data:
                        avatar_list = avatars_data['avatar_group_list']
                    elif 'avatars' in avatars_data:
                        avatar_list = avatars_data['avatars']
                    elif 'list' in avatars_data:
                        avatar_list = avatars_data['list']
                elif isinstance(avatars_data, list):
                    # Direct list response
                    avatar_list = avatars_data
                
                print(f"✅ Successfully fetched {len(avatar_list)} avatars from HeyGen")
                
                if len(avatar_list) == 0:
                    print(f"⚠️ Warning: No avatars found in response. Response structure might have changed.")
                    print(f"📥 Full response structure: {str(avatars_data)[:500]}")
                
                # Regex pattern to match UUID format (32 hex characters like "36b31b6e7c4d489c9dae7d1b3b634dca")
                uuid_pattern = re.compile(r'^[0-9a-f]{32}$', re.IGNORECASE)
                
                # Format the response to include relevant avatar information
                formatted_avatars = []
                saved_count = 0
                skipped_count = 0
                
                for avatar in avatar_list:
                    # Handle both dict and object-like structures
                    if isinstance(avatar, dict):
                        # Based on actual HeyGen API response structure:
                        # - avatar_id: avatar ID (not "id")
                        # - avatar_name: avatar name (not "name")
                        # - preview_image_url: the image URL
                        # - preview_video_url: the video URL (for video avatars)
                        # - type: avatar type (can be null)
                        avatar_id = avatar.get("avatar_id", avatar.get("id", ""))
                        
                        # Filter: Only include avatars with UUID format (32 hex characters)
                        if not avatar_id:
                            print(f"⏭️ Skipping avatar with no ID. Available keys: {list(avatar.keys())}")
                            skipped_count += 1
                            continue
                        if not uuid_pattern.match(avatar_id):
                            print(f"⏭️ Skipping avatar with non-UUID ID: {avatar_id} (length: {len(avatar_id)})")
                            skipped_count += 1
                            continue
                        
                        avatar_name = avatar.get("avatar_name", avatar.get("name", ""))
                        
                        # Check for video URL first (for video avatars), then fall back to image URLs
                        # Based on actual API: preview_video_url is the field for video avatars
                        # Also check for type field that might indicate video avatar
                        avatar_type = avatar.get("type", avatar.get("avatar_type", ""))
                        is_video_avatar = avatar_type and ("video" in str(avatar_type).lower() or "talking" in str(avatar_type).lower())
                        
                        # Check preview_video_url first (actual API field name)
                        video_url = (
                            avatar.get("preview_video_url") or  # Primary field from actual API
                            avatar.get("video_url") or 
                            avatar.get("video_preview_url") or
                            avatar.get("video_preview") or
                            avatar.get("video") or
                            avatar.get("preview_video") or
                            ""
                        )
                        
                        # Log all available keys for debugging video avatars
                        if is_video_avatar or not video_url:
                            all_keys = list(avatar.keys())
                            video_related_keys = [k for k in all_keys if "video" in k.lower() or "preview" in k.lower()]
                            if video_related_keys:
                                print(f"🔍 Avatar {avatar_id} - Video-related keys found: {video_related_keys}")
                        
                        # If video URL exists, use it; otherwise use image/preview URLs
                        if video_url:
                            avatar_url = video_url
                            print(f"🎥 Avatar {avatar_id} is a video avatar, using video URL: {video_url[:100]}...")
                        elif is_video_avatar:
                            # If marked as video avatar but no video URL found, log for debugging
                            print(f"⚠️ Avatar {avatar_id} is marked as video type but no video URL found. Available keys: {list(avatar.keys())}")
                            # Still try to get image URL as fallback
                            avatar_url = (
                                avatar.get("preview_image_url") or
                                avatar.get("preview_image") or 
                                avatar.get("avatar_url") or 
                                avatar.get("url") or 
                                avatar.get("image_url") or
                                avatar.get("thumbnail_url") or
                                avatar.get("preview_url") or
                                avatar.get("image") or
                                avatar.get("avatar_image_url") or
                                ""
                            )
                        else:
                            # preview_image_url is the primary field for the avatar image URL (actual API field name)
                            avatar_url = (
                                avatar.get("preview_image_url") or  # Primary field from actual API
                                avatar.get("preview_image") or 
                                avatar.get("avatar_url") or 
                                avatar.get("url") or 
                                avatar.get("image_url") or
                                avatar.get("thumbnail_url") or
                                avatar.get("preview_url") or
                                avatar.get("image") or
                                avatar.get("avatar_image_url") or
                                ""
                            )
                        
                        # Extract preview_image_url separately for thumbnail (HeyGen API provides this)
                        preview_image_url = avatar.get("preview_image_url") or avatar.get("preview_image") or None
                        
                        # Convert created_at from timestamp to ISO string if it's a number
                        created_at = avatar.get("created_at", avatar.get("created", ""))
                        if isinstance(created_at, (int, float)):
                            from datetime import datetime
                            created_at = datetime.fromtimestamp(created_at).isoformat()
                        
                        formatted_avatar = {
                            "avatar_id": avatar_id,
                            "avatar_name": avatar_name,
                            "avatar_url": avatar_url,
                            "preview_image_url": preview_image_url,  # Store preview_image_url separately for thumbnail
                            "created_at": created_at,
                            "updated_at": avatar.get("updated_at", avatar.get("updated", "")),
                            "status": avatar.get("train_status", avatar.get("status", "active")),
                        }
                        # Log if avatar_url is missing for debugging
                        if not avatar_url:
                            print(f"⚠️ Avatar {avatar_id} has no avatar_url. Available keys: {list(avatar.keys())}")
                        else:
                            media_type = "video" if video_url else "image"
                            print(f"✅ Avatar {avatar_id} ({avatar_name}) - {media_type} URL: {avatar_url[:100]}...")
                    else:
                        # If it's an object with attributes
                        avatar_id = getattr(avatar, "id", getattr(avatar, "avatar_id", ""))
                        
                        # Filter: Only include avatars with UUID format (32 hex characters)
                        if not avatar_id:
                            print(f"⏭️ Skipping avatar with no ID")
                            skipped_count += 1
                            continue
                        if not uuid_pattern.match(avatar_id):
                            print(f"⏭️ Skipping avatar with non-UUID ID: {avatar_id} (length: {len(avatar_id)})")
                            skipped_count += 1
                            continue
                        
                        avatar_name = getattr(avatar, "name", getattr(avatar, "avatar_name", ""))
                        
                        # Check for video URL first (for video avatars), then fall back to image URLs
                        # Also check for type field that might indicate video avatar
                        avatar_type = getattr(avatar, "type", getattr(avatar, "avatar_type", ""))
                        is_video_avatar = avatar_type and ("video" in str(avatar_type).lower() or "talking" in str(avatar_type).lower())
                        
                        video_url = (
                            getattr(avatar, "video_url", None) or 
                            getattr(avatar, "preview_video_url", None) or 
                            getattr(avatar, "video_preview", None) or
                            getattr(avatar, "video", None) or
                            getattr(avatar, "video_preview_url", None) or
                            getattr(avatar, "preview_video", None) or
                            ""
                        )
                        
                        # If video URL exists, use it; otherwise use image/preview URLs
                        if video_url:
                            avatar_url = video_url
                            print(f"🎥 Avatar {avatar_id} is a video avatar, using video URL: {video_url[:100] if video_url else ''}...")
                        elif is_video_avatar:
                            # If marked as video avatar but no video URL found, log for debugging
                            print(f"⚠️ Avatar {avatar_id} is marked as video type but no video URL found")
                            # Still try to get image URL as fallback
                            avatar_url = (
                                getattr(avatar, "preview_image", None) or 
                                getattr(avatar, "avatar_url", None) or 
                                getattr(avatar, "url", None) or 
                                getattr(avatar, "image_url", None) or
                                getattr(avatar, "thumbnail_url", None) or
                                getattr(avatar, "preview_url", None) or
                                getattr(avatar, "image", None) or
                                ""
                            )
                        else:
                            avatar_url = (
                                getattr(avatar, "preview_image_url", None) or
                                getattr(avatar, "preview_image", None) or 
                                getattr(avatar, "avatar_url", None) or 
                                getattr(avatar, "url", None) or 
                                getattr(avatar, "image_url", None) or
                                getattr(avatar, "thumbnail_url", None) or
                                getattr(avatar, "preview_url", None) or
                                getattr(avatar, "image", None) or
                                ""
                            )
                        
                        created_at = getattr(avatar, "created_at", getattr(avatar, "created", ""))
                        if isinstance(created_at, (int, float)):
                            from datetime import datetime
                            created_at = datetime.fromtimestamp(created_at).isoformat()
                        
                        formatted_avatar = {
                            "avatar_id": avatar_id,
                            "avatar_name": avatar_name,
                            "avatar_url": avatar_url or "",
                            "created_at": created_at,
                            "updated_at": getattr(avatar, "updated_at", getattr(avatar, "updated", "")),
                            "status": getattr(avatar, "train_status", getattr(avatar, "status", "active")),
                        }
                        
                        # Log if avatar_url is missing for debugging
                        if not avatar_url:
                            print(f"⚠️ Avatar {avatar_id} has no avatar_url. Available attributes: {dir(avatar)}")
                        else:
                            media_type = "video" if video_url else "image"
                            print(f"✅ Avatar {avatar_id} ({avatar_name}) - {media_type} URL: {avatar_url[:100]}...")
                    formatted_avatars.append(formatted_avatar)
                
                # Save avatars to database if they don't already exist
                from datetime import datetime
                for formatted_avatar in formatted_avatars:
                    avatar_id = formatted_avatar.get("avatar_id", "")
                    avatar_name = formatted_avatar.get("avatar_name", "")
                    avatar_url = formatted_avatar.get("avatar_url", "")
                    
                    if not avatar_id:
                        print(f"⚠️ Skipping avatar with no avatar_id")
                        skipped_count += 1
                        continue
                    
                    # Check if avatar already exists in database (by avatar_id or generation_id)
                    # Only consider it existing if it's completed and has an avatar_url
                    existing_job = AvatarGenerationJob.objects.filter(
                        user=user
                    ).filter(
                        models.Q(avatar_id=avatar_id) | models.Q(generation_id=avatar_id)
                    ).filter(
                        status='completed',
                        avatar_url__isnull=False
                    ).exclude(
                        avatar_url=''
                    ).first()
                    
                    if existing_job:
                        # Update existing job if needed
                        # Always update if:
                        # 1. avatar_url changed, OR
                        # 2. We have a video URL and the existing one is not a video (prioritize video URLs), OR
                        # 3. provider is not 'heygen' (avatars from HeyGen dashboard should have provider='heygen')
                        should_update = False
                        update_reason = ""
                        
                        if avatar_url and existing_job.avatar_url != avatar_url:
                            should_update = True
                            update_reason = "URL changed"
                        elif video_url and existing_job.avatar_url:
                            # Check if existing URL is a video URL
                            existing_is_video = any(ext in existing_job.avatar_url.lower() for ext in ['.mp4', '.webm', '.mov', 'video'])
                            new_is_video = any(ext in avatar_url.lower() for ext in ['.mp4', '.webm', '.mov', 'video'])
                            if new_is_video and not existing_is_video:
                                should_update = True
                                update_reason = "Updating to video URL"
                        
                        # Ensure provider is 'heygen' for avatars fetched from HeyGen dashboard
                        if existing_job.provider != 'heygen':
                            should_update = True
                            if update_reason:
                                update_reason += ", provider updated to 'heygen'"
                            else:
                                update_reason = "Provider updated to 'heygen'"
                        
                        if should_update:
                            existing_job.avatar_url = avatar_url
                            # Update note with preview_image_url if available (for HeyGen dashboard avatars)
                            preview_image_url = formatted_avatar.get("preview_image_url")
                            if preview_image_url:
                                existing_job.note = preview_image_url
                            if existing_job.provider != 'heygen':
                                existing_job.provider = 'heygen'
                            existing_job.save()
                            print(f"🔄 Updated avatar {avatar_id} in database ({update_reason}): {avatar_url[:100]}...")
                        else:
                            print(f"⏭️ Avatar {avatar_id} already exists in database, skipping")
                        skipped_count += 1
                    else:
                        # Create new job record for this avatar
                        try:
                            # Parse created_at if it's a string
                            created_at_dt = None
                            if formatted_avatar.get("created_at"):
                                created_at_str = formatted_avatar.get("created_at")
                                try:
                                    if isinstance(created_at_str, str):
                                        # Try parsing ISO format
                                        created_at_dt = datetime.fromisoformat(created_at_str.replace('Z', '+00:00'))
                                    elif isinstance(created_at_str, (int, float)):
                                        created_at_dt = datetime.fromtimestamp(created_at_str)
                                except (ValueError, TypeError) as e:
                                    print(f"⚠️ Could not parse created_at for avatar {avatar_id}: {e}")
                                    created_at_dt = timezone.now()
                            else:
                                created_at_dt = timezone.now()
                            
                            # Get preview_image_url from formatted_avatar for thumbnail
                            # Store preview_image_url in note field for HeyGen dashboard avatars
                            preview_image_url = formatted_avatar.get("preview_image_url")
                            note_value = preview_image_url if preview_image_url else None
                            
                            new_job = AvatarGenerationJob.objects.create(
                                user=user,
                                prompt=avatar_name or "Avatar from HeyGen dashboard",  # Use avatar name as prompt
                                name=avatar_name,
                                avatar_id=avatar_id,
                                generation_id=avatar_id,  # Use avatar_id as generation_id for consistency
                                avatar_url=avatar_url,
                                note=note_value,  # Save preview_image_url in note field for HeyGen dashboard avatars
                                status="completed",
                                progress=100,
                                completed_at=created_at_dt if created_at_dt else timezone.now(),
                                provider="heygen"
                            )
                            saved_count += 1
                            print(f"💾 Saved avatar {avatar_id} ({avatar_name}) to database")
                        except Exception as save_error:
                            print(f"❌ Error saving avatar {avatar_id} to database: {str(save_error)}")
                            import traceback
                            print(traceback.format_exc())
                            skipped_count += 1
                
                print(f"📊 Avatar sync summary: {saved_count} saved, {skipped_count} skipped (already exist)")
                
                return Response(
                    ResponseInfo.success({
                        "avatars": formatted_avatars,
                        "total_count": len(formatted_avatars),
                        "saved_count": saved_count,
                        "skipped_count": skipped_count
                    }, f"Avatars fetched successfully. {saved_count} new avatar(s) saved to database."),
                    status=status.HTTP_200_OK
                )
            else:
                error_msg = f"HeyGen API returned status {response.status_code}"
                print(f"❌ {error_msg}: {response.text}")
                return Response(
                    ResponseInfo.error(error_msg),
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
                
        except Exception as e:
            print(f"❌ Error fetching avatars from HeyGen: {str(e)}")
            import traceback
            print(traceback.format_exc())
            return Response(
                ResponseInfo.error(f"Error fetching avatars: {str(e)}"),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class AssetListFromHeyGenView(APIView):
    """Get list of assets (images) from HeyGen media"""
    
    def get(self, request):
        """Fetch assets from HeyGen API"""
        try:
            user = get_current_user(request)
            if not user:
                return Response(
                    ResponseInfo.error("Authentication required"),
                    status=status.HTTP_401_UNAUTHORIZED
                )
            
            # Get HeyGen API key from environment
            heygen_api_key = os.getenv('HEYGEN_API_KEY')
            if not heygen_api_key:
                return Response(
                    ResponseInfo.error("HeyGen API key not configured"),
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
            
            # Call HeyGen API to get asset list
            url = "https://api.heygen.com/v1/asset/list"
            headers = {
                "accept": "application/json",
                "x-api-key": heygen_api_key
            }
            
            print(f"🔄 Fetching assets from HeyGen API...")
            response = requests.get(url, headers=headers)
            
            if response.status_code == 200:
                assets_data = response.json()
                print(f"📥 HeyGen Asset API Response type: {type(assets_data)}")
                if isinstance(assets_data, dict):
                    print(f"📥 HeyGen Asset API Response keys: {list(assets_data.keys())}")
                
                # Handle different possible response structures
                asset_list = []
                if isinstance(assets_data, dict):
                    # Try different possible keys
                    if 'data' in assets_data:
                        data = assets_data['data']
                        if isinstance(data, dict):
                            asset_list = data.get('assets', data.get('list', data.get('items', [])))
                        elif isinstance(data, list):
                            asset_list = data
                    elif 'assets' in assets_data:
                        asset_list = assets_data['assets']
                    elif 'list' in assets_data:
                        asset_list = assets_data['list']
                    elif 'items' in assets_data:
                        asset_list = assets_data['items']
                elif isinstance(assets_data, list):
                    # Direct list response
                    asset_list = assets_data
                
                print(f"✅ Successfully fetched {len(asset_list)} assets from HeyGen")
                
                # Format the response to include relevant asset information
                formatted_assets = []
                for asset in asset_list:
                    # Handle both dict and object-like structures
                    if isinstance(asset, dict):
                        # Extract asset key and file type
                        asset_key = asset.get("key", asset.get("image_key", asset.get("asset_key", "")))
                        asset_id = asset.get("id", asset.get("asset_id", ""))
                        asset_name = asset.get("name", asset.get("asset_name", asset.get("title", "")))
                        file_type = asset.get("file_type", asset.get("type", asset.get("asset_type", "image")))
                        
                        # Get URL (works for both images and videos)
                        media_url = (
                            asset.get("url") or 
                            asset.get("image_url") or 
                            asset.get("video_url") or
                            asset.get("preview_url") or 
                            asset.get("thumbnail_url") or 
                            asset.get("preview_image") or
                            asset.get("image") or
                            ""
                        )
                        
                        # Construct asset_key if not provided
                        # Format: "image/{id}/original" for images, "video/{id}/original" for videos
                        if not asset_key and asset_id:
                            if file_type == "video":
                                asset_key = f"video/{asset_id}/original"
                            else:
                                asset_key = f"image/{asset_id}/original"
                        
                        # Convert created_at from timestamp to ISO string if it's a number
                        created_at = asset.get("created_at", asset.get("created", asset.get("created_ts", "")))
                        if isinstance(created_at, (int, float)):
                            from datetime import datetime
                            created_at = datetime.fromtimestamp(created_at).isoformat()
                        
                        formatted_asset = {
                            "asset_key": asset_key or "",
                            "asset_id": asset_id,
                            "asset_name": asset_name or f"Asset {asset_id}" if asset_id else "Unnamed Asset",
                            "image_url": media_url or "",
                            "video_url": media_url if file_type == "video" else "",
                            "created_at": created_at,
                            "type": file_type,
                        }
                    else:
                        # Handle object-like structure
                        asset_key = getattr(asset, "key", getattr(asset, "image_key", getattr(asset, "asset_key", "")))
                        asset_id = getattr(asset, "id", getattr(asset, "asset_id", ""))
                        asset_name = getattr(asset, "name", getattr(asset, "asset_name", getattr(asset, "title", "")))
                        file_type = getattr(asset, "file_type", getattr(asset, "type", getattr(asset, "asset_type", "image")))
                        
                        media_url = (
                            getattr(asset, "url", None) or 
                            getattr(asset, "image_url", None) or 
                            getattr(asset, "video_url", None) or
                            getattr(asset, "preview_url", None) or 
                            getattr(asset, "thumbnail_url", None) or 
                            getattr(asset, "preview_image", None) or
                            getattr(asset, "image", None) or
                            ""
                        )
                        
                        # Construct asset_key if not provided
                        if not asset_key and asset_id:
                            if file_type == "video":
                                asset_key = f"video/{asset_id}/original"
                            else:
                                asset_key = f"image/{asset_id}/original"
                        
                        created_at = getattr(asset, "created_at", getattr(asset, "created", getattr(asset, "created_ts", "")))
                        if isinstance(created_at, (int, float)):
                            from datetime import datetime
                            created_at = datetime.fromtimestamp(created_at).isoformat()
                        
                        formatted_asset = {
                            "asset_key": asset_key or "",
                            "asset_id": asset_id,
                            "asset_name": asset_name or f"Asset {asset_id}" if asset_id else "Unnamed Asset",
                            "image_url": media_url or "",
                            "video_url": media_url if file_type == "video" else "",
                            "created_at": created_at,
                            "type": file_type,
                        }
                    formatted_assets.append(formatted_asset)
                
                return Response(
                    ResponseInfo.success({
                        "assets": formatted_assets,
                        "total_count": len(formatted_assets)
                    }, "Assets fetched successfully"),
                    status=status.HTTP_200_OK
                )
            else:
                error_msg = f"HeyGen API returned status {response.status_code}"
                print(f"❌ {error_msg}: {response.text}")
                return Response(
                    ResponseInfo.error(error_msg),
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
                
        except Exception as e:
            print(f"❌ Error fetching assets from HeyGen: {str(e)}")
            import traceback
            print(traceback.format_exc())
            return Response(
                ResponseInfo.error(f"Error fetching assets: {str(e)}"),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


def generate_three_avatar_prompts_with_openai(user_prompt):
    """Generate three different avatar prompt variations using OpenAI based on user input
    
    Args:
        user_prompt (str): User's original avatar description/prompt
        
    Returns:
        list: Three different detailed avatar prompt variations optimized for HeyGen photo_avatar generation
    """
    try:
        # Get OpenAI API key
        openai_api_key = os.getenv('OPENAI_API_KEY')
        
        if not openai_api_key:
            print("⚠️ OpenAI API key not found in environment variables")
            print("💡 Please add OPENAI_API_KEY to your .env file")
            # Return three variations of the original prompt
            return [
                f"A professional portrait of {user_prompt}, realistic style, natural lighting, high quality, detailed facial features, clean background",
                f"A character design of {user_prompt}, artistic style, expressive features, vibrant colors, professional photography, studio lighting",
                f"A detailed avatar of {user_prompt}, photorealistic style, soft lighting, high resolution, portrait orientation, professional quality"
            ]
        
        print(f"🤖 OpenAI API Key: {'Present' if openai_api_key else 'Missing'}")
        
        # Create system prompt for generating three avatar-specific variations
        system_prompt = """You are an expert at creating high-quality, detailed avatar generation prompts for AI avatar generation tools like HeyGen's photo_avatar API.
Based on the user's input, create THREE different comprehensive prompt variations that are specifically designed for avatar/character generation.

CRITICAL REQUIREMENT: Each prompt MUST be detailed and descriptive, focusing on appearance, characteristics, and visual attributes suitable for creating realistic or stylized avatars.

IMPORTANT: Focus on AVATAR-SPECIFIC elements. These prompts should describe a person's appearance, characteristics, and visual attributes that can be used to generate an avatar image.

Key Elements to include for AVATAR prompts:

1. PHYSICAL APPEARANCE:
- Age: Infant, Child, Teen, Young Adult, Middle-Aged, Elderly
- Gender: Man, Woman, Non-binary, Unspecified
- Ethnicity: Specific ethnic background or Unspecified
- Hair: Color, style, length, texture (e.g., "long black wavy hair", "short blonde pixie cut", "curly brown hair")
- Eyes: Color, shape, expression (e.g., "bright blue eyes", "warm brown eyes", "almond-shaped eyes")
- Facial features: Bone structure, skin tone, distinctive features
- Build: Body type, height, physique
- Facial hair: Beard, mustache, clean-shaven (for applicable genders)

2. CLOTHING & STYLE:
- Clothing type: Formal, casual, business, traditional, modern, vintage, sporty
- Specific garments: Shirt, dress, suit, jacket, accessories
- Colors and patterns: Specific color schemes, patterns, textures
- Style: Classic, contemporary, trendy, professional, artistic

3. POSE & ORIENTATION:
- Portrait orientation: Vertical, horizontal, square
- Pose: Full body, half body, close-up, headshot
- Position: Standing, sitting, facing camera, side profile, three-quarter view
- Expression: Smiling, serious, neutral, friendly, professional

4. VISUAL STYLE:
- Realistic: Photorealistic, professional photography style
- Artistic: Stylized, illustrated, digital art style
- Quality: High resolution, detailed, professional quality
- Background: Clean, solid color, blurred, studio setting, environmental

5. CHARACTER & MOOD:
- Personality traits: Friendly, serious, professional, approachable, confident
- Expression: Smiling, neutral, serious, warm, energetic
- Mood: Professional, casual, elegant, vibrant, calm

6. DETAILED DESCRIPTIONS:
- Specific facial features (nose shape, cheekbones, jawline)
- Skin tone and texture
- Hair details (parting, volume, styling)
- Eye characteristics (color, size, shape, expression)
- Clothing details (fit, style, accessories)
- Overall appearance and presentation

WORD COUNT REQUIREMENTS:
- Each prompt MUST be 30-80 words long
- Use rich, descriptive language with specific details
- Include multiple appearance attributes, style elements, and visual characteristics
- Describe appearance, clothing, pose, and mood in detail
- Paint a complete visual picture of the avatar

CRITICAL FORMAT REQUIREMENTS:
- Return EXACTLY 3 prompts
- Separate each prompt with "|||" (three pipe characters)
- Each prompt must be on a single line
- No explanations, no numbering, no additional text
- No line breaks within prompts
- Just the three comprehensive avatar prompts separated by "|||"

Remember: These are AVATAR prompts - focus on appearance, characteristics, visual attributes, and how the person looks! Each prompt should be a complete, detailed description of an avatar that could be generated."""

        user_message = f"""User's original prompt: "{user_prompt}"

Based on this prompt, create THREE highly detailed avatar generation prompts that:
1. Are EXACTLY 30-80 words long with SPECIFIC details about appearance, characteristics, and visual attributes
2. Each take a DIFFERENT creative approach (different styles, poses, expressions, clothing)
3. Focus on APPEARANCE and how the person looks (not actions or scenes)
4. Include specific physical features (hair, eyes, facial features, build)
5. Describe clothing, style, and presentation in detail
6. Specify pose, orientation, and expression
7. Create different visual styles and moods across the three variations
8. Include background and lighting descriptions
9. Use vivid, descriptive language that paints a complete picture of the avatar
10. Optimize for avatar/character generation tools

WORD COUNT REQUIREMENT: Each prompt must be 30-80 words. Count your words carefully!

Return ONLY the three detailed avatar prompts separated by "|||" (three pipe characters).
No explanations, no labels, just the three comprehensive avatar prompts.

CRITICAL: Each prompt MUST focus on AVATAR elements - appearance, characteristics, visual attributes, how the person looks! Make each prompt a complete, detailed description of an avatar that could be generated.

NOW CREATE THE THREE AVATAR PROMPTS:"""
        
        # Merge system prompt and user message
        merged_prompt = f"{system_prompt}\n\n{user_message}"
        
        print("🔄 Calling OpenAI API for three avatar prompt variations...")
        print(f"📝 User prompt: {user_prompt}")
        
        # Initialize OpenAI client
        client = openai.OpenAI(api_key=openai_api_key)
        
        # Retry logic for API call
        max_retries = 3
        for attempt in range(max_retries):
            try:
                # Call OpenAI API
                response = client.chat.completions.create(
                    model="gpt-3.5-turbo",
                    messages=[
                        {"role": "user", "content": merged_prompt}
                    ],
                    max_tokens=1200,  # Enough for 3 detailed avatar prompts
                    temperature=0.9,
                    top_p=0.95,
                    frequency_penalty=0.1,
                    presence_penalty=0.5
                )
                break  # Success, exit retry loop
            except Exception as e:
                print(f"⚠️ OpenAI API call attempt {attempt + 1} failed: {str(e)}")
                if attempt == max_retries - 1:
                    raise e
                time.sleep(1)
        
        # Parse the response
        response_text = response.choices[0].message.content.strip()
        print(f"🤖 OpenAI Response received ({len(response_text)} characters)")
        
        # Split by the separator and clean up
        raw_prompts = response_text.split("|||")
        prompts = [prompt.strip() for prompt in raw_prompts if prompt.strip()]
        
        print(f"✅ Parsed {len(prompts)} avatar prompts")
        
        # Ensure we have exactly 3 prompts
        if len(prompts) != 3:
            print(f"⚠️ Expected 3 prompts, got {len(prompts)}. Creating fallback prompts.")
            prompts = [
                f"A professional portrait of {user_prompt}, realistic style, natural lighting, high quality, detailed facial features, clean background, professional photography, studio setting",
                f"A character design of {user_prompt}, artistic style, expressive features, vibrant colors, professional photography, studio lighting, detailed appearance, modern presentation",
                f"A detailed avatar of {user_prompt}, photorealistic style, soft lighting, high resolution, portrait orientation, professional quality, clean background, expressive features"
            ]
        
        print("=" * 80)
        print("🎯 THREE AVATAR PROMPT VARIATIONS GENERATED:")
        print("=" * 80)
        for i, prompt in enumerate(prompts, 1):
            print(f"👤 Avatar Prompt {i}: {prompt[:100]}...")
        print("=" * 80)
        
        return prompts
        
    except Exception as e:
        print(f"❌ Error generating three avatar prompts: {str(e)}")
        print(f"💡 Falling back to default avatar prompt variations")
        return [
            f"A professional portrait of {user_prompt}, realistic style, natural lighting, high quality, detailed facial features, clean background, professional photography, studio setting",
            f"A character design of {user_prompt}, artistic style, expressive features, vibrant colors, professional photography, studio lighting, detailed appearance, modern presentation",
            f"A detailed avatar of {user_prompt}, photorealistic style, soft lighting, high resolution, portrait orientation, professional quality, clean background, expressive features"
        ]


class AvatarPromptGenerationView(APIView):
    """Generate three avatar prompt variations using OpenAI"""
    
    def post(self, request):
        """Generate three different avatar prompt variations based on user input"""
        try:
            # Get current user from JWT token
            user = get_current_user(request)
            if not user:
                return Response(
                    ResponseInfo.error("Authentication required"),
                    status=status.HTTP_401_UNAUTHORIZED
                )
            
            # Extract data from request
            prompt = request.data.get('prompt', '').strip()
            
            # Validate required fields
            if not prompt:
                return Response(
                    ResponseInfo.error("Prompt is required for prompt generation"),
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Generate three avatar prompt variations using OpenAI
            print("👤 Generating three avatar prompt variations with OpenAI...")
            prompt_variations = generate_three_avatar_prompts_with_openai(prompt)
            
            # Format the response
            response_data = {
                "original_prompt": prompt,
                "prompt_variations": prompt_variations,
                "generation_type": "avatar"
            }
            
            return Response(
                ResponseInfo.success(response_data, "Three avatar prompt variations generated successfully"),
                status=status.HTTP_200_OK
            )
            
        except Exception as e:
            print(f"❌ Error generating avatar prompt variations: {str(e)}")
            import traceback
            print(f"📋 Full traceback: {traceback.format_exc()}")
            return Response(
                ResponseInfo.error(f"Failed to generate avatar prompt variations: {str(e)}"),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

def generate_avatar_script_variations(base_script='', tone='', audience='', additional_context='', script_type_prompt=''):
    """Generate three enhanced avatar video scripts using OpenAI
    
    Args:
        base_script: Optional script provided by user for enhancement
        tone: Desired tone for the script
        audience: Target audience for the script
        additional_context: Additional context to incorporate
        script_type_prompt: User's description of what type of script they want (used when base_script is not provided)
    """
    try:
        openai_api_key = os.getenv('OPENAI_API_KEY')
        if not openai_api_key:
            print("⚠️ OpenAI API key not configured - returning fallback variations")
            if base_script and base_script.strip():
                base = base_script.strip()
                variations = [
                    base,
                    f"{base}\n\n[Style tweak: Emphasize clarity and friendly tone.]",
                    f"{base}\n\n[Call-to-action suggestion: Encourage the audience to take the next step.]"
                ]
                return [v.strip() for v in variations if v.strip()]
            else:
                # Fallback when no base script is provided
                return [
                    "Welcome to our presentation. Today, we'll explore exciting opportunities.",
                    "Thank you for joining us. Let's dive into what makes this special.",
                    "Hello and welcome. We're here to share something important with you."
                ]
        
        client = openai.OpenAI(api_key=openai_api_key)
        
        # Determine if we're enhancing an existing script or generating from scratch
        has_base_script = base_script and base_script.strip()
        has_script_type_prompt = script_type_prompt and script_type_prompt.strip()
        
        if has_base_script:
            # Mode 1: Enhance user-provided script
            system_prompt = (
                "You are an expert copywriter who crafts concise, engaging voiceover scripts for AI avatar videos. "
                "Your task is to enhance and polish the user's provided script while keeping the core message intact. "
                "Keep it conversational, ensure it sounds natural when spoken aloud, and always produce exactly three distinct script variations."
            )
        elif has_script_type_prompt:
            # Mode 2: Generate script based on user's description
            system_prompt = (
                "You are an expert copywriter who crafts concise, engaging voiceover scripts for AI avatar videos. "
                "Your task is to generate three distinct script variations based on the user's description of what type of script they want. "
                "Create scripts that are conversational, natural when spoken aloud, and tailored to the user's requirements. "
                "Always produce exactly three distinct script variations."
            )
        else:
            # Fallback: generate generic scripts
            system_prompt = (
                "You are an expert copywriter who crafts concise, engaging voiceover scripts for AI avatar videos. "
                "Create conversational scripts that sound natural when spoken aloud. "
                "Always produce exactly three distinct script variations."
            )
        
        tone_instruction = tone.strip() if tone else "Use a confident, friendly, and professional tone."
        audience_instruction = (
            f"The intended audience is: {audience.strip()}." if audience else
            "Write for a broad professional audience."
        )
        context_instruction = (
            f"Additional context to weave in: {additional_context.strip()}." if additional_context else
            "No additional context provided."
        )
        
        # Build user prompt based on the mode
        if has_base_script:
            # Enhancement mode
            user_prompt = f"""Original script provided by the user:
\"\"\"{base_script.strip()}\"\"\"

Please enhance and polish this script while keeping the core message intact. Produce THREE distinct improved scripts.

Guidelines:
- {tone_instruction}
- {audience_instruction}
- {context_instruction}
- Keep the length between 90 and 220 words unless the original script is shorter. If the original script is shorter, expand it naturally.
- Maintain the original language (if the input is Hindi, reply in Hindi, etc.).
- Use short, natural sentences that sound great when spoken.
- Include a clear call-to-action if the original script implies one.
- Do not add stage directions or camera instructions.
- Do not wrap the output in quotes or markdown.

Return exactly three enhanced scripts separated. Do NOT number them. Do NOT add extra text outside the scripts.

Enhanced Scripts:"""
        elif has_script_type_prompt:
            # Generation mode based on user's description
            user_prompt = f"""User's description of the script they want:
\"\"\"{script_type_prompt.strip()}\"\"\"

Please generate THREE distinct script variations based on the above description. Create engaging, natural-sounding voiceover scripts for AI avatar videos.

Guidelines:
- {tone_instruction}
- {audience_instruction}
- {context_instruction}
- Keep the length between 90 and 220 words.
- Use short, natural sentences that sound great when spoken.
- Include a clear call-to-action if appropriate for the script type.
- Do not add stage directions or camera instructions.
- Do not wrap the output in quotes or markdown.
- Ensure each variation has a distinct approach while meeting the user's requirements.

Return exactly three distinct scripts. Do NOT number them. Do NOT add extra text outside the scripts.

Generated Scripts:"""
        else:
            # Generic generation mode
            user_prompt = f"""Please generate THREE distinct engaging voiceover scripts for AI avatar videos.

Guidelines:
- {tone_instruction}
- {audience_instruction}
- {context_instruction}
- Keep the length between 90 and 220 words.
- Use short, natural sentences that sound great when spoken.
- Include a clear call-to-action.
- Do not add stage directions or camera instructions.
- Do not wrap the output in quotes or markdown.

Return exactly three distinct scripts. Do NOT number them. Do NOT add extra text outside the scripts.

Generated Scripts:"""
        
        max_retries = 3
        response = None
        for attempt in range(max_retries):
            try:
                response = client.chat.completions.create(
                    model="gpt-3.5-turbo",
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ],
                    temperature=0.75,
                    max_tokens=600,
                    top_p=0.9,
                    frequency_penalty=0.2,
                    presence_penalty=0.2
                )
                break
            except Exception as api_error:
                print(f"⚠️ Script generation attempt {attempt + 1} failed: {str(api_error)}")
                if attempt < max_retries - 1:
                    time.sleep(1)
                else:
                    raise api_error
        
        enhanced_output = response.choices[0].message.content.strip()
        # Handle both "Enhanced Scripts:" and "Generated Scripts:" prefixes
        if enhanced_output.lower().startswith("enhanced scripts:"):
            enhanced_output = enhanced_output.split(":", 1)[1].strip()
        elif enhanced_output.lower().startswith("generated scripts:"):
            enhanced_output = enhanced_output.split(":", 1)[1].strip()
        
        def _split_variations(text):
            if not text:
                return []
            cleaned = text.replace("|||", "<|||>")
            segments = [segment.strip() for segment in cleaned.split("<|||>") if segment.strip()]
            if len(segments) >= 3:
                return segments[:3]
            if len(segments) == 2:
                merged_segments = []
                for seg in segments:
                    parts = [part.strip(" -*\t") for part in seg.split("\n\n") if part.strip()]
                    if len(parts) > 1:
                        merged_segments.extend(parts)
                    else:
                        merged_segments.append(seg)
                if len(merged_segments) >= 3:
                    return merged_segments[:3]
            sentences = [sentence.strip() for sentence in cleaned.replace("\r", " ").split(". ") if sentence.strip()]
            if len(sentences) >= 3:
                approx_chunk = max(1, len(sentences) // 3)
                chunked = []
                for i in range(0, len(sentences), approx_chunk):
                    chunked.append(". ".join(sentences[i:i+approx_chunk]).strip())
                if len(chunked) >= 3:
                    return chunked[:3]
            paragraphs = [para.strip() for para in cleaned.split("\n\n") if para.strip()]
            if len(paragraphs) >= 3:
                return paragraphs[:3]
            return segments
        
        raw_variations = _split_variations(enhanced_output)

        if not raw_variations:
            # Fallback based on what was provided
            if base_script and base_script.strip():
                raw_variations = [base_script.strip()]
            elif script_type_prompt and script_type_prompt.strip():
                raw_variations = [
                    f"Script based on: {script_type_prompt.strip()[:100]}...",
                    f"Alternative approach for: {script_type_prompt.strip()[:100]}...",
                    f"Creative variation of: {script_type_prompt.strip()[:100]}..."
                ]
            else:
                raw_variations = [
                    "Welcome to our presentation. Today, we'll explore exciting opportunities.",
                    "Thank you for joining us. Let's dive into what makes this special.",
                    "Hello and welcome. We're here to share something important with you."
                ]
        
        print("=" * 80)
        print("📝 ENHANCED AVATAR SCRIPTS GENERATED")
        print("=" * 80)
        for idx, script in enumerate(raw_variations, 1):
            print(f"[Variant {idx}] {script[:120]}{'...' if len(script) > 120 else ''}")
        print("=" * 80)
        
        return raw_variations[:3]
    
    except Exception as e:
        print(f"Error generating avatar script: {str(e)}")
        import traceback
        print(f"📋 Full traceback: {traceback.format_exc()}")
        # Return appropriate fallback based on what was provided
        if base_script and base_script.strip():
            return [base_script.strip()]
        elif script_type_prompt and script_type_prompt.strip():
            return [
                f"Script based on: {script_type_prompt.strip()[:100]}...",
                f"Alternative approach for: {script_type_prompt.strip()[:100]}...",
                f"Creative variation of: {script_type_prompt.strip()[:100]}..."
            ]
        else:
            return [
                "Welcome to our presentation. Today, we'll explore exciting opportunities.",
                "Thank you for joining us. Let's dive into what makes this special.",
                "Hello and welcome. We're here to share something important with you."
            ]


def refine_avatar_script_with_openai(base_script, additional_details, tone=''):
    """Refine an avatar narration script with additional user-supplied details"""
    try:
        openai_api_key = os.getenv('OPENAI_API_KEY')
        if not openai_api_key:
            print("OpenAI API key not configured - returning combined script")
            return f"{base_script.strip()}\n\n[Incorporate: {additional_details.strip()}]"
        
        client = openai.OpenAI(api_key=openai_api_key)
        tone_instruction = tone.strip() if tone else "Keep the tone consistent with the original script."
        
        user_prompt = f"""Original avatar narration script:
\"\"\"{base_script.strip()}\"\"\"

Additional instructions from user:
\"\"\"{additional_details.strip()}\"\"\"

Task:
- Produce ONE refined version of the script that naturally incorporates the additional instructions.
- {tone_instruction}
- Keep it concise (90-220 words if possible), conversational, and suitable for AI avatar narration.
- Maintain the original language (if input is Hindi, reply in Hindi, etc.).
- Use short sentences that sound natural when spoken.
- Avoid adding scene directions, camera notes, or markdown formatting.
- Respond with ONLY the refined script (no preamble, numbering, or labels)."""
        
        max_retries = 3
        response = None
        for attempt in range(max_retries):
            try:
                response = client.chat.completions.create(
                    model="gpt-3.5-turbo",
                    messages=[
                        {"role": "user", "content": user_prompt}
                    ],
                    temperature=0.7,
                    max_tokens=500,
                    top_p=0.9,
                    frequency_penalty=0.1,
                    presence_penalty=0.2
                )
                break
            except Exception as api_error:
                print(f"Script refinement attempt {attempt + 1} failed: {str(api_error)}")
                if attempt < max_retries - 1:
                    time.sleep(1)
                else:
                    raise api_error
        
        refined_script = response.choices[0].message.content.strip()
        print("=" * 80)
        print("✨ REFINED AVATAR SCRIPT GENERATED")
        print("=" * 80)
        print(refined_script[:500])
        print("=" * 80)
        return refined_script
    
    except Exception as e:
        print(f"Error refining avatar script: {str(e)}")
        import traceback
        print(f"Full traceback: {traceback.format_exc()}")
        return f"{base_script.strip()}\n\n{additional_details.strip()}"


def refine_avatar_prompt_with_openai(base_prompt, additional_details):
    """Refine a selected avatar prompt with additional user details using OpenAI
    
    Args:
        base_prompt (str): The base avatar prompt selected by user
        additional_details (str): Additional details provided by user
        
    Returns:
        str: A refined avatar prompt that incorporates the additional details
    """
    try:
        # Get OpenAI API key
        openai_api_key = os.getenv('OPENAI_API_KEY')
        
        if not openai_api_key:
            print("⚠️ OpenAI API key not found - returning combined prompt")
            return f"{base_prompt}. {additional_details}"
        
        print("🔄 Refining avatar prompt with OpenAI...")
        print(f"📝 Base prompt: {base_prompt[:100]}...")
        print(f"➕ Additional details: {additional_details}")
        
        # Initialize OpenAI client
        client = openai.OpenAI(api_key=openai_api_key)
        
        # Create a simple refinement prompt focused on avatar appearance
        refinement_prompt = f"""You are an expert at refining avatar generation prompts for AI avatar generation tools.

BASE AVATAR PROMPT:
{base_prompt}

ADDITIONAL USER DETAILS TO INCORPORATE:
{additional_details}

YOUR TASK:
Create ONE refined avatar prompt that naturally incorporates the additional details into the base prompt. Keep it simple and focused on avatar appearance and characteristics.

CRITICAL RULES:

1. **FOCUS ON APPEARANCE**: This is an avatar prompt - focus on how the person looks, not complex artistic specifications.

2. **INCORPORATE USER DETAILS**: Seamlessly integrate the additional details about:
   - Physical appearance (hair, eyes, facial features, age, gender, ethnicity)
   - Clothing and style
   - Pose and expression
   - Background (if mentioned)
   - Any specific characteristics the user wants

3. **KEEP IT SIMPLE**: Do NOT add complex specifications like:
   - Cinematic styles
   - Vibes and moods (unless specifically requested)
   - Complex artistic techniques
   - Photography specifications (unless relevant)
   - Overly detailed lighting descriptions

4. **LENGTH**: Keep the refined prompt concise (30-100 words), clear, and descriptive.

5. **AVATAR-FOCUSED**: Focus on:
   - Physical characteristics
   - Appearance details
   - Clothing and style
   - Expression and pose
   - Simple, natural descriptions

6. **NATURAL LANGUAGE**: Write in natural, simple language that describes the avatar clearly.

EXAMPLE OF GOOD REFINED PROMPT:
"A professional portrait of a young woman with long brown wavy hair, bright green eyes, and a warm smile. She's wearing a blue blazer over a white shirt, professional business attire. Clean background, natural lighting, friendly expression."

NOW CREATE THE REFINED AVATAR PROMPT - Keep it simple, clear, and focused on appearance:

REFINED AVATAR PROMPT:"""

        # Call OpenAI API with retry logic
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = client.chat.completions.create(
                    model="gpt-3.5-turbo",
                    messages=[
                        {"role": "user", "content": refinement_prompt}
                    ],
                    max_tokens=300,  # Shorter for simpler prompts
                    temperature=0.7,  # Lower for more consistent results
                    top_p=0.9,
                    frequency_penalty=0.1,
                    presence_penalty=0.2
                )
                break  # Success, exit retry loop
            except Exception as api_error:
                if attempt < max_retries - 1:
                    print(f"⚠️ Attempt {attempt + 1} failed, retrying...")
                    time.sleep(1)
                else:
                    raise api_error
        
        # Get the refined prompt
        refined_prompt = response.choices[0].message.content.strip()
        
        # Remove "REFINED AVATAR PROMPT:" prefix if present
        if refined_prompt.startswith("REFINED AVATAR PROMPT:"):
            refined_prompt = refined_prompt.replace("REFINED AVATAR PROMPT:", "").strip()
        
        print("=" * 80)
        print("🎯 REFINED AVATAR PROMPT GENERATED:")
        print("=" * 80)
        print(refined_prompt)
        print("=" * 80)
        print(f"✅ Refined prompt length: {len(refined_prompt)} characters (~{len(refined_prompt.split())} words)")
        print("=" * 80)
        
        return refined_prompt
        
    except Exception as e:
        print(f"❌ Error refining avatar prompt: {str(e)}")
        import traceback
        print(f"📋 Full traceback: {traceback.format_exc()}")
        # Return combined prompt as fallback
        return f"{base_prompt}. {additional_details}"


class RefineAvatarPromptView(APIView):
    """Refine a selected avatar prompt with additional details"""
    
    def post(self, request):
        """Refine an avatar prompt by incorporating additional user details"""
        try:
            # Get current user from JWT token
            user = get_current_user(request)
            if not user:
                return Response(
                    ResponseInfo.error("Authentication required"),
                    status=status.HTTP_401_UNAUTHORIZED
                )
            
            # Extract data from request
            base_prompt = request.data.get('base_prompt', '').strip()
            additional_details = request.data.get('additional_details', '').strip()
            
            # Validate required fields
            if not base_prompt:
                return Response(
                    ResponseInfo.error("Base prompt is required"),
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            if not additional_details:
                return Response(
                    ResponseInfo.error("Additional details are required to refine the prompt"),
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Refine the avatar prompt using OpenAI
            print(f"🔧 Refining avatar prompt with additional details...")
            print(f"📝 Base prompt: {base_prompt[:100]}...")
            print(f"➕ Additional details: {additional_details}")
            
            refined_prompt = refine_avatar_prompt_with_openai(base_prompt, additional_details)
            
            response_data = {
                "base_prompt": base_prompt,
                "additional_details": additional_details,
                "refined_prompt": refined_prompt
            }
            
            return Response(
                ResponseInfo.success(response_data, "Avatar prompt refined successfully"),
                status=status.HTTP_200_OK
            )
            
        except Exception as e:
            print(f"❌ Error refining avatar prompt: {str(e)}")
            import traceback
            print(f"📋 Full traceback: {traceback.format_exc()}")
            return Response(
                ResponseInfo.error(f"Failed to refine avatar prompt: {str(e)}"),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class AvatarScriptGenerationView(APIView):
    """Generate an enhanced script for avatar video narration
    
    Accepts either:
    - 'script': User-provided script to enhance
    - 'script_type_prompt': Description of what type of script the user wants (used when script is not provided)
    """
    
    def post(self, request):
        try:
            user = get_current_user(request)
            if not user:
                return Response(
                    ResponseInfo.error("Authentication required"),
                    status=status.HTTP_401_UNAUTHORIZED
                )
            
            script_text = request.data.get('script', '')
            script_type_prompt = request.data.get('script_type_prompt', '')
            tone = request.data.get('tone', '')
            audience = request.data.get('audience', '')
            additional_context = request.data.get('additional_context', '')
            
            # Validate that at least one of script or script_type_prompt is provided
            has_script = script_text and script_text.strip()
            has_script_type_prompt = script_type_prompt and script_type_prompt.strip()
            
            if not has_script and not has_script_type_prompt:
                return Response(
                    ResponseInfo.error("Either 'script' (for enhancement) or 'script_type_prompt' (for generation) is required"),
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            print("🧠 Generating enhanced avatar script with OpenAI...")
            script_variations = generate_avatar_script_variations(
                base_script=script_text if has_script else '',
                tone=tone,
                audience=audience,
                additional_context=additional_context,
                script_type_prompt=script_type_prompt if has_script_type_prompt else ''
            )
            
            response_data = {
                "script_variations": script_variations,
                "metadata": {
                    "tone": tone,
                    "audience": audience,
                    "additional_context": additional_context,
                    "mode": "enhancement" if has_script else "generation"
                }
            }
            
            # Include original script or script type prompt in response
            if has_script:
                response_data["original_script"] = script_text
            if has_script_type_prompt:
                response_data["script_type_prompt"] = script_type_prompt
            
            return Response(
                ResponseInfo.success(response_data, "Script variations generated successfully"),
                status=status.HTTP_200_OK
            )
        
        except Exception as e:
            print(f"❌ Error generating enhanced avatar script: {str(e)}")
            import traceback
            print(f"📋 Full traceback: {traceback.format_exc()}")
            return Response(
                ResponseInfo.error(f"Failed to generate script: {str(e)}"),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class AvatarScriptRefinementView(APIView):
    """Refine a selected avatar script with additional details"""
    
    def post(self, request):
        try:
            user = get_current_user(request)
            if not user:
                return Response(
                    ResponseInfo.error("Authentication required"),
                    status=status.HTTP_401_UNAUTHORIZED
                )
            
            base_script = request.data.get('base_script', '')
            additional_details = request.data.get('additional_details', '')
            tone = request.data.get('tone', '')
            
            if not base_script or not base_script.strip():
                return Response(
                    ResponseInfo.error("Base script is required"),
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            if not additional_details or not additional_details.strip():
                return Response(
                    ResponseInfo.error("Additional details are required to refine the script"),
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            refined_script = refine_avatar_script_with_openai(
                base_script,
                additional_details,
                tone=tone
            )
            
            response_data = {
                "base_script": base_script,
                "additional_details": additional_details,
                "refined_script": refined_script
            }
            
            return Response(
                ResponseInfo.success(response_data, "Avatar script refined successfully"),
                status=status.HTTP_200_OK
            )
        
        except Exception as e:
            print(f"❌ Error refining avatar script: {str(e)}")
            import traceback
            print(f"📋 Full traceback: {traceback.format_exc()}")
            return Response(
                ResponseInfo.error(f"Failed to refine script: {str(e)}"),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
