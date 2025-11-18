import os
import uuid
import time
import threading
import warnings
import base64
import openai
import csv
import json
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


def load_job_metadata(job):
    """Parse job.note JSON safely."""
    if not job.note:
        return {}
    try:
        data = json.loads(job.note)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def save_job_metadata(job, metadata):
    """Store metadata dict in job.note."""
    try:
        job.note = json.dumps(metadata)
    except Exception:
        # Fallback to plain string representation
        job.note = str(metadata)


def get_veo_file_metadata(job):
    """Return dict containing stored Veo file references."""
    metadata = load_job_metadata(job)
    veo_meta = metadata.get('veo_metadata')
    if isinstance(veo_meta, dict):
        return veo_meta
    # Backward compatibility: metadata might have been stored at top-level
    legacy_keys = ('veo_file_uri', 'veo_file_name', 'veo_mime_type')
    if all(key in metadata for key in legacy_keys):
        return {key: metadata.get(key) for key in legacy_keys}
    return {}


def process_csv_feedback(csv_file):
    """Process CSV file and extract feedback data
    
    Supports two CSV formats:
    1. New format: Review ID, Product ASIN, Product Name, Reviewer Name, Reviewer ID, 
                   Review Title, Review Text, Rating, Verified Purchase, Review Date, 
                   Helpful Votes, Total Votes, Country
    2. Old/Simple format: Basic feedback text or any key-value CSV
    """
    try:
        # Reset file pointer to beginning
        csv_file.seek(0)
        
        # Read CSV content
        csv_content = csv_file.read().decode('utf-8')
        print(f"📄 CSV Content Preview: {csv_content[:200]}...")
        
        # Check if it's structured CSV (has headers) or simple text feedback
        lines = csv_content.split('\n')
        print(f"📄 Number of lines: {len(lines)}")
        
        # Try to parse as structured CSV first
        try:
            csv_reader = csv.DictReader(csv_content.splitlines())
            feedback_data = list(csv_reader)
            
            if feedback_data:
                print(f"✅ Successfully processed {len(feedback_data)} structured CSV entries")
                print(f"📋 CSV Headers detected: {list(feedback_data[0].keys())}")
                return feedback_data
        except Exception as csv_error:
            print(f"📄 Not structured CSV, trying as text feedback: {csv_error}")
        
        # If not structured CSV, treat as simple text feedback
        if csv_content and len(csv_content.strip()) > 0:
            print("📄 Processing as simple text feedback")
            feedback_data = [{
                'feedback_type': 'general',
                'description': csv_content.strip(),
                'rating': 'N/A',
                'source': 'text_file'
            }]
            print(f"✅ Successfully processed 1 text feedback entry")
            return feedback_data
        
        print("⚠️ No valid feedback data found in CSV")
        return []
        
    except Exception as e:
        print(f"❌ Error processing CSV feedback: {str(e)}")
        print(f"📄 CSV file type: {type(csv_file)}")
        print(f"📄 CSV file name: {getattr(csv_file, 'name', 'Unknown')}")
        return []


def extract_review_text_from_csv(feedback_data):
    """Extract and combine review text from CSV feedback data"""
    review_texts = []
    
    for feedback in feedback_data:
        # Try different possible field names for review text
        review_text = feedback.get('Review Text', '') or feedback.get('review_text', '') or feedback.get('Review', '') or feedback.get('review', '')
        
        if review_text and len(review_text.strip()) > 0:
            review_texts.append(review_text.strip())
    
    return ' '.join(review_texts)


def generate_enhanced_prompt_with_openai(user_prompt, feedback_data):
    """Generate enhanced prompt using OpenAI based on CSV feedback and Review Text
    
    Uses the new CSV structure to extract Review Text and ratings for better prompt generation
    """
    try:
        # Get OpenAI API key
        openai_api_key = os.getenv('OPENAI_API_KEY')
        
        if not openai_api_key:
            print("⚠️ OpenAI API key not found in environment variables")
            print("💡 Please add OPENAI_API_KEY to your .env file")
            return user_prompt
        
        print(f"🤖 OpenAI API Key: {'Present' if openai_api_key else 'Missing'}")
        
        # Extract review text from CSV using new structure
        review_text = extract_review_text_from_csv(feedback_data)
        
        # Prepare feedback summary with additional metadata from new CSV structure
        feedback_summary = ""
        if feedback_data:
            feedback_summary = "Based on the following product review feedback:\n"
            
            for i, feedback in enumerate(feedback_data[:5]):  # Limit to first 5 entries
                # New CSV format fields
                product_name = feedback.get('Product Name', 'Unknown Product')
                rating = feedback.get('Rating', 'N/A')
                review_text_field = feedback.get('Review Text', '')
                review_title = feedback.get('Review Title', '')
                verified = feedback.get('Verified Purchase', 'No')
                helpful_votes = feedback.get('Helpful Votes', 0)
                
                feedback_summary += f"\n📦 Review {i+1}:\n"
                feedback_summary += f"  • Product: {product_name}\n"
                feedback_summary += f"  • Rating: {rating}/5 stars\n"
                if review_title:
                    feedback_summary += f"  • Title: {review_title}\n"
                if review_text_field:
                    feedback_summary += f"  • Review: {review_text_field[:200]}...\n"
                feedback_summary += f"  • Verified Purchase: {verified}\n"
                feedback_summary += f"  • Helpful Votes: {helpful_votes}\n"
            
            print(f"📊 Processing {len(feedback_data)} review feedback entries")
        
        # Create prompt for OpenAI
        system_prompt = """You are an expert at enhancing video generation prompts based on product review feedback. 
        Analyze the user's prompt and the provided customer reviews to create an improved, more detailed prompt 
        that will generate better videos. Focus on:
        1. Understanding customer preferences from review ratings and text
        2. Incorporating specific product features mentioned in reviews
        3. Adding visual elements that customers appreciate
        4. Enhancing composition based on verified purchase feedback
        5. Including style and quality elements that align with high-rated reviews
        6. Adding technical video production terms (camera movements, lighting, cinematography)
        7. Improving visual descriptions based on customer sentiment
        8. Creating dynamic video scenarios that showcase product benefits
        
        Return only the enhanced prompt, no explanations."""
        
        user_message = f"""
        Original user prompt: "{user_prompt}"
        
        {feedback_summary}
        
        Please enhance this prompt based on the product review feedback to create a better video generation prompt. 
        Consider the ratings, customer feedback, and product details from the reviews to make the video more aligned with what customers appreciate.
        """
        
        print("🔄 Calling OpenAI API for prompt enhancement with review feedback...")
        
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
        print("🎯 PROMPT ENHANCEMENT RESULTS FROM REVIEW FEEDBACK:")
        print("=" * 80)
        print(f"📝 Original User Prompt: {user_prompt}")
        print("-" * 80)
        print(f"📦 Review Data Summary:")
        print(f"   • Total Reviews: {len(feedback_data)}")
        if feedback_data:
            ratings = [f.get('Rating', 0) for f in feedback_data if f.get('Rating')]
            if ratings:
                avg_rating = sum(float(r) for r in ratings if r) / len([r for r in ratings if r])
                print(f"   • Average Rating: {avg_rating:.1f}/5 stars")
        print("-" * 80)
        print(f"✨ Enhanced Prompt by OpenAI: {enhanced_prompt}")
        print("=" * 80)
        return enhanced_prompt
        
    except Exception as e:
        print(f"❌ Error generating enhanced prompt: {str(e)}")
        print(f"💡 Falling back to original user prompt: {user_prompt}")
        return user_prompt


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


def generate_three_video_prompts_with_openai(user_prompt):
    """Generate three different video prompt variations using OpenAI based on user input
    
    Args:
        user_prompt (str): User's original video prompt
        
    Returns:
        list: Three different detailed video prompt variations
    """
    try:
        # Get OpenAI API key
        openai_api_key = os.getenv('OPENAI_API_KEY')
        
        if not openai_api_key:
            print("⚠️ OpenAI API key not found in environment variables")
            print("💡 Please add OPENAI_API_KEY to your .env file")
            # Return three variations of the original prompt (250-300 words each)
            return [
                f"A cinematic video opening with a smooth aerial drone shot that gracefully descends from 50 feet toward {user_prompt}, featuring professional camera work with steady movements, natural lighting creating depth and dimension, capturing the scene with a shallow depth of field that keeps the subject in sharp focus while the background gently blurs, creating an intimate and engaging visual narrative with warm color grading and a professional, polished aesthetic. The camera executes a slow 360-degree circular orbit around the subject at eye level, capturing dynamic movements and interactions with smooth gimbal-stabilized motion throughout. The video maintains a steady pace with gentle transitions between wide establishing shots and medium close-ups, showcasing the subject's actions and environmental details with rich, vibrant colors and natural lighting that enhances the overall mood and atmosphere. The cinematography emphasizes fluid camera movements, including slow dolly shots, gentle pans, and subtle zoom effects that draw the viewer's attention to key elements while maintaining visual continuity and narrative flow throughout the entire video sequence.",
                
                f"Create a dynamic handheld video showing {user_prompt} with energetic camera movements that follow the action closely, featuring quick transitions between different angles and perspectives, vibrant natural lighting with high contrast and rich colors, the camera capturing spontaneous moments and authentic interactions, creating a lively, documentary-style feel with contemporary visual language and an engaging, relatable mood. The video begins with a wide establishing shot that quickly cuts to medium shots and close-ups, using fast-paced editing and dynamic camera work including tracking shots, whip pans, and handheld movements that create a sense of immediacy and energy. The lighting varies from natural daylight to golden hour warmth, with dramatic shadows and highlights that add visual interest and depth to each frame. The camera work emphasizes movement and action, following subjects as they interact with their environment, using techniques like rack focus, shallow depth of field, and varied shot sizes to create a compelling visual narrative that keeps viewers engaged throughout the entire video experience.",
                
                f"An artistic slow-motion video capturing {user_prompt} through a series of carefully composed shots that emphasize beauty and emotion, beginning with a wide establishing shot that slowly zooms in, featuring dramatic lighting with strong shadows and highlights, the camera executing graceful movements like slow pans and gentle tilts, creating a dreamlike, atmospheric visual poem with moody color grading and an introspective, contemplative tone. The video unfolds with deliberate pacing, using techniques like time-lapse effects, slow-motion sequences, and smooth camera transitions to create a meditative viewing experience. The cinematography emphasizes visual storytelling through careful composition, with each shot carefully framed to highlight the subject's emotional journey and environmental context. The lighting design creates a rich, layered visual experience with warm and cool tones that shift throughout the video, while the camera movements remain fluid and purposeful, guiding the viewer's attention through the narrative with elegant precision and artistic flair that transforms a simple concept into a compelling visual story."
            ]
        
        print(f"🤖 OpenAI API Key: {'Present' if openai_api_key else 'Missing'}")
        
        # Create system prompt for generating three video-specific variations
        system_prompt = """You are an expert at creating high-quality, detailed video generation prompts for AI video generation tool - Google Veo 3.1. 
Based on the user's input, create THREE different comprehensive prompt variations that are specifically designed for video generation.

CRITICAL REQUIREMENT: Each prompt MUST be exactly 250-300 words long with rich, specific details.

IMPORTANT: Focus on VIDEO-SPECIFIC elements. These prompts should describe what happens in the video, how things move, and the overall cinematic feel.

Key Elements to include for VIDEO prompts:

1. CAMERA MOVEMENTS & CINEMATOGRAPHY:
- Static shot, Slow pan, Zoom in/out, Dolly shot, Tracking shot, Crane shot, Handheld
- Smooth gimbal movement, Steady cam, Aerial drone shot, First-person POV
- Camera pull-back, Push-in, Orbit around subject, Whip pan, Tilt, Dutch angle
- Shot types: Wide shot, Medium shot, Close-up, Extreme close-up, Establishing shot, Over-the-shoulder
- Depth of field: Shallow focus, Deep focus, Rack focus (focus shift between subjects)
- Frame rate feel: Slow motion, Real-time, Time-lapse effect, Smooth/Cinematic

2. SCENE DYNAMICS & MOTION:
- Subject movement and actions (walking, running, dancing, gesturing, interacting, working, playing)
- Environmental changes (wind blowing, water flowing, lights changing, clouds moving, shadows shifting)
- Interaction between elements (people talking, objects moving, animals playing, vehicles in motion)
- Temporal progression (sunrise to day, seasons changing, growth/transformation, day to night)
- Speed of movement (fast-paced, leisurely, graceful, energetic, sudden, gradual)
- Direction of movement (toward camera, away, left to right, circular, diagonal, vertical)

3. LIGHTING & VISUAL STYLE:
- Lighting: Natural daylight, Golden hour, Blue hour, Studio lighting, Neon lights, Dramatic shadows, Backlit, Rim lighting, Practical lighting
- Visual styles: Cinematic (film-like, professional movie quality, epic), Documentary (realistic, observational, authentic), Commercial (polished, advertising style, high production value), Artistic (creative, experimental, unique perspective), Vintage (retro film look, grainy, nostalgic), Modern (clean, contemporary, sleek), Music Video (stylized, rhythmic cuts, artistic flair)

4. MOOD & ATMOSPHERE:
- Emotional tone: Joyful, Melancholic, Suspenseful, Peaceful, Energetic, Mysterious, Romantic, Dramatic, Intense, Serene
- Setting atmosphere: Cozy, Epic, Intimate, Grand, Moody, Bright, Dreamy, Ethereal, Gritty, Elegant
- Weather/Environment: Sunny, Rainy, Foggy, Snowy, Windy, Clear, Stormy, Misty, Overcast, Partly cloudy

5. DETAILED SCENE CONSTRUCTION:
- Specific locations and settings (indoor/outdoor, urban/rural, natural/artificial environments)
- Character descriptions and their actions throughout the video
- Object interactions and movements
- Environmental details that enhance the visual narrative
- Color palettes and visual themes
- Sound design elements (though focus on visual aspects)

6. TEMPORAL FLOW & NARRATIVE:
- Beginning: How the video opens and establishes the scene
- Middle: The main action, movement, and development
- End: How the video concludes or transitions
- Pacing: Fast, slow, varied, building to climax, steady rhythm
- Story arc: Even for simple concepts, create a visual narrative flow

WORD COUNT REQUIREMENTS:
- Each prompt MUST be exactly 250-300 words long
- Use rich, descriptive language with specific details
- Include multiple camera movements, lighting descriptions, and scene dynamics
- Describe actions, movements, and interactions in detail
- Paint a complete visual picture that tells a story through motion

CRITICAL FORMAT REQUIREMENTS:
- Return EXACTLY 3 prompts
- Separate each prompt with "|||" (three pipe characters)
- Each prompt must be on a single line
- No explanations, no numbering, no additional text
- No line breaks within prompts
- Just the three comprehensive video prompts separated by "|||"

Remember: These are VIDEO prompts - focus on movement, action, camera work, temporal flow, and what HAPPENS in the video! Each prompt should be a complete, detailed description of a video that could be generated."""

        user_message = f"""User's original prompt: "{user_prompt}"

Based on this prompt, create THREE highly detailed video generation prompts that:
1. Are EXACTLY 250-300 words long with SPECIFIC details about camera movement, subject actions, and scene dynamics
2. Each take a DIFFERENT creative approach (different camera styles, moods, cinematography, pacing)
3. Focus on MOVEMENT and what HAPPENS in the video over time (not just static descriptions)
4. Include specific camera movements (dolly, pan, zoom, tracking, crane, handheld, etc.)
5. Describe subject actions, movements, and interactions in detail throughout the video
6. Specify lighting, atmosphere, and mood appropriate for video with rich descriptions
7. Create different visual styles and emotional tones across the three variations
8. Describe the temporal flow - how the video unfolds from beginning to end
9. Include environmental details, character descriptions, and object interactions
10. Use vivid, descriptive language that paints a complete moving picture

WORD COUNT REQUIREMENT: Each prompt must be 250-300 words. Count your words carefully!

Return ONLY the three detailed video prompts separated by "|||" (three pipe characters).
No explanations, no labels, just the three comprehensive video prompts.

CRITICAL: Each prompt MUST focus on VIDEO elements - what moves, how the camera moves, what actions happen, how the scene unfolds over time! Make each prompt a complete, detailed description of a video that could be generated.

NOW CREATE THE THREE VIDEO PROMPTS:"""
        
        # Merge system prompt and user message
        merged_prompt = f"{system_prompt}\n\n{user_message}"
        
        print("🔄 Calling OpenAI API for three video prompt variations...")
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
                    max_tokens=2500,  # Increased to accommodate 250-300 word prompts
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
        
        print(f"✅ Parsed {len(prompts)} video prompts")
        
        # Ensure we have exactly 3 prompts
        if len(prompts) != 3:
            print(f"⚠️ Expected 3 prompts, got {len(prompts)}. Creating fallback prompts.")
            prompts = [
                f"A cinematic video opening with a smooth dolly shot moving toward {user_prompt}, featuring professional camera work with steady movements, natural lighting creating depth and dimension, capturing the scene with a shallow depth of field that keeps the subject in sharp focus while the background gently blurs, creating an intimate and engaging visual narrative with warm color grading and a professional, polished aesthetic. The camera executes a slow 360-degree circular orbit around the subject at eye level, capturing dynamic movements and interactions with smooth gimbal-stabilized motion throughout. The video maintains a steady pace with gentle transitions between wide establishing shots and medium close-ups, showcasing the subject's actions and environmental details with rich, vibrant colors and natural lighting that enhances the overall mood and atmosphere. The cinematography emphasizes fluid camera movements, including slow dolly shots, gentle pans, and subtle zoom effects that draw the viewer's attention to key elements while maintaining visual continuity and narrative flow throughout the entire video sequence.",
                
                f"Create a dynamic handheld video showing {user_prompt} with energetic camera movements that follow the action closely, featuring quick transitions between different angles and perspectives, vibrant natural lighting with high contrast and rich colors, the camera capturing spontaneous moments and authentic interactions, creating a lively, documentary-style feel with contemporary visual language and an engaging, relatable mood. The video begins with a wide establishing shot that quickly cuts to medium shots and close-ups, using fast-paced editing and dynamic camera work including tracking shots, whip pans, and handheld movements that create a sense of immediacy and energy. The lighting varies from natural daylight to golden hour warmth, with dramatic shadows and highlights that add visual interest and depth to each frame. The camera work emphasizes movement and action, following subjects as they interact with their environment, using techniques like rack focus, shallow depth of field, and varied shot sizes to create a compelling visual narrative that keeps viewers engaged throughout the entire video experience.",
                
                f"An artistic slow-motion video capturing {user_prompt} through a series of carefully composed shots that emphasize beauty and emotion, beginning with a wide establishing shot that slowly zooms in, featuring dramatic lighting with strong shadows and highlights, the camera executing graceful movements like slow pans and gentle tilts, creating a dreamlike, atmospheric visual poem with moody color grading and an introspective, contemplative tone. The video unfolds with deliberate pacing, using techniques like time-lapse effects, slow-motion sequences, and smooth camera transitions to create a meditative viewing experience. The cinematography emphasizes visual storytelling through careful composition, with each shot carefully framed to highlight the subject's emotional journey and environmental context. The lighting design creates a rich, layered visual experience with warm and cool tones that shift throughout the video, while the camera movements remain fluid and purposeful, guiding the viewer's attention through the narrative with elegant precision and artistic flair that transforms a simple concept into a compelling visual story."
            ]
        
        print("=" * 80)
        print("🎯 THREE VIDEO PROMPT VARIATIONS GENERATED:")
        print("=" * 80)
        for i, prompt in enumerate(prompts, 1):
            print(f"📹 Video Prompt {i}: {prompt[:100]}...")
        print("=" * 80)
        
        return prompts
        
    except Exception as e:
        print(f"❌ Error generating three video prompts: {str(e)}")
        print(f"💡 Falling back to default video prompt variations")
        return [
            f"A cinematic video opening with a smooth aerial drone shot that gracefully descends from 50 feet toward {user_prompt}, featuring professional camera work with steady movements, natural lighting creating depth and dimension, capturing the scene with a shallow depth of field that keeps the subject in sharp focus while the background gently blurs, creating an intimate and engaging visual narrative with warm color grading and a professional, polished aesthetic. The camera executes a slow 360-degree circular orbit around the subject at eye level, capturing dynamic movements and interactions with smooth gimbal-stabilized motion throughout. The video maintains a steady pace with gentle transitions between wide establishing shots and medium close-ups, showcasing the subject's actions and environmental details with rich, vibrant colors and natural lighting that enhances the overall mood and atmosphere. The cinematography emphasizes fluid camera movements, including slow dolly shots, gentle pans, and subtle zoom effects that draw the viewer's attention to key elements while maintaining visual continuity and narrative flow throughout the entire video sequence.",
            
            f"Create a dynamic handheld video showing {user_prompt} with energetic camera movements that follow the action closely, featuring quick transitions between different angles and perspectives, vibrant natural lighting with high contrast and rich colors, the camera capturing spontaneous moments and authentic interactions, creating a lively, documentary-style feel with contemporary visual language and an engaging, relatable mood. The video begins with a wide establishing shot that quickly cuts to medium shots and close-ups, using fast-paced editing and dynamic camera work including tracking shots, whip pans, and handheld movements that create a sense of immediacy and energy. The lighting varies from natural daylight to golden hour warmth, with dramatic shadows and highlights that add visual interest and depth to each frame. The camera work emphasizes movement and action, following subjects as they interact with their environment, using techniques like rack focus, shallow depth of field, and varied shot sizes to create a compelling visual narrative that keeps viewers engaged throughout the entire video experience.",
            
            f"An artistic slow-motion video capturing {user_prompt} through a series of carefully composed shots that emphasize beauty and emotion, beginning with a wide establishing shot that slowly zooms in, featuring dramatic lighting with strong shadows and highlights, the camera executing graceful movements like slow pans and gentle tilts, creating a dreamlike, atmospheric visual poem with moody color grading and an introspective, contemplative tone. The video unfolds with deliberate pacing, using techniques like time-lapse effects, slow-motion sequences, and smooth camera transitions to create a meditative viewing experience. The cinematography emphasizes visual storytelling through careful composition, with each shot carefully framed to highlight the subject's emotional journey and environmental context. The lighting design creates a rich, layered visual experience with warm and cool tones that shift throughout the video, while the camera movements remain fluid and purposeful, guiding the viewer's attention through the narrative with elegant precision and artistic flair that transforms a simple concept into a compelling visual story."
        ]


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

        # Capture Veo file metadata for future extensions
        veo_file_name = getattr(getattr(generated_video, 'video', None), 'name', None)
        veo_file_uri = getattr(getattr(generated_video, 'video', None), 'uri', None)
        veo_mime_type = getattr(getattr(generated_video, 'video', None), 'mime_type', None)
        veo_metadata = {
            'veo_file_name': veo_file_name,
            'veo_file_uri': veo_file_uri,
            'veo_mime_type': veo_mime_type,
        }
        metadata = load_job_metadata(job)
        metadata['veo_metadata'] = veo_metadata
        save_job_metadata(job, metadata)
        job.save(update_fields=['note'])
        
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


def extend_video_with_veo(job_id, prompt, source_veo_metadata):
    """Extend video duration by 7 seconds using Google Veo 3.1 API"""
    try:
        # Get API key from environment
        gemini_api_key = os.getenv('NANO_BANANA_API_KEY')
        if not gemini_api_key:
            raise Exception("Google Gemini API key not configured. Please add NANO_BANANA_API_KEY to your .env file")
        
        print(f"🎬 Starting video extension for job {job_id}")
        print(f"📝 Prompt: {prompt}")
        print(f"🔑 API Key: {'Present' if gemini_api_key else 'Missing'}")
        
        # Initialize the Google GenAI client
        client = genai.Client(api_key=gemini_api_key)
        
        # Update job status to processing
        job = VideoGenerationJob.objects.get(job_id=job_id)
        job.status = 'processing'
        job.started_at = datetime.now()
        job.progress = 10
        job.save()
        
        if not source_veo_metadata:
            raise Exception("No Veo metadata found for source video. Please regenerate the video before extending.")
        
        veo_file_name = source_veo_metadata.get('veo_file_name')
        veo_file_uri = source_veo_metadata.get('veo_file_uri')
        veo_mime_type = source_veo_metadata.get('veo_mime_type')
        print(f"📁 Source Veo metadata: name={veo_file_name}, uri={veo_file_uri}")
        
        if not veo_file_uri and veo_file_name:
            try:
                clean_file = client.files.get(name=veo_file_name)
                veo_file_uri = getattr(clean_file, 'uri', None)
                veo_mime_type = getattr(clean_file, 'mime_type', veo_mime_type)
                print(f"🔁 Retrieved Veo file via files.get(): uri={veo_file_uri}")
            except Exception as fetch_error:
                print(f"⚠️ Could not retrieve Veo file {veo_file_name}: {str(fetch_error)}")
                raise Exception("Unable to retrieve Veo-generated source video for extension") from fetch_error
        
        if not veo_file_uri:
            raise Exception("Veo file reference missing or invalid. Please regenerate the original video before extending.")
        
        video_file_ref = types.Video(uri=veo_file_uri)
        video_source = types.GenerateVideosSource(
            prompt=prompt,
            video=video_file_ref,
        )
        
        # Update progress
        job.progress = 30
        job.save()
        
        print(f"🎬 Calling Veo 3.1 API for video extension...")
        print(f"   - Prompt: {prompt[:100]}...")
        print(f"   - Source video URI: {veo_file_uri}")
        
        operation = client.models.generate_videos(
            model="veo-3.1-generate-preview",
            source=video_source,
        )
        
        # Update progress
        job.progress = 40
        job.save()
        
        # Poll the operation status until the video is ready
        print(f"⏳ Waiting for video extension to complete...")
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
        
        print(f"✅ Video extension completed, downloading...")
        
        # Check if operation has response and generated_videos
        if not operation.response:
            raise Exception("No response from video extension operation")
        
        if not hasattr(operation.response, 'generated_videos') or not operation.response.generated_videos:
            print(f"⚠️ Response structure: {dir(operation.response)}")
            print(f"⚠️ Response content: {operation.response}")
            raise Exception("No generated videos in response")
        
        # Download the extended video
        generated_video = operation.response.generated_videos[0]
        print(f"📹 Extended video object: {generated_video}")

        # Capture Veo metadata for the newly generated clip so it can be extended again
        veo_file_name = getattr(getattr(generated_video, 'video', None), 'name', None)
        veo_file_uri = getattr(getattr(generated_video, 'video', None), 'uri', None)
        veo_mime_type = getattr(getattr(generated_video, 'video', None), 'mime_type', None)
        veo_metadata = {
            'veo_file_name': veo_file_name,
            'veo_file_uri': veo_file_uri,
            'veo_mime_type': veo_mime_type,
        }
        metadata = load_job_metadata(job)
        metadata['veo_metadata'] = veo_metadata
        save_job_metadata(job, metadata)
        job.save(update_fields=['note'])
        
        # Create filename for the extended video
        video_filename = f"video_extended_{job_id}_{int(time.time())}.mp4"
        
        # Download video content
        if hasattr(generated_video, 'video'):
            video_content = client.files.download(file=generated_video.video)
        elif hasattr(generated_video, 'uri'):
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
        
        print(f"💾 Extended video saved: {video_path} (size: {len(video_content)} bytes)")
        
        # Update job with completion details
        job.status = 'completed'
        job.completed_at = datetime.now()
        job.progress = 100
        job.video_file_path = video_path
        job.video_url = f"{settings.MEDIA_URL}{video_path}"
        job.save()
        
        print(f"✅ Video extension completed successfully for job {job_id}")
        
    except Exception as e:
        print(f"❌ Error extending video for job {job_id}: {str(e)}")
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
            
            # Process CSV feedback file if provided
            feedback_data = []
            csv_files: list = []
            print(f"🔍 Checking for CSV files in request.FILES: {list(request.FILES.keys())}")

            # Collect csv files from getlist('csv_file') if available
            try:
                csv_files = list(request.FILES.getlist('csv_file'))  # type: ignore[attr-defined]
            except Exception:
                csv_files = []

            # Also collect any indexed csv_file_* keys
            for key, file in request.FILES.items():
                if key.startswith('csv_file_'):
                    csv_files.append(file)

            if csv_files:
                print(f"✅ {len(csv_files)} CSV file(s) detected")
                for idx, file in enumerate(csv_files, start=1):
                    print(f"📁 CSV[{idx}]: {file.name} ({file.content_type}, {file.size} bytes)")
                    if (file.content_type == 'text/csv' or file.content_type == 'text/plain' or file.name.endswith('.csv') or file.name.endswith('.txt')):
                        try:
                            data = process_csv_feedback(file)
                            feedback_data.extend(data)
                        except Exception as e:
                            print(f"❌ Error processing CSV file {file.name}: {str(e)}")
                            continue
                    else:
                        print(f"⚠️ Skipping CSV file with wrong content type: {file.content_type}")
             
            # Generate enhanced prompt using OpenAI if CSV feedback is provided
            final_prompt = prompt  # Default to user's original prompt
            original_prompt = prompt  # Store original prompt
            
            if feedback_data:
                print("🤖 CSV feedback detected - Generating enhanced prompt with OpenAI...")
                print(f"📈 Feedback entries (merged across files): {len(feedback_data)}")
                final_prompt = generate_enhanced_prompt_with_openai(prompt, feedback_data)
                print(f"🎯 FINAL ENHANCED PROMPT FOR VIDEO GENERATION: {final_prompt}")
            else:
                print("📝 No CSV feedback - Using original user prompt")
                print(f"🎯 FINAL PROMPT FOR VIDEO GENERATION: {final_prompt}")
            
            # Create video generation job
            job = VideoGenerationJob.objects.create(
                user=user,
                prompt=final_prompt,  # Use enhanced prompt
                original_prompt=original_prompt,  # Store original prompt
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
                    'prompt': job.prompt,  # Enhanced prompt (final prompt used for generation)
                    'original_prompt': job.original_prompt,  # User's original prompt
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


def refine_video_prompt_with_openai(base_prompt, additional_details):
    """Refine a selected video prompt with additional user details using OpenAI
    
    Args:
        base_prompt (str): The base video prompt selected by user
        additional_details (str): Additional details provided by user
        
    Returns:
        str: A more detailed, refined video prompt
    """
    try:
        # Get OpenAI API key
        openai_api_key = os.getenv('OPENAI_API_KEY')
        
        if not openai_api_key:
            print("⚠️ OpenAI API key not found - returning combined prompt")
            return f"{base_prompt}. Additional details: {additional_details}"
        
        print("🔄 Refining video prompt with OpenAI...")
        print(f"📝 Base prompt length: {len(base_prompt)} characters")
        print(f"➕ Additional details length: {len(additional_details)} characters")
        
        # Initialize OpenAI client
        client = openai.OpenAI(api_key=openai_api_key)
        
        # Create a comprehensive refinement prompt for VIDEO
        refinement_prompt = f"""You are an expert at refining and enhancing VIDEO generation prompts for professional-grade AI video generation tools like Google Veo.

BASE VIDEO PROMPT (already detailed):
{base_prompt}

ADDITIONAL USER DETAILS TO INCORPORATE:
{additional_details}

YOUR TASK:
Create ONE extremely detailed, refined VIDEO prompt that intelligently merges the base prompt with the additional details.

CRITICAL RULES FOR VIDEO PROMPTS:

1. **VIDEO ELEMENT REPLACEMENT**: If the additional details specify video-specific elements (camera movements, scene dynamics, motion, pacing, cinematography, lighting, mood), REPLACE the corresponding elements in the base prompt. Do NOT keep both versions.

Examples:
- If base has "slow pan" and user adds "fast tracking shot" → Use "fast tracking shot"
- If base has "static camera" and user adds "handheld movement" → Use "handheld movement"
- If base has "slow motion" and user adds "real-time" → Use "real-time"
- If base has "golden hour" and user adds "blue hour" → Use "blue hour"
- If base has "subject walking" and user adds "subject running" → Use "subject running"

2. **ADDITIVE ELEMENTS**: If the additional details add NEW elements (objects, actions, environmental details, characters, props) that don't contradict the base, seamlessly integrate them.

Examples:
- Base: "person walking in park" + Additional: "add flying birds in background" → Include both
- Base: "sunset scene" + Additional: "add gentle wind blowing trees" → Include both

3. **LENGTH & DETAIL**: The refined prompt MUST be 100-150 words. Focus on VIDEO-SPECIFIC elements:
- Camera movements and cinematography (dolly, pan, zoom, tracking, aerial, handheld, etc.)
- Subject actions and movements (walking, running, gesturing, interacting, etc.)
- Scene dynamics (environmental changes, interactions, temporal progression)
- Motion details (speed, direction, type of action)
- Lighting and atmosphere specific to video
- Frame rate feel (slow motion, real-time, time-lapse)
- Shot types and composition
- Temporal flow (how the video unfolds from beginning to end)

4. **VIDEO-FIRST FOCUS**: This is a VIDEO prompt - emphasize:
- What MOVES and HOW it moves
- Camera work and cinematography
- Actions that unfold over TIME
- Scene progression and temporal elements
- NOT static image descriptions

5. **PROFESSIONAL LANGUAGE**: Use cinematography, videography, and filmmaking terminology. Be specific and technical.

EXAMPLE OF DETAIL LEVEL NEEDED FOR VIDEO:
"A cinematic 8-second video opening with a smooth aerial drone shot that gracefully descends from 50 feet toward a young woman in a flowing white sundress spinning joyfully in a sunlit meadow filled with golden wildflowers, the camera executing a slow 360-degree circular orbit around her at eye level as she twirls with arms outstretched, capturing her dress billowing in the gentle breeze and her long chestnut hair floating in the wind, the wildflowers swaying rhythmically in synchronized motion, warm golden hour sunlight streaming through from camera left at 45 degrees creating beautiful natural lens flares and soft bokeh effects in the background, the video maintaining smooth gimbal-stabilized movement throughout, ending with a slow push-in to a medium close-up of her radiant smiling face as she looks directly at camera with pure joy, shot at 60fps for slight slow-motion effect, color graded with warm honey tones and soft highlights, creating a dreamy, ethereal, uplifting atmosphere that captures the essence of carefree happiness"

NOW CREATE THE REFINED VIDEO PROMPT - Make it highly detailed, 100-150 words, with ALL style changes from the user incorporated, focusing on MOVEMENT, CAMERA WORK, and TEMPORAL FLOW:

REFINED VIDEO PROMPT:"""

        # Call OpenAI API with retry logic
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = client.chat.completions.create(
                    model="gpt-3.5-turbo",
                    messages=[
                        {"role": "user", "content": refinement_prompt}
                    ],
                    max_tokens=800,
                    temperature=0.8,
                    top_p=0.9,
                    frequency_penalty=0.1,
                    presence_penalty=0.3
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
        
        # Remove "REFINED VIDEO PROMPT:" prefix if present
        if refined_prompt.startswith("REFINED VIDEO PROMPT:"):
            refined_prompt = refined_prompt.replace("REFINED VIDEO PROMPT:", "").strip()
        
        print("=" * 80)
        print("🎯 REFINED VIDEO PROMPT GENERATED:")
        print("=" * 80)
        print(refined_prompt)
        print("=" * 80)
        print(f"✅ Refined prompt length: {len(refined_prompt)} characters (~{len(refined_prompt.split())} words)")
        print("=" * 80)
        
        return refined_prompt
        
    except Exception as e:
        print(f"❌ Error refining video prompt: {str(e)}")
        import traceback
        print(f"📋 Full traceback: {traceback.format_exc()}")
        # Return combined prompt as fallback
        return f"{base_prompt}. Additional details: {additional_details}"


class VideoPromptGenerationView(APIView):
    """Generate three video prompt variations using OpenAI"""
    
    def post(self, request):
        """Generate three different video prompt variations based on user input"""
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
            
            # Check for CSV files - prompt generation should only work without them
            has_csv_file = any(key == 'csv_file' for key in request.FILES.keys())
            
            if has_csv_file:
                return Response(
                    ResponseInfo.error("Prompt generation is only available when no CSV files are provided"),
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Generate three video prompt variations using OpenAI
            print("🎬 Generating three video prompt variations with OpenAI...")
            prompt_variations = generate_three_video_prompts_with_openai(prompt)
            
            # Format the response - join prompts with ||| for frontend parsing
            prompts_string = "|||".join(prompt_variations)
            
            response_data = {
                "original_prompt": prompt,
                "prompts": prompts_string,  # Pipe-separated string for frontend
                "prompt_variations": prompt_variations,  # List for backward compatibility
                "generation_type": "video"
            }
            
            return Response(
                ResponseInfo.success(response_data, "Three video prompt variations generated successfully"),
                status=status.HTTP_200_OK
            )
            
        except Exception as e:
            print(f"❌ Error generating video prompt variations: {str(e)}")
            import traceback
            print(f"📋 Full traceback: {traceback.format_exc()}")
            return Response(
                ResponseInfo.error(f"Failed to generate video prompt variations: {str(e)}"),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class RefineVideoPromptView(APIView):
    """Refine a selected video prompt with additional details"""
    
    def post(self, request):
        """Refine a video prompt by incorporating additional user details"""
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
            
            # Refine the video prompt using OpenAI
            print(f"🔧 Refining video prompt with additional details...")
            print(f"📝 Base prompt: {base_prompt[:100]}...")
            print(f"➕ Additional details: {additional_details}")
            
            refined_prompt = refine_video_prompt_with_openai(base_prompt, additional_details)
            
            response_data = {
                "base_prompt": base_prompt,
                "additional_details": additional_details,
                "refined_prompt": refined_prompt
            }
            
            return Response(
                ResponseInfo.success(response_data, "Video prompt refined successfully"),
                status=status.HTTP_200_OK
            )
            
        except Exception as e:
            print(f"❌ Error refining video prompt: {str(e)}")
            import traceback
            print(f"📋 Full traceback: {traceback.format_exc()}")
            return Response(
                ResponseInfo.error(f"Failed to refine video prompt: {str(e)}"),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class VideoExtendView(APIView):
    """API view for extending video duration using Google Veo 3.1"""
    parser_classes = [MultiPartParser, FormParser]
    
    def post(self, request):
        try:
            print(f"🎬 Video extension request received")
            print(f"📊 Request data: {request.data}")
            
            # Get current user
            user = get_current_user(request)
            print(f"👤 User: {user.email if user else 'Anonymous'}")
            
            # Extract form data
            source_job_id = request.data.get('source_job_id', '').strip()
            prompt = request.data.get('prompt', '').strip()
            
            print(f"📝 Extracted data - Source Job ID: {source_job_id}, Prompt: {prompt}")
            
            # Validate required fields
            if not source_job_id:
                print("❌ Validation failed: Source job ID is required")
                return Response(
                    ResponseInfo.error("Source job ID is required"),
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            if not prompt:
                print("❌ Validation failed: Prompt is required")
                return Response(
                    ResponseInfo.error("Prompt is required for video extension"),
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Get the source video job
            try:
                source_job = VideoGenerationJob.objects.get(job_id=source_job_id)
            except VideoGenerationJob.DoesNotExist:
                return Response(
                    ResponseInfo.error("Source video job not found"),
                    status=status.HTTP_404_NOT_FOUND
                )
            
            # Check if source job has a completed video
            if source_job.status != 'completed' or not source_job.video_file_path:
                return Response(
                    ResponseInfo.error("Source video must be completed to extend it"),
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Check if user has permission to extend this video
            if user and source_job.user and source_job.user != user:
                return Response(
                    ResponseInfo.error("Access denied"),
                    status=status.HTTP_403_FORBIDDEN
                )
            
            # Ensure the source job has Veo metadata (only Veo-generated videos can be extended)
            source_veo_metadata = get_veo_file_metadata(source_job)
            veo_uri_present = source_veo_metadata.get('veo_file_uri')
            veo_name_present = source_veo_metadata.get('veo_file_name')
            if not (veo_uri_present or veo_name_present):
                return Response(
                    ResponseInfo.error(
                        "Original Veo file reference is missing. Please regenerate the video before extending."
                    ),
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Create a new video generation job for the extended video
            extended_job = VideoGenerationJob.objects.create(
                user=user,
                prompt=prompt,
                original_prompt=f"Extended from job {source_job_id}: {prompt}",
                style=source_job.style,
                quality=source_job.quality,
                duration=source_job.duration + 7,  # Add 7 seconds
                status='queued'
            )
            
            print(f"✅ Extended video job created with ID: {extended_job.job_id}")
            
            # Start video extension in background thread
            thread = threading.Thread(
                target=extend_video_with_veo,
                args=(extended_job.job_id, prompt, dict(source_veo_metadata))
            )
            thread.daemon = True
            thread.start()
            
            print(f"🚀 Background thread started for extended video job {extended_job.job_id}")
            
            return Response(
                ResponseInfo.success({
                    'job_id': str(extended_job.job_id),
                    'source_job_id': str(source_job_id),
                    'status': extended_job.status,
                    'prompt': prompt,
                    'duration': extended_job.duration
                }, "Video extension started successfully"),
                status=status.HTTP_201_CREATED
            )
            
        except Exception as e:
            print(f"❌ Error in VideoExtendView: {str(e)}")
            import traceback
            print(f"📋 Full traceback: {traceback.format_exc()}")
            return Response(
                ResponseInfo.error(f"Failed to start video extension: {str(e)}"),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
