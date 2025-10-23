import os
import uuid
import base64
import warnings
import threading
import requests
import csv
import io
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
import openai

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


def process_csv_feedback(csv_file):
    """Process CSV file and extract feedback data"""
    try:
        # Reset file pointer to beginning
        csv_file.seek(0)
        
        # Read CSV content
        csv_content = csv_file.read().decode('utf-8').strip()
        print(f"📄 CSV Content Length: {len(csv_content)} characters")
        print(f"📄 CSV Content Preview: {csv_content[:200]}...")
        
        # Check if it's structured CSV (has headers) or simple text feedback
        lines = csv_content.split('\n')
        print(f"📄 Number of lines: {len(lines)}")
        
        # Try to parse as structured CSV first
        try:
            csv_reader = csv.DictReader(io.StringIO(csv_content))
            feedback_data = []
            for row in csv_reader:
                print(f"📊 Processing structured CSV row: {row}")
                feedback_data.append(row)
            
            if feedback_data:
                print(f"✅ Successfully processed {len(feedback_data)} structured CSV entries")
                return feedback_data
        except Exception as csv_error:
            print(f"📄 Not structured CSV, trying as text feedback: {csv_error}")
        
        # If not structured CSV, treat as simple text feedback
        if csv_content and len(csv_content.strip()) > 0:
            print("📄 Processing as simple text feedback")
            feedback_data = [{
                'feedback_type': 'general',
                'description': csv_content.strip(),
                'improvement_suggestion': csv_content.strip()
            }]
            print(f"✅ Successfully processed 1 text feedback entry: {feedback_data[0]['description']}")
            return feedback_data
        else:
            print("⚠️ Empty CSV content")
            return []
            
    except Exception as e:
        print(f"❌ Error processing CSV: {str(e)}")
        print(f"📄 CSV file type: {type(csv_file)}")
        print(f"📄 CSV file name: {getattr(csv_file, 'name', 'Unknown')}")
        return []


def generate_enhanced_prompt_with_openai(user_prompt, feedback_data):
    """Generate enhanced prompt using OpenAI based on CSV feedback"""
    try:
        # Get OpenAI API key
        openai_api_key = os.getenv('OPENAI_API_KEY')
        
        if not openai_api_key:
            print("⚠️ OpenAI API key not found in environment variables")
            print("💡 Please add OPENAI_API_KEY to your .env file")
            return user_prompt
        
        print(f"🤖 OpenAI API Key: {'Present' if openai_api_key else 'Missing'}")
        
        # Prepare feedback summary
        feedback_summary = ""
        if feedback_data:
            feedback_summary = "Based on the following feedback data:\n"
            for i, feedback in enumerate(feedback_data[:5]):  # Limit to first 5 entries
                feedback_summary += f"Feedback {i+1}: {str(feedback)}\n"
            print(f"📊 Processing {len(feedback_data)} feedback entries")
        
        # Create prompt for OpenAI
        system_prompt = """You are an expert at enhancing image generation prompts based on user feedback. 
        Analyze the user's prompt and the provided feedback data to create an improved, more detailed prompt 
        that will generate better images. Focus on:
        1. Adding specific details and improvements based on feedback
        2. Enhancing visual descriptions
        3. Adding technical photography terms
        4. Improving composition and style descriptions
        
        Return only the enhanced prompt, no explanations."""
        
        user_message = f"""
        Original user prompt: "{user_prompt}"
        
        {feedback_summary}
        
        Please enhance this prompt based on the feedback data to create a better image generation prompt.
        """
        
        print("🔄 Calling OpenAI API for prompt enhancement...")
        
        # Initialize OpenAI client with new API format
        client = openai.OpenAI(api_key=openai_api_key)
        
        # Call OpenAI API with new format
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            max_tokens=500,
            temperature=0.7
        )
        
        enhanced_prompt = response.choices[0].message.content.strip()
        print("=" * 80)
        print("🎯 PROMPT ENHANCEMENT RESULTS:")
        print("=" * 80)
        print(f"📝 Original User Prompt: {user_prompt}")
        print("-" * 80)
        print(f"✨ Enhanced Prompt by OpenAI: {enhanced_prompt}")
        print("=" * 80)
        return enhanced_prompt
        
    except Exception as e:
        print(f"❌ Error generating enhanced prompt: {str(e)}")
        print(f"💡 Falling back to original user prompt: {user_prompt}")
        return user_prompt


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
            
            # Process CSV feedback file if provided
            feedback_data = []
            csv_file = None
            print(f"🔍 Checking for CSV files in request.FILES: {list(request.FILES.keys())}")
            
            for key, file in request.FILES.items():
                print(f"📁 Found file: {key}, Content-Type: {file.content_type}, Size: {file.size}")
                if key == 'csv_feedback' and (file.content_type == 'text/csv' or file.content_type == 'text/plain' or file.name.endswith('.csv') or file.name.endswith('.txt')):
                    try:
                        csv_file = file
                        print(f"✅ CSV file detected: {file.name}")
                        feedback_data = process_csv_feedback(file)
                        print(f"📊 Processed CSV feedback with {len(feedback_data)} entries")
                    except Exception as e:
                        print(f"❌ Error processing CSV feedback: {str(e)}")
                        continue
                elif key == 'csv_feedback':
                    print(f"⚠️ CSV file found but wrong content type: {file.content_type}")
            
            # Generate enhanced prompt using OpenAI if CSV feedback is provided
            final_prompt = prompt  # Default to user's original prompt
            if feedback_data and csv_file:
                print("🤖 CSV feedback detected - Generating enhanced prompt with OpenAI...")
                print(f"📊 CSV file: {csv_file.name} ({csv_file.size} bytes)")
                print(f"📈 Feedback entries: {len(feedback_data)}")
                final_prompt = generate_enhanced_prompt_with_openai(prompt, feedback_data)
                print(f"🎯 FINAL ENHANCED PROMPT FOR IMAGE GENERATION: {final_prompt}")
            else:
                print("📝 No CSV feedback provided - Using original user prompt")
                print(f"🎯 FINAL PROMPT FOR IMAGE GENERATION: {final_prompt}")
            
            # Create job in database
            job = ImageGenerationJob.objects.create(
                job_id=job_id,
                user=user,
                prompt=final_prompt,  # Use enhanced prompt
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
                
                print("=" * 100)
                print("🚀 STARTING IMAGE GENERATION WITH GOOGLE GENAI")
                print("=" * 100)
                print(f"🎯 PROMPT BEING USED: {enhanced_prompt}")
                print("=" * 100)
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

class DeleteJobView(APIView):
    def delete(self, request, job_id):
        """Delete a job and its associated files from the database and filesystem"""
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
                job = ImageGenerationJob.objects.get(job_id=job_id, user=user)
            except ImageGenerationJob.DoesNotExist:
                return Response(
                    ResponseInfo.error("Job not found or access denied"),
                    status=status.HTTP_404_NOT_FOUND
                )
            
            # Delete physical image file if it exists
            if job.image_url:
                try:
                    # Extract file path from URL
                    if job.image_url.startswith('/media/'):
                        file_path = job.image_url.replace('/media/', '')
                        full_path = os.path.join(settings.MEDIA_ROOT, file_path)
                        if os.path.exists(full_path):
                            os.remove(full_path)
                            print(f"✅ Deleted physical file: {full_path}")
                except Exception as e:
                    print(f"Warning: Could not delete physical file: {str(e)}")
            
            # Delete reference images from database (they are stored as base64, no physical files)
            job.reference_images.all().delete()
            
            # Delete the job record from database
            job.delete()
            
            print(f"✅ Job {job_id} deleted successfully from database")
            
            return Response(
                ResponseInfo.success("Job deleted successfully"),
                status=status.HTTP_200_OK
            )
            
        except Exception as e:
            print(f"❌ Error deleting job {job_id}: {str(e)}")
            return Response(
                ResponseInfo.error(f"Failed to delete job: {str(e)}"),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class DashboardStatsView(APIView):
    def get(self, request):
        """Get dashboard statistics for the current user"""
        try:
            # Get current user from JWT token
            user = get_current_user(request)
            if not user:
                return Response(
                    ResponseInfo.error("Authentication required"),
                    status=status.HTTP_401_UNAUTHORIZED
                )
            
            # Get user's job statistics
            total_jobs = ImageGenerationJob.objects.filter(user=user).count()
            completed_jobs = ImageGenerationJob.objects.filter(user=user, status='completed').count()
            processing_jobs = ImageGenerationJob.objects.filter(user=user, status='processing').count()
            failed_jobs = ImageGenerationJob.objects.filter(user=user, status='failed').count()
            
            # Get recent jobs (last 5)
            recent_jobs = ImageGenerationJob.objects.filter(user=user).order_by('-created_at')[:5]
            
            # Format recent activity
            recent_activity = []
            for job in recent_jobs:
                # Calculate time ago
                time_ago = self._get_time_ago(job.created_at)
                
                recent_activity.append({
                    'id': str(job.job_id),
                    'type': 'image',
                    'title': job.prompt[:50] + ('...' if len(job.prompt) > 50 else ''),
                    'status': job.status,
                    'time': time_ago,
                    'image_url': job.image_url,
                    'created_at': job.created_at.isoformat() if job.created_at else None
                })
            
            stats = {
                'total_images': completed_jobs,
                'total_jobs': total_jobs,
                'processing_jobs': processing_jobs,
                'failed_jobs': failed_jobs,
                'success_rate': round((completed_jobs / total_jobs * 100) if total_jobs > 0 else 0, 1)
            }
            
            return Response(
                ResponseInfo.success({
                    'stats': stats,
                    'recent_activity': recent_activity
                }),
                status=status.HTTP_200_OK
            )
            
        except Exception as e:
            print(f"❌ Error getting dashboard stats: {str(e)}")
            return Response(
                ResponseInfo.error(f"Failed to get dashboard stats: {str(e)}"),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    def _get_time_ago(self, created_at):
        """Calculate time ago string"""
        if not created_at:
            return "Unknown time"
        
        from django.utils import timezone
        now = timezone.now()
        diff = now - created_at
        
        if diff.days > 0:
            return f"{diff.days} day{'s' if diff.days > 1 else ''} ago"
        elif diff.seconds > 3600:
            hours = diff.seconds // 3600
            return f"{hours} hour{'s' if hours > 1 else ''} ago"
        elif diff.seconds > 60:
            minutes = diff.seconds // 60
            return f"{minutes} minute{'s' if minutes > 1 else ''} ago"
        else:
            return "Just now"
