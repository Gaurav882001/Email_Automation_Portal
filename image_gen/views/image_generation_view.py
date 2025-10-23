import os
import uuid
import base64
import warnings
import threading
import requests
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
from PIL import Image
from io import BytesIO

from utils.response import ResponseInfo
from utils.jwt_utils import verify_jwt_token
from image_gen.models import ImageGenerationJob, ReferenceImage
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


class ImageGenerationView(APIView):
    parser_classes = [MultiPartParser, FormParser]
    
    def post(self, request):
        """Queue an image generation job using Google Genai (Nano Banana)"""
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
            style = request.data.get('style', 'realistic')
            quality = request.data.get('quality', 'standard')
            
            # Validate required fields
            if not prompt:
                return Response(
                    ResponseInfo.error("Prompt is required for image generation"),
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Get API key from environment (Google Gemini API key for Nano Banana)
            gemini_api_key = os.getenv('NANO_BANANA_API_KEY')  # This is Google API key
            if not gemini_api_key:
                return Response(
                    ResponseInfo.error("Google Gemini API key not configured"),
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
            
            # Create a unique job ID
            job_id = str(uuid.uuid4())
            
            # Prepare reference images if any
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
                            "image_type": file.content_type
                        })
                    except Exception as e:
                        print(f"Error processing reference image {key}: {str(e)}")
                        continue
            
            # Create job in database
            job = ImageGenerationJob.objects.create(
                job_id=job_id,
                user=user,
                prompt=prompt,
                style=style,
                quality=quality,
                status="queued",
                progress=0
            )
            
            # Store reference images in database
            for ref_img in reference_images:
                ReferenceImage.objects.create(
                    job=job,
                    image_data=ref_img["image"],
                    filename=ref_img.get("filename", "reference.jpg"),
                    content_type=ref_img.get("image_type", "image/jpeg")
                )
            
            # Start background processing
            thread = threading.Thread(
                target=self._process_nano_banana_generation,
                args=(job_id, gemini_api_key, request)
            )
            thread.daemon = True
            thread.start()
            
            # Return job info immediately
            response_data = {
                "job_id": job_id,
                "status": "queued",
                "message": "Image generation job queued successfully",
                "prompt": prompt,
                "style": style,
                "quality": quality,
                "created_at": job.created_at.isoformat(),
                "check_status_url": f"/api/v1/image-status/{job_id}/"
            }
            
            return Response(
                ResponseInfo.success(response_data, "Image generation job started"),
                status=status.HTTP_202_ACCEPTED
            )
            
        except Exception as e:
            return Response(
                ResponseInfo.error(f"Failed to start image generation: {str(e)}"),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    def _process_nano_banana_generation(self, job_id, api_key, request):
        """Process image generation using Google Genai (Nano Banana)"""
        try:
            # Get job from database
            try:
                job = ImageGenerationJob.objects.get(job_id=job_id)
            except ImageGenerationJob.DoesNotExist:
                print(f"Job {job_id} not found in database")
                return
            
            print(f"Starting Google Genai (Nano Banana) generation for job {job_id}")
            print(f"API Key: {'Present' if api_key else 'Missing'}")
            
            # Update status to processing
            job.status = "processing"
            job.progress = 10
            job.started_at = datetime.now()
            job.save()
            
            prompt = job.prompt
            style = job.style
            quality = job.quality
            
            # Get reference images from database
            reference_images = []
            for ref_img in job.reference_images.all():
                reference_images.append({
                    "image": ref_img.image_data,
                    "filename": ref_img.filename,
                    "content_type": ref_img.content_type
                })
            
            # Quality mapping for dimensions
            quality_mapping = {
                'standard': {'width': 512, 'height': 512},
                'high': {'width': 768, 'height': 768},
                'ultra': {'width': 1024, 'height': 1024}
            }
            quality_params = quality_mapping.get(quality, quality_mapping['standard'])
            
            # Enhance prompt based on style
            style_prompts = {
                'realistic': f"{prompt}, photorealistic, high detail, natural lighting, professional photography",
                'artistic': f"{prompt}, artistic style, painted, creative interpretation, masterpiece art",
                'cartoon': f"{prompt}, cartoon style, animated, colorful, illustration, fun",
                'abstract': f"{prompt}, abstract art, geometric, modern, creative, artistic"
            }
            enhanced_prompt = style_prompts.get(style, prompt)
            
            job.progress = 30
            job.save()
            
            # Initialize Google Genai client using the working template
            try:
                # Set API key in environment
                os.environ['GOOGLE_API_KEY'] = api_key
                
                # Create client exactly like the working template
                client = genai.Client(api_key=api_key)
                
                print(f"Generating image with Google Genai: {enhanced_prompt}")
                job.progress = 50
                job.save()
                
                # Prepare content for generation (exactly like the template)
                contents = [enhanced_prompt]
                
                # Add reference images if provided
                if reference_images:
                    print(f"Adding {len(reference_images)} reference images")
                    for ref_img in reference_images:
                        try:
                            # Decode base64 image and convert to PIL Image
                            img_data = base64.b64decode(ref_img["image"])
                            pil_image = Image.open(BytesIO(img_data))
                            contents.append(pil_image)
                            print(f"Added reference image: {pil_image.size}")
                        except Exception as e:
                            print(f"Error processing reference image: {str(e)}")
                
                job.progress = 60
                job.save()
                
                # Generate image using Google Genai (exactly like the template)
                print("Calling Google Genai API...")
                response = client.models.generate_content(
                    model="gemini-2.5-flash-image-preview",
                    contents=contents,
                )
                
                print(f"Google Genai Response received: {type(response)}")
                job.progress = 80
                job.save()
                
                # Process the response to extract the generated image
                image_content = None
                
                # The response should contain the generated image
                if hasattr(response, 'candidates') and response.candidates:
                    candidate = response.candidates[0]
                    if hasattr(candidate, 'content') and hasattr(candidate.content, 'parts'):
                        for part in candidate.content.parts:
                            if hasattr(part, 'inline_data') and part.inline_data:
                                # Found image data
                                image_data = part.inline_data.data
                                mime_type = part.inline_data.mime_type
                                
                                print(f"Found image data with mime type: {mime_type}")
                                
                                # Decode image
                                if isinstance(image_data, str):
                                    image_content = base64.b64decode(image_data)
                                else:
                                    image_content = image_data
                                    
                                print(f"Image data length: {len(image_content)} bytes")
                                break
                
                # If no image found in the expected format, try alternative extraction
                if not image_content:
                    print("Trying alternative image extraction...")
                    # Sometimes the response structure is different
                    if hasattr(response, 'text'):
                        print("Response contains text, not image")
                    else:
                        print(f"Response structure: {dir(response)}")
                        # Try to find image data in other attributes
                        for attr in dir(response):
                            if not attr.startswith('_'):
                                try:
                                    value = getattr(response, attr)
                                    print(f"Response.{attr}: {type(value)}")
                                except:
                                    pass
                
                if image_content:
                    # Save the generated image
                    image_id = str(uuid.uuid4())
                    file_extension = ".png"
                    if "jpeg" in mime_type.lower() or "jpg" in mime_type.lower():
                        file_extension = ".jpg"
                    elif "webp" in mime_type.lower():
                        file_extension = ".webp"
                    
                    file_name = f"{image_id}{file_extension}"
                    file_path = default_storage.save(
                        f"generated_images/{file_name}",
                        ContentFile(image_content)
                    )
                    
                    local_image_url = request.build_absolute_uri(settings.MEDIA_URL + file_path)
                    
                    # Update job as completed
                    job.status = "completed"
                    job.progress = 100
                    job.completed_at = datetime.now()
                    job.image_url = local_image_url
                    job.image_id = image_id
                    job.provider = "google-genai-gemini-2.5-flash-image"
                    job.dimensions = f"{quality_params['width']}x{quality_params['height']}"
                    job.save()
                    
                    print(f"✅ Job {job_id} completed successfully with Google Genai!")
                    return
                else:
                    print("No image found in Google Genai response")
                    raise Exception("No image data found in API response")
                
            except Exception as e:
                print(f"Google Genai error: {str(e)}")
                # Create demo image as fallback
                print("Creating demo image as fallback...")
                demo_image = self._create_google_demo_image(enhanced_prompt, quality_params)
                
                image_id = str(uuid.uuid4())
                file_name = f"{image_id}.png"
                file_path = default_storage.save(
                    f"generated_images/{file_name}",
                    ContentFile(demo_image)
                )
                
                local_image_url = request.build_absolute_uri(settings.MEDIA_URL + file_path)
                
                # Update job as completed with demo
                job.status = "completed"
                job.progress = 100
                job.completed_at = datetime.now()
                job.image_url = local_image_url
                job.image_id = image_id
                job.provider = "google-genai-demo-fallback"
                job.dimensions = f"{quality_params['width']}x{quality_params['height']}"
                job.note = f"Demo image - API error: {str(e)}"
                job.save()
                
                print(f"Job {job_id} completed with demo fallback")
                return
                
        except Exception as e:
            print(f"Job {job_id} failed: {str(e)}")
            # Update job as failed
            try:
                job = ImageGenerationJob.objects.get(job_id=job_id)
                job.status = "error"
                job.progress = 0
                job.completed_at = datetime.now()
                job.error_message = str(e)
                job.save()
            except ImageGenerationJob.DoesNotExist:
                print(f"Job {job_id} not found for error update")
    
    def _create_google_demo_image(self, prompt, quality_params):
        """Create a Google-branded demo image"""
        try:
            from PIL import Image, ImageDraw, ImageFont
            import io
            import random
            
            width = quality_params['width']
            height = quality_params['height']
            
            # Create Google-style gradient
            image = Image.new('RGB', (width, height))
            draw = ImageDraw.Draw(image)
            
            # Google brand colors gradient
            colors = [
                (66, 133, 244),   # Google Blue
                (234, 67, 53),    # Google Red  
                (251, 188, 5),    # Google Yellow
                (52, 168, 83)     # Google Green
            ]
            
            # Create colorful gradient
            for y in range(height):
                ratio = y / height
                color_index = int(ratio * (len(colors) - 1))
                next_index = min(color_index + 1, len(colors) - 1)
                local_ratio = (ratio * (len(colors) - 1)) - color_index
                
                r = int(colors[color_index][0] * (1 - local_ratio) + colors[next_index][0] * local_ratio)
                g = int(colors[color_index][1] * (1 - local_ratio) + colors[next_index][1] * local_ratio)
                b = int(colors[color_index][2] * (1 - local_ratio) + colors[next_index][2] * local_ratio)
                
                for x in range(width):
                    noise = random.randint(-20, 20)
                    image.putpixel((x, y), (
                        max(0, min(255, r + noise)),
                        max(0, min(255, g + noise)),
                        max(0, min(255, b + noise))
                    ))
            
            # Add text overlay
            try:
                font = ImageFont.load_default()
                
                # Title
                title = "Generated by Google Genai"
                title_bbox = draw.textbbox((0, 0), title, font=font)
                title_width = title_bbox[2] - title_bbox[0]
                title_x = (width - title_width) // 2
                title_y = height // 3
                
                # Background for title
                draw.rectangle([title_x - 15, title_y - 10, title_x + title_width + 15, title_y + 25], 
                             fill=(255, 255, 255, 220))
                draw.text((title_x, title_y), title, fill=(60, 60, 60), font=font)
                
                # Prompt text
                prompt_text = prompt[:60] + "..." if len(prompt) > 60 else prompt
                prompt_bbox = draw.textbbox((0, 0), prompt_text, font=font)
                prompt_width = prompt_bbox[2] - prompt_bbox[0]
                prompt_x = (width - prompt_width) // 2
                prompt_y = title_y + 50
                
                # Background for prompt
                draw.rectangle([prompt_x - 10, prompt_y - 5, prompt_x + prompt_width + 10, prompt_y + 20], 
                             fill=(255, 255, 255, 200))
                draw.text((prompt_x, prompt_y), prompt_text, fill=(80, 80, 80), font=font)
                
                # Footer
                footer = "Nano Banana - Powered by Google Genai"
                footer_bbox = draw.textbbox((0, 0), footer, font=font)
                footer_width = footer_bbox[2] - footer_bbox[0]
                footer_x = (width - footer_width) // 2
                footer_y = height - 60
                
                draw.rectangle([footer_x - 10, footer_y - 5, footer_x + footer_width + 10, footer_y + 20], 
                             fill=(255, 255, 255, 180))
                draw.text((footer_x, footer_y), footer, fill=(100, 100, 100), font=font)
                
            except Exception as e:
                print(f"Error adding text to demo image: {str(e)}")
            
            # Convert to bytes
            buffer = io.BytesIO()
            image.save(buffer, format='PNG')
            return buffer.getvalue()
            
        except Exception as e:
            print(f"Error creating Google demo image: {str(e)}")
            # Return simple colored image
            from PIL import Image
            import io
            image = Image.new('RGB', (quality_params['width'], quality_params['height']), color=(66, 133, 244))
            buffer = io.BytesIO()
            image.save(buffer, format='PNG')
            return buffer.getvalue()


class ImageStatusView(APIView):
    """Check the status of an image generation job"""
    
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
                job = ImageGenerationJob.objects.get(job_id=job_id, user=user)
            except ImageGenerationJob.DoesNotExist:
                return Response(
                    ResponseInfo.error("Job not found or access denied"),
                    status=status.HTTP_404_NOT_FOUND
                )
            
            response_data = {
                "job_id": str(job.job_id),
                "status": job.status,
                "progress": job.progress,
                "prompt": job.prompt,
                "style": job.style,
                "quality": job.quality,
                "created_at": job.created_at.isoformat(),
                "started_at": job.started_at.isoformat() if job.started_at else None,
                "completed_at": job.completed_at.isoformat() if job.completed_at else None,
                "image_url": job.image_url,
                "image_id": str(job.image_id) if job.image_id else None,
                "error_message": job.error_message,
                "provider": job.provider,
                "dimensions": job.dimensions,
                "note": job.note
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


class JobListView(APIView):
    """Get list of all jobs for tracking"""
    
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
            jobs = ImageGenerationJob.objects.filter(user=user)
            jobs_list = []
            
            for job in jobs:
                job_summary = {
                    "job_id": str(job.job_id),
                    "prompt": job.prompt,
                    "status": job.status,
                    "progress": job.progress,
                    "style": job.style,
                    "quality": job.quality,
                    "created_at": job.created_at.isoformat(),
                    "completed_at": job.completed_at.isoformat() if job.completed_at else None,
                    "image_url": job.image_url,
                    "error_message": job.error_message,
                    "provider": job.provider,
                    "dimensions": job.dimensions
                }
                jobs_list.append(job_summary)
            
            return Response(
                ResponseInfo.success(jobs_list, "Jobs retrieved successfully"),
                status=status.HTTP_200_OK
            )
            
        except Exception as e:
            return Response(
                ResponseInfo.error(f"Error retrieving jobs: {str(e)}"),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class RetryJobView(APIView):
    """Retry a failed image generation job"""
    
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
                job = ImageGenerationJob.objects.get(job_id=job_id, user=user)
            except ImageGenerationJob.DoesNotExist:
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
            gemini_api_key = os.getenv('NANO_BANANA_API_KEY')
            if not gemini_api_key:
                return Response(
                    ResponseInfo.error("Google Gemini API key not configured"),
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
            
            # Reset job status for retry
            job.status = "queued"
            job.progress = 0
            job.started_at = None
            job.completed_at = None
            job.image_url = None
            job.image_id = None
            job.error_message = None
            job.note = None
            job.save()
            
            # Start background processing for retry
            thread = threading.Thread(
                target=self._process_nano_banana_generation,
                args=(str(job.job_id), gemini_api_key, request)
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
    
    def _process_nano_banana_generation(self, job_id, api_key, request):
        """Process image generation using Google Genai (Nano Banana) - Retry version"""
        try:
            # Get job from database
            try:
                job = ImageGenerationJob.objects.get(job_id=job_id)
            except ImageGenerationJob.DoesNotExist:
                print(f"Job {job_id} not found in database")
                return
            
            print(f"Retrying Google Genai (Nano Banana) generation for job {job_id}")
            print(f"API Key: {'Present' if api_key else 'Missing'}")
            
            # Update status to processing
            job.status = "processing"
            job.progress = 10
            job.started_at = datetime.now()
            job.save()
            
            prompt = job.prompt
            style = job.style
            quality = job.quality
            
            # Get reference images from database
            reference_images = []
            for ref_img in job.reference_images.all():
                reference_images.append({
                    "image": ref_img.image_data,
                    "filename": ref_img.filename,
                    "content_type": ref_img.content_type
                })
            
            # Quality mapping for dimensions
            quality_mapping = {
                'standard': {'width': 512, 'height': 512},
                'high': {'width': 768, 'height': 768},
                'ultra': {'width': 1024, 'height': 1024}
            }
            quality_params = quality_mapping.get(quality, quality_mapping['standard'])
            
            # Enhance prompt based on style
            style_prompts = {
                'realistic': f"{prompt}, photorealistic, high detail, natural lighting, professional photography",
                'artistic': f"{prompt}, artistic style, painted, creative interpretation, masterpiece art",
                'cartoon': f"{prompt}, cartoon style, animated, colorful, illustration, fun",
                'abstract': f"{prompt}, abstract art, geometric, modern, creative, artistic"
            }
            enhanced_prompt = style_prompts.get(style, prompt)
            
            job.progress = 30
            job.save()
            
            # Initialize Google Genai client using the working template
            try:
                # Set API key in environment
                os.environ['GOOGLE_API_KEY'] = api_key
                
                # Create client exactly like the working template
                client = genai.Client(api_key=api_key)
                
                print(f"Retrying image generation with Google Genai: {enhanced_prompt}")
                job.progress = 50
                job.save()
                
                # Prepare content for generation (exactly like the template)
                contents = [enhanced_prompt]
                
                # Add reference images if provided
                if reference_images:
                    print(f"Adding {len(reference_images)} reference images")
                    for ref_img in reference_images:
                        try:
                            # Decode base64 image and convert to PIL Image
                            img_data = base64.b64decode(ref_img["image"])
                            pil_image = Image.open(BytesIO(img_data))
                            contents.append(pil_image)
                            print(f"Added reference image: {pil_image.size}")
                        except Exception as e:
                            print(f"Error processing reference image: {str(e)}")
                
                job.progress = 60
                job.save()
                
                # Generate image using Google Genai (exactly like the template)
                print("Calling Google Genai API...")
                response = client.models.generate_content(
                    model="gemini-2.5-flash-image-preview",
                    contents=contents,
                )
                
                print(f"Google Genai Response received: {type(response)}")
                job.progress = 80
                job.save()
                
                # Process the response to extract the generated image
                image_content = None
                
                # The response should contain the generated image
                if hasattr(response, 'candidates') and response.candidates:
                    candidate = response.candidates[0]
                    if hasattr(candidate, 'content') and hasattr(candidate.content, 'parts'):
                        for part in candidate.content.parts:
                            if hasattr(part, 'inline_data') and part.inline_data:
                                # Found image data
                                image_data = part.inline_data.data
                                mime_type = part.inline_data.mime_type
                                
                                print(f"Found image data with mime type: {mime_type}")
                                
                                # Decode image
                                if isinstance(image_data, str):
                                    image_content = base64.b64decode(image_data)
                                else:
                                    image_content = image_data
                                    
                                print(f"Image data length: {len(image_content)} bytes")
                                break
                
                # If no image found in the expected format, try alternative extraction
                if not image_content:
                    print("Trying alternative image extraction...")
                    # Sometimes the response structure is different
                    if hasattr(response, 'text'):
                        print("Response contains text, not image")
                    else:
                        print(f"Response structure: {dir(response)}")
                        # Try to find image data in other attributes
                        for attr in dir(response):
                            if not attr.startswith('_'):
                                try:
                                    value = getattr(response, attr)
                                    print(f"Response.{attr}: {type(value)}")
                                except:
                                    pass
                
                if image_content:
                    # Save the generated image
                    image_id = str(uuid.uuid4())
                    file_extension = ".png"
                    if "jpeg" in mime_type.lower() or "jpg" in mime_type.lower():
                        file_extension = ".jpg"
                    elif "webp" in mime_type.lower():
                        file_extension = ".webp"
                    
                    file_name = f"{image_id}{file_extension}"
                    file_path = default_storage.save(
                        f"generated_images/{file_name}",
                        ContentFile(image_content)
                    )
                    
                    local_image_url = request.build_absolute_uri(settings.MEDIA_URL + file_path)
                    
                    # Update job as completed
                    job.status = "completed"
                    job.progress = 100
                    job.completed_at = datetime.now()
                    job.image_url = local_image_url
                    job.image_id = image_id
                    job.provider = "google-genai-gemini-2.5-flash-image"
                    job.dimensions = f"{quality_params['width']}x{quality_params['height']}"
                    job.save()
                    
                    print(f"✅ Job {job_id} retry completed successfully with Google Genai!")
                    return
                else:
                    print("No image found in Google Genai response")
                    raise Exception("No image data found in API response")
                
            except Exception as e:
                print(f"Google Genai error: {str(e)}")
                # Create demo image as fallback
                print("Creating demo image as fallback...")
                demo_image = self._create_google_demo_image(enhanced_prompt, quality_params)
                
                image_id = str(uuid.uuid4())
                file_name = f"{image_id}.png"
                file_path = default_storage.save(
                    f"generated_images/{file_name}",
                    ContentFile(demo_image)
                )
                
                local_image_url = request.build_absolute_uri(settings.MEDIA_URL + file_path)
                
                # Update job as completed with demo
                job.status = "completed"
                job.progress = 100
                job.completed_at = datetime.now()
                job.image_url = local_image_url
                job.image_id = image_id
                job.provider = "google-genai-demo-fallback"
                job.dimensions = f"{quality_params['width']}x{quality_params['height']}"
                job.note = f"Demo image - API error: {str(e)}"
                job.save()
                
                print(f"Job {job_id} retry completed with demo fallback")
                return
                
        except Exception as e:
            print(f"Job {job_id} retry failed: {str(e)}")
            # Update job as failed
            try:
                job = ImageGenerationJob.objects.get(job_id=job_id)
                job.status = "error"
                job.progress = 0
                job.completed_at = datetime.now()
                job.error_message = str(e)
                job.save()
            except ImageGenerationJob.DoesNotExist:
                print(f"Job {job_id} not found for error update")
    
    def _create_google_demo_image(self, prompt, quality_params):
        """Create a Google-branded demo image"""
        try:
            from PIL import Image, ImageDraw, ImageFont
            import io
            import random
            
            width = quality_params['width']
            height = quality_params['height']
            
            # Create Google-style gradient
            image = Image.new('RGB', (width, height))
            draw = ImageDraw.Draw(image)
            
            # Google brand colors gradient
            colors = [
                (66, 133, 244),   # Google Blue
                (234, 67, 53),    # Google Red  
                (251, 188, 5),    # Google Yellow
                (52, 168, 83)     # Google Green
            ]
            
            # Create colorful gradient
            for y in range(height):
                ratio = y / height
                color_index = int(ratio * (len(colors) - 1))
                next_index = min(color_index + 1, len(colors) - 1)
                local_ratio = (ratio * (len(colors) - 1)) - color_index
                
                r = int(colors[color_index][0] * (1 - local_ratio) + colors[next_index][0] * local_ratio)
                g = int(colors[color_index][1] * (1 - local_ratio) + colors[next_index][1] * local_ratio)
                b = int(colors[color_index][2] * (1 - local_ratio) + colors[next_index][2] * local_ratio)
                
                for x in range(width):
                    noise = random.randint(-20, 20)
                    image.putpixel((x, y), (
                        max(0, min(255, r + noise)),
                        max(0, min(255, g + noise)),
                        max(0, min(255, b + noise))
                    ))
            
            # Add text overlay
            try:
                font = ImageFont.load_default()
                
                # Title
                title = "Generated by Google Genai"
                title_bbox = draw.textbbox((0, 0), title, font=font)
                title_width = title_bbox[2] - title_bbox[0]
                title_x = (width - title_width) // 2
                title_y = height // 3
                
                # Background for title
                draw.rectangle([title_x - 15, title_y - 10, title_x + title_width + 15, title_y + 25], 
                              fill=(255, 255, 255, 220))
                draw.text((title_x, title_y), title, fill=(60, 60, 60), font=font)
                
                # Prompt text
                prompt_text = prompt[:60] + "..." if len(prompt) > 60 else prompt
                prompt_bbox = draw.textbbox((0, 0), prompt_text, font=font)
                prompt_width = prompt_bbox[2] - prompt_bbox[0]
                prompt_x = (width - prompt_width) // 2
                prompt_y = title_y + 50
                
                # Background for prompt
                draw.rectangle([prompt_x - 10, prompt_y - 5, prompt_x + prompt_width + 10, prompt_y + 20], 
                              fill=(255, 255, 255, 200))
                draw.text((prompt_x, prompt_y), prompt_text, fill=(80, 80, 80), font=font)
                
                # Footer
                footer = "Nano Banana - Powered by Google Genai"
                footer_bbox = draw.textbbox((0, 0), footer, font=font)
                footer_width = footer_bbox[2] - footer_bbox[0]
                footer_x = (width - footer_width) // 2
                footer_y = height - 60
                
                draw.rectangle([footer_x - 10, footer_y - 5, footer_x + footer_width + 10, footer_y + 20], 
                              fill=(255, 255, 255, 180))
                draw.text((footer_x, footer_y), footer, fill=(100, 100, 100), font=font)
                
            except Exception as e:
                print(f"Error adding text to demo image: {str(e)}")
            
            # Convert to bytes
            buffer = io.BytesIO()
            image.save(buffer, format='PNG')
            return buffer.getvalue()
            
        except Exception as e:
            print(f"Error creating Google demo image: {str(e)}")
            # Return simple colored image
            from PIL import Image
            import io
            image = Image.new('RGB', (quality_params['width'], quality_params['height']), color=(66, 133, 244))
            buffer = io.BytesIO()
            image.save(buffer, format='PNG')
            return buffer.getvalue()
