import os
import uuid
import base64
import warnings
import threading
import requests
import time
import openai
from django.conf import settings
from django.utils import timezone
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
                        # Avatar generation completed
                        try:
                            job.status = "completed"
                            job.progress = 100
                            job.completed_at = timezone.now()
                            if avatar_url:
                                # Truncate URL if it's too long
                                job.avatar_url = avatar_url[:2000] if len(avatar_url) > 2000 else avatar_url
                            
                            # Extract and save avatar_id if available (truncate to 255 chars)
                            avatar_id_value = status_data.get('data', {}).get('avatar_id') or status_data.get('avatar_id')
                            if avatar_id_value:
                                job.avatar_id = str(avatar_id_value)[:255]
                            
                            # For prompt-based generation (no image_key), upload the generated avatar to HeyGen
                            # Refresh job from DB to avoid race conditions with concurrent status checks
                            job.refresh_from_db()
                            
                            if not job.image_key and avatar_url:
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
                                                
                                                # Save image_key immediately to prevent duplicate uploads
                                                job.image_key = image_key
                                                job.save(update_fields=['image_key'])
                                                
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
                                                        job.generation_id = str(group_id)  # Update to avatar_group ID
                                                        # Keep the original avatar_url for display
                                                    else:
                                                        print(f"⚠️ Failed to get group_id from avatar_group response: {avatar_group_response.text[:200]}")
                                                else:
                                                    print(f"⚠️ Failed to create avatar_group: {avatar_group_response.status_code} - {avatar_group_response.text[:200]}")
                                            else:
                                                print(f"⚠️ No image_key in upload response: {upload_data}")
                                        else:
                                            print(f"⚠️ Failed to upload image to HeyGen: {upload_response.status_code} - {upload_response.text[:200]}")
                                    else:
                                        print(f"⚠️ Failed to download image from {avatar_url}: {img_response.status_code}")
                                except Exception as upload_error:
                                    print(f"⚠️ Error uploading generated avatar to HeyGen: {str(upload_error)}")
                                    import traceback
                                    print(traceback.format_exc())
                                    # Continue anyway - at least we have the avatar_url
                            
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
        job.error_message = "Avatar generation timed out after 5 minutes"
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
                
                # Validate required video fields
                if not video_payload.get('video_inputs') or len(video_payload.get('video_inputs', [])) == 0:
                    return Response(
                        ResponseInfo.error("video_inputs is required for video generation"),
                        status=status.HTTP_400_BAD_REQUEST
                    )
                
                video_input = video_payload['video_inputs'][0]
                if not video_input.get('voice') or not video_input['voice'].get('voice_id'):
                    return Response(
                        ResponseInfo.error("voice_id is required in video_inputs"),
                        status=status.HTTP_400_BAD_REQUEST
                    )
                
                if not video_input.get('character') or not (video_input['character'].get('avatar_id') or video_input['character'].get('talking_photo_id')):
                    return Response(
                        ResponseInfo.error("avatar_id or talking_photo_id is required in character"),
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
                    name=video_input.get('voice', {}).get('input_text', 'Avatar Video')[:100],
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
            
            # Call HeyGen video generation API
            video_url = "https://api.heygen.com/v2/video/generate"
            headers = {
                "accept": "application/json",
                "content-type": "application/json",
                "x-api-key": api_key
            }
            
            print(f"📤 Calling HeyGen video generation API...")
            print(f"📋 Payload: {video_payload}")
            
            try:
                response = requests.post(video_url, json=video_payload, headers=headers, timeout=60)
                
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
                    error_msg = f"HeyGen API returned status {response.status_code}: {response.text[:500]}"
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
        max_attempts = 120  # Check for up to 10 minutes (120 * 5 seconds)
        attempt = 0
        
        while attempt < max_attempts:
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
                        # Video failed
                        error_msg = status_data.get('data', {}).get('message') or status_data.get('message', 'Video generation failed')
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
                        print(f"⏳ Video still processing... (attempt {attempt + 1}/{max_attempts})")
                
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
                if attempt >= max_attempts:
                    break
                time.sleep(5)
        
        # Timeout - mark as error
        try:
            job = AvatarGenerationJob.objects.get(job_id=job_id)
            job.status = "error"
            job.error_message = "Video generation timed out after 10 minutes"
            job.completed_at = timezone.now()
            job.save()
            print(f"⏱️ Job {job_id} timed out")
        except AvatarGenerationJob.DoesNotExist:
            pass


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
                                    # Video failed
                                    error_msg = (
                                        status_data.get('data', {}).get('message') or 
                                        status_data.get('message') or 
                                        status_data.get('data', {}).get('error') or
                                        status_data.get('error') or
                                        'Video generation failed'
                                    )
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
                                    job.status = "completed"
                                    job.progress = 100
                                    if avatar_url:
                                        job.avatar_url = avatar_url
                                    if not job.completed_at:
                                        job.completed_at = timezone.now()
                                    
                                    # For prompt-based generation (no image_key), upload the generated avatar to HeyGen
                                    # Refresh job from DB to avoid race conditions with concurrent uploads
                                    job.refresh_from_db()
                                    
                                    if not job.image_key and avatar_url:
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
                                                        
                                                        # Save image_key immediately to prevent duplicate uploads
                                                        job.image_key = image_key
                                                        job.save(update_fields=['image_key'])
                                                        
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
                                                                job.generation_id = str(group_id)  # Update to avatar_group ID
                                                                # Keep the original avatar_url for display
                                                            else:
                                                                print(f"⚠️ AvatarStatusView: Failed to get group_id from avatar_group response: {avatar_group_response.text[:200]}")
                                                        else:
                                                            print(f"⚠️ AvatarStatusView: Failed to create avatar_group: {avatar_group_response.status_code} - {avatar_group_response.text[:200]}")
                                                    else:
                                                        print(f"⚠️ AvatarStatusView: No image_key in upload response: {upload_data}")
                                                else:
                                                    print(f"⚠️ AvatarStatusView: Failed to upload image to HeyGen: {upload_response.status_code} - {upload_response.text[:200]}")
                                            else:
                                                print(f"⚠️ AvatarStatusView: Failed to download image from {avatar_url}: {img_response.status_code}")
                                        except Exception as upload_error:
                                            print(f"⚠️ AvatarStatusView: Error uploading generated avatar to HeyGen: {str(upload_error)}")
                                            import traceback
                                            print(traceback.format_exc())
                                            # Continue anyway - at least we have the avatar_url
                                    
                                    job.save()
                                    print(f"✅ AvatarStatusView: Job {job_id} updated to completed!")
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
                "thumbnail_url": job.note if job.provider == 'heygen_video' and job.note and (job.note.startswith('http') or job.note.startswith('https')) else None  # For video jobs, note contains thumbnail URL
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
                    "thumbnail_url": job.note if job.provider == 'heygen_video' and job.note and (job.note.startswith('http') or job.note.startswith('https')) else None  # For video jobs, note contains thumbnail URL
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

