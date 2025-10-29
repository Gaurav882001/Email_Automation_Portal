import os
import uuid
import time
import threading
import warnings
import base64
import tempfile
from datetime import datetime
from django.conf import settings
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.parsers import MultiPartParser, FormParser
from dotenv import load_dotenv
from google import genai
from google.genai import types
from PIL import Image
from io import BytesIO

from utils.response import ResponseInfo
from utils.jwt_utils import verify_jwt_token
from image_gen.models import VideoGenerationJob, VideoReferenceImage
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


def generate_video_with_veo(job_id, prompt, duration):
    """Generate video using Google Veo 3.1 API with optional reference images"""
    try:
        # Get API key from environment
        gemini_api_key = os.getenv('NANO_BANANA_API_KEY')  # Using same key as image generation
        if not gemini_api_key:
            raise Exception("Google Gemini API key not configured. Please add NANO_BANANA_API_KEY to your .env file")
        
        print(f"🎬 Starting video generation for job {job_id}")
        print(f"📝 Prompt: {prompt}")
        print(f"⏱️ Duration: {duration} seconds")
        print(f"🔑 API Key: {'Present' if gemini_api_key else 'Missing'}")
        
        # Initialize the Google GenAI client
        client = genai.Client(api_key=gemini_api_key)
        
        # Update job status to processing
        job = VideoGenerationJob.objects.get(job_id=job_id)
        job.status = 'processing'
        job.started_at = datetime.now()
        job.progress = 10
        job.save()
        
        # Get reference images if any
        reference_images = VideoReferenceImage.objects.filter(job=job)
        ref_count = reference_images.count()
        print(f"📸 Reference images count: {ref_count}/3 (max allowed by Veo 3.1)")
        
        # Validate reference image count
        if ref_count > 3:
            raise Exception(f"Too many reference images ({ref_count}). Veo 3.1 supports maximum 3 reference images.")
        
        # Prepare reference images list for video generation config
        reference_image_objects = []
        
        # Process reference images if provided
        if reference_images.exists():
            print("🖼️ Processing and uploading reference images...")
            
            for idx, ref_img in enumerate(reference_images, 1):
                try:
                    print(f"  📸 Processing reference image {idx}: {ref_img.filename}")
                    
                    # Decode base64 image data
                    try:
                        image_data_bytes = base64.b64decode(ref_img.image_data)
                        print(f"     - Decoded image data size: {len(image_data_bytes)} bytes")
                    except Exception as decode_error:
                        print(f"     - Base64 decode error: {str(decode_error)}")
                        print(f"     - Image data length: {len(ref_img.image_data) if ref_img.image_data else 0}")
                        print(f"     - First 100 chars of image_data: {ref_img.image_data[:100] if ref_img.image_data else 'None'}")
                        raise Exception(f"Failed to decode base64 image data: {str(decode_error)}")
                    
                    # Load image with PIL
                    try:
                        pil_image = Image.open(BytesIO(image_data_bytes))
                    except Exception as pil_error:
                        print(f"     - PIL open error: {str(pil_error)}")
                        print(f"     - Decoded bytes first 20: {image_data_bytes[:20]}")
                        raise Exception(f"Failed to open image with PIL: {str(pil_error)}")
                    
                    # Convert to RGB mode if necessary
                    if pil_image.mode not in ('RGB', 'RGBA'):
                        print(f"     - Converting from {pil_image.mode} to RGB mode")
                        pil_image = pil_image.convert('RGB')
                    
                    print(f"     - Size: {pil_image.size}, Mode: {pil_image.mode}")
                    
                    # Convert image to JPEG bytes for Veo 3.1
                    output = BytesIO()
                    if pil_image.mode == 'RGBA':
                        # Convert RGBA to RGB for JPEG
                        rgb_image = Image.new('RGB', pil_image.size, (255, 255, 255))
                        rgb_image.paste(pil_image, mask=pil_image.split()[3])
                        rgb_image.save(output, format='JPEG', quality=95)
                    else:
                        pil_image.save(output, format='JPEG', quality=95)
                    
                    # Get the JPEG bytes
                    jpeg_bytes = output.getvalue()
                    
                    print(f"     - Converted to JPEG (size: {len(jpeg_bytes)} bytes)")
                    
                    # Create Image object with raw bytes (not base64)
                    # The types.Image expects raw bytes, not base64 encoded
                    image_obj = types.Image(
                        image_bytes=jpeg_bytes,
                        mime_type="image/jpeg"
                    )
                    
                    # Create VideoGenerationReferenceImage object
                    ref_image_obj = types.VideoGenerationReferenceImage(
                        image=image_obj,
                        reference_type=ref_img.reference_type
                    )
                    reference_image_objects.append(ref_image_obj)
                    print(f"  ✓ Added reference image {idx}: {ref_img.filename}")
                    
                except Exception as img_error:
                    print(f"  ❌ Error processing reference image {ref_img.filename}: {str(img_error)}")
                    import traceback
                    print(f"     Traceback: {traceback.format_exc()}")
                    raise Exception(f"Failed to process reference image {ref_img.filename}: {str(img_error)}")
            
            print(f"✅ All {ref_count} reference images uploaded successfully")
        
        # Generate video using Veo 3.1
        print(f"🎬 Calling Veo 3.1 API...")
        if reference_images.exists():
            print(f"   - Reference images: {ref_count}")
            print(f"   - Prompt: {prompt[:100]}...")
            
            # Generate with reference images using config with correct parameter name
            operation = client.models.generate_videos(
                model="veo-3.1-generate-preview",
                prompt=prompt,
                config=types.GenerateVideosConfig(
                    reference_images=reference_image_objects  # Use plural 'reference_images'
                )
            )
        else:
            print(f"   - Prompt only: {prompt[:100]}...")
            
            # Generate with prompt only
            operation = client.models.generate_videos(
                model="veo-3.1-generate-preview",
                prompt=prompt,
            )
        
        # Update progress
        job.progress = 30
        job.save()
        
        # Poll the operation status until the video is ready
        print(f"⏳ Waiting for video generation to complete...")
        while not operation.done:
            print(f"   - Still processing... (progress: {job.progress}%)")
            time.sleep(10)
            operation = client.operations.get(operation)
            
            # Update progress (gradual increase)
            if job.progress < 80:
                job.progress += 10
                job.save()
        
        # Update progress to 90%
        job.progress = 90
        job.save()
        
        print(f"✅ Video generation completed, downloading...")
        
        # Check if operation has response and generated_videos
        if not operation.response:
            raise Exception("No response from video generation operation")
        
        if not hasattr(operation.response, 'generated_videos') or not operation.response.generated_videos:
            # Log the response structure for debugging
            print(f"⚠️ Response structure: {dir(operation.response)}")
            print(f"⚠️ Response content: {operation.response}")
            raise Exception("No generated videos in response")
        
        # Download the generated video
        generated_video = operation.response.generated_videos[0]
        print(f"📹 Generated video object: {generated_video}")
        
        # Create filename for the video
        video_filename = f"video_{job_id}_{int(time.time())}.mp4"
        
        # Download video content
        if hasattr(generated_video, 'video'):
            video_content = client.files.download(file=generated_video.video)
        elif hasattr(generated_video, 'uri'):
            # Alternative: download from URI
            video_content = client.files.download(name=generated_video.uri)
        else:
            print(f"⚠️ Video object structure: {dir(generated_video)}")
            raise Exception(f"Cannot find video content in generated_video object")
        
        # Save video to media directory
        video_path = f"generated_videos/{video_filename}"
        full_path = os.path.join(settings.MEDIA_ROOT, video_path)
        
        # Ensure directory exists
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        
        # Save video file
        with open(full_path, 'wb') as f:
            f.write(video_content)
        
        print(f"💾 Video saved: {video_path} (size: {len(video_content)} bytes)")
        
        # Update job with completion details
        job.status = 'completed'
        job.completed_at = datetime.now()
        job.progress = 100
        job.video_file_path = video_path
        job.video_url = f"{settings.MEDIA_URL}{video_path}"
        job.save()
        
        print(f"✅ Video generation completed successfully for job {job_id}")
        
    except Exception as e:
        print(f"❌ Error generating video for job {job_id}: {str(e)}")
        import traceback
        print(f"📋 Full traceback: {traceback.format_exc()}")
        
        # Update job with error
        job = VideoGenerationJob.objects.get(job_id=job_id)
        job.status = 'failed'
        job.error_message = str(e)
        job.completed_at = datetime.now()
        job.save()


class VideoGenerationView(APIView):
    """API view for generating videos using Google Veo 3.1"""
    parser_classes = [MultiPartParser, FormParser]
    
    def post(self, request):
        try:
            print(f"🎬 Video generation request received")
            print(f"📊 Request data: {request.data}")
            
            # Get current user
            user = get_current_user(request)
            print(f"👤 User: {user.email if user else 'Anonymous'}")
            
            # Extract form data
            prompt = request.data.get('prompt', '').strip()
            style = request.data.get('style', 'realistic')
            quality = request.data.get('quality', 'high')
            duration = int(request.data.get('duration', 5))
            
            print(f"📝 Extracted data - Prompt: {prompt}, Style: {style}, Quality: {quality}, Duration: {duration}")
            
            # Validate required fields
            if not prompt:
                print("❌ Validation failed: Prompt is required")
                return Response(
                    ResponseInfo.error("Prompt is required"),
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Validate duration
            if duration not in [5, 10, 15, 30, 60]:
                print(f"⚠️ Invalid duration {duration}, defaulting to 5 seconds")
                duration = 5  # Default to 5 seconds if invalid
            
            # Create video generation job
            job = VideoGenerationJob.objects.create(
                user=user,
                prompt=prompt,
                style=style,
                quality=quality,
                duration=duration,
                status='queued'
            )
            
            print(f"✅ Video job created with ID: {job.job_id}")
            
            # Process reference images if provided
            reference_image_count = 0
            reference_image_keys = [key for key in request.FILES.keys() if key.startswith('reference_image_')]
            
            # Validate reference image count (Veo 3.1 supports max 3 reference images)
            if len(reference_image_keys) > 3:
                print(f"❌ Too many reference images: {len(reference_image_keys)}. Maximum is 3.")
                job.delete()  # Clean up the created job
                return Response(
                    ResponseInfo.error("Maximum 3 reference images allowed for video generation"),
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            for key in reference_image_keys:
                reference_image_count += 1
                image_file = request.FILES[key]
                
                # Read and encode image
                image_data = image_file.read()
                
                # Validate that it's a valid image before storing
                try:
                    test_image = Image.open(BytesIO(image_data))
                    test_image.verify()  # Verify it's a valid image
                    print(f"  ✓ Validated image: {image_file.name} ({test_image.format}, {test_image.size})")
                except Exception as validation_error:
                    print(f"  ❌ Invalid image file {image_file.name}: {str(validation_error)}")
                    job.delete()  # Clean up the created job
                    return Response(
                        ResponseInfo.error(f"Invalid image file '{image_file.name}': {str(validation_error)}"),
                        status=status.HTTP_400_BAD_REQUEST
                    )
                
                # Re-read the file since verify() consumed it
                image_file.seek(0)
                image_data = image_file.read()
                
                image_base64 = base64.b64encode(image_data).decode('utf-8')
                
                # Store reference image
                VideoReferenceImage.objects.create(
                    job=job,
                    image_data=image_base64,
                    filename=image_file.name,
                    content_type=image_file.content_type,
                    reference_type='asset'  # Default to 'asset' as per Google's example
                )
                print(f"  📸 Stored reference image {reference_image_count}/3: {image_file.name}")
            
            if reference_image_count > 0:
                print(f"✅ {reference_image_count} reference images stored for job {job.job_id} (max 3 allowed)")
            else:
                print(f"ℹ️ No reference images provided for job {job.job_id}")
            
            # Start video generation in background thread
            thread = threading.Thread(
                target=generate_video_with_veo,
                args=(job.job_id, prompt, duration)
            )
            thread.daemon = True
            thread.start()
            
            print(f"🚀 Background thread started for job {job.job_id}")
            
            return Response(
                ResponseInfo.success({
                    'job_id': str(job.job_id),
                    'status': job.status,
                    'prompt': prompt,
                    'style': style,
                    'quality': quality,
                    'duration': duration
                }, "Video generation started successfully"),
                status=status.HTTP_201_CREATED
            )
            
        except Exception as e:
            print(f"❌ Error in VideoGenerationView: {str(e)}")
            import traceback
            print(f"📋 Full traceback: {traceback.format_exc()}")
            return Response(
                ResponseInfo.error(f"Failed to start video generation: {str(e)}"),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class VideoStatusView(APIView):
    """API view for checking video generation status"""
    
    def get(self, request, job_id):
        try:
            # Get current user
            user = get_current_user(request)
            
            # Get job
            try:
                job = VideoGenerationJob.objects.get(job_id=job_id)
                
                # Check if user has permission to view this job
                if user and job.user and job.user != user:
                    return Response(
ResponseInfo.error("Access denied"),
                        status=status.HTTP_403_FORBIDDEN
                    )
                
                return Response(
                    ResponseInfo.success({
                            'job_id': str(job.job_id),
                            'status': job.status,
                            'progress': job.progress,
                            'prompt': job.prompt,
                            'style': job.style,
                            'quality': job.quality,
                            'duration': job.duration,
                            'created_at': job.created_at.isoformat(),
                            'started_at': job.started_at.isoformat() if job.started_at else None,
                            'completed_at': job.completed_at.isoformat() if job.completed_at else None,
                            'video_url': job.video_url,
                            'error_message': job.error_message
                        }, "Job status retrieved successfully"),
                    status=status.HTTP_200_OK
                )
                
            except VideoGenerationJob.DoesNotExist:
                return Response(
                    ResponseInfo.error("Job not found"),
                    status=status.HTTP_404_NOT_FOUND
                )
                
        except Exception as e:
            print(f"Error in VideoStatusView: {str(e)}")
            return Response(
                ResponseInfo.error(f"Failed to get job status: {str(e)}"),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class VideoJobListView(APIView):
    """API view for listing all video generation jobs"""
    
    def get(self, request):
        try:
            # Get current user
            user = get_current_user(request)
            
            # Get all jobs for the user (or all jobs if no user)
            if user:
                jobs = VideoGenerationJob.objects.filter(user=user)
            else:
                jobs = VideoGenerationJob.objects.all()
            
            # Convert to list of dictionaries
            jobs_data = []
            for job in jobs:
                jobs_data.append({
                    'job_id': str(job.job_id),
                    'status': job.status,
                    'progress': job.progress,
                    'prompt': job.prompt,
                    'style': job.style,
                    'quality': job.quality,
                    'duration': job.duration,
                    'created_at': job.created_at.isoformat(),
                    'started_at': job.started_at.isoformat() if job.started_at else None,
                    'completed_at': job.completed_at.isoformat() if job.completed_at else None,
                    'video_url': job.video_url,
                    'provider': job.provider,
                    'error_message': job.error_message
                })
            
            return Response(
                ResponseInfo.success(jobs_data, "Jobs retrieved successfully"),
                status=status.HTTP_200_OK
            )
            
        except Exception as e:
            print(f"Error in VideoJobListView: {str(e)}")
            return Response(
                ResponseInfo.error(f"Failed to get jobs: {str(e)}"),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class VideoRetryJobView(APIView):
    """API view for retrying failed video generation jobs"""
    
    def post(self, request, job_id):
        try:
            # Get current user
            user = get_current_user(request)
            
            # Get job
            try:
                job = VideoGenerationJob.objects.get(job_id=job_id)
                
                # Check if user has permission to retry this job
                if user and job.user and job.user != user:
                    return Response(
ResponseInfo.error("Access denied"),
                        status=status.HTTP_403_FORBIDDEN
                    )
                
                # Check if job can be retried
                if job.status not in ['failed', 'queued']:
                    return Response(
ResponseInfo.error("Job cannot be retried in current status"),
                        status=status.HTTP_400_BAD_REQUEST
                    )
                
                # Reset job status
                job.status = 'queued'
                job.progress = 0
                job.started_at = None
                job.completed_at = None
                job.error_message = None
                job.video_url = None
                job.video_file_path = None
                job.save()
                
                # Start video generation in background thread
                thread = threading.Thread(
                    target=generate_video_with_veo,
                    args=(job.job_id, job.prompt, job.duration)
                )
                thread.daemon = True
                thread.start()
                
                return Response(
                    ResponseInfo.success({
                            'job_id': str(job.job_id),
                            'status': job.status
                        }, "Job retry started successfully"),
                    status=status.HTTP_200_OK
                )
                
            except VideoGenerationJob.DoesNotExist:
                return Response(
                    ResponseInfo.error("Job not found"),
                    status=status.HTTP_404_NOT_FOUND
                )
                
        except Exception as e:
            print(f"Error in VideoRetryJobView: {str(e)}")
            return Response(
                ResponseInfo.error(f"Failed to retry job: {str(e)}"),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class VideoDeleteJobView(APIView):
    """API view for deleting video generation jobs"""
    
    def delete(self, request, job_id):
        try:
            # Get current user
            user = get_current_user(request)
            
            # Get job
            try:
                job = VideoGenerationJob.objects.get(job_id=job_id)
                
                # Check if user has permission to delete this job
                if user and job.user and job.user != user:
                    return Response(
ResponseInfo.error("Access denied"),
                        status=status.HTTP_403_FORBIDDEN
                    )
                
                # Delete video file if it exists
                if job.video_file_path:
                    try:
                        full_path = os.path.join(settings.MEDIA_ROOT, job.video_file_path)
                        if os.path.exists(full_path):
                            os.remove(full_path)
                    except Exception as e:
                        print(f"Error deleting video file: {str(e)}")
                
                # Delete job
                job.delete()
                
                return Response(
ResponseInfo.success("Job deleted successfully"),
                    status=status.HTTP_200_OK
                )
                
            except VideoGenerationJob.DoesNotExist:
                return Response(
                    ResponseInfo.error("Job not found"),
                    status=status.HTTP_404_NOT_FOUND
                )
                
        except Exception as e:
            print(f"Error in VideoDeleteJobView: {str(e)}")
            return Response(
                ResponseInfo.error(f"Failed to delete job: {str(e)}"),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class VideoDashboardStatsView(APIView):
    """API view for getting video generation dashboard statistics"""
    
    def get(self, request):
        try:
            # Get current user
            user = get_current_user(request)
            
            # Get jobs for the user (or all jobs if no user)
            if user:
                jobs = VideoGenerationJob.objects.filter(user=user)
            else:
                jobs = VideoGenerationJob.objects.all()
            
            # Calculate statistics
            total_jobs = jobs.count()
            completed_jobs = jobs.filter(status='completed').count()
            processing_jobs = jobs.filter(status='processing').count()
            failed_jobs = jobs.filter(status='failed').count()
            queued_jobs = jobs.filter(status='queued').count()
            
            # Calculate success rate
            success_rate = (completed_jobs / total_jobs * 100) if total_jobs > 0 else 0
            
            stats = {
                'total_jobs': total_jobs,
                'completed_jobs': completed_jobs,
                'processing_jobs': processing_jobs,
                'failed_jobs': failed_jobs,
                'queued_jobs': queued_jobs,
                'success_rate': round(success_rate, 2)
            }
            
            return Response(
                ResponseInfo.success(stats, "Statistics retrieved successfully"),
                status=status.HTTP_200_OK
            )
            
        except Exception as e:
            print(f"Error in VideoDashboardStatsView: {str(e)}")
            return Response(
                ResponseInfo.error(f"Failed to get statistics: {str(e)}"),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
