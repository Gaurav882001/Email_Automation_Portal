import os
import uuid
import base64
import warnings
import threading
import requests
import csv
import io
import time
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
                'improvement_suggestion': csv_content.strip()
            }]
            print(f"✅ Successfully processed 1 text feedback entry: {feedback_data[0]['description']}")
            return feedback_data
        else:
            print("⚠️ Empty CSV content")
            return []
            
    except Exception as e:
        print(f"❌ Error processing CSV feedback: {str(e)}")
        print(f"📄 CSV file type: {type(csv_file)}")
        print(f"📄 CSV file name: {getattr(csv_file, 'name', 'Unknown')}")
        return []


def extract_review_text_from_csv(feedback_data):
    """Extract review text from CSV data based on new CSV structure
    
    Checks for 'Review Text' column (new format) or falls back to other text fields
    """
    if not feedback_data:
        return ""
    
    review_texts = []
    
    for row in feedback_data:
        # Check for new CSV format columns
        if 'Review Text' in row and row['Review Text']:
            review_texts.append(row['Review Text'].strip())
        elif 'Review Title' in row and row['Review Title']:
            review_texts.append(row['Review Title'].strip())
        # Fallback to old format columns
        elif 'description' in row and row['description']:
            review_texts.append(row['description'].strip())
        elif 'improvement_suggestion' in row and row['improvement_suggestion']:
            review_texts.append(row['improvement_suggestion'].strip())
    
    # Join all review texts with newline separator
    combined_review_text = "\n".join(review_texts)
    print(f"📝 Extracted Review Text: {combined_review_text[:300]}...")
    return combined_review_text


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
        system_prompt = """You are an expert at enhancing image generation prompts based on product review feedback. 
        Analyze the user's prompt and the provided customer reviews to create an improved, more detailed prompt 
        that will generate better images. Focus on:
        1. Understanding customer preferences from review ratings and text
        2. Incorporating specific product features mentioned in reviews
        3. Adding visual elements that customers appreciate
        4. Enhancing composition based on verified purchase feedback
        5. Including style and quality elements that align with high-rated reviews
        6. Adding technical photography terms
        7. Improving visual descriptions based on customer sentiment
        
        Return only the enhanced prompt, no explanations."""
        
        user_message = f"""
        Original user prompt: "{user_prompt}"
        
        {feedback_summary}
        
        Please enhance this prompt based on the product review feedback to create a better image generation prompt. 
        Consider the ratings, customer feedback, and product details from the reviews to make the image more aligned with what customers appreciate.
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


def generate_three_prompts_with_openai(user_prompt, feedback_data=None):
    """Generate three different prompt variations using OpenAI based on user input and optional feedback
    
    Args:
        user_prompt (str): User's original prompt
        feedback_data (list): Optional CSV feedback data
        
    Returns:
        list: Three different prompt variations
    """
    try:
        # Get OpenAI API key
        openai_api_key = os.getenv('OPENAI_API_KEY')
        
        if not openai_api_key:
            print("⚠️ OpenAI API key not found in environment variables")
            print("💡 Please add OPENAI_API_KEY to your .env file")
            # Return three variations of the original prompt using Midjourney structure
            return [
                f"Photograph, professional style, medium shot of {user_prompt}, natural lighting, clean composition",
                f"Digital illustration, artistic style, wide shot of {user_prompt}, vibrant colors, detailed atmosphere",
                f"Oil painting, classical style, close-up of {user_prompt}, dramatic lighting, rich textures"
            ]
        
        print(f"🤖 OpenAI API Key: {'Present' if openai_api_key else 'Missing'}")
        
        # Prepare feedback summary if available
        # feedback_summary = ""
        # if feedback_data:
        #     feedback_summary = "Based on the following product review feedback:\n"
            
        #     for i, feedback in enumerate(feedback_data[:3]):  # Limit to first 3 entries for prompt generation
        #         product_name = feedback.get('Product Name', 'Unknown Product')
        #         rating = feedback.get('Rating', 'N/A')
        #         review_text_field = feedback.get('Review Text', '')
        #         review_title = feedback.get('Review Title', '')
                
        #         feedback_summary += f"\n📦 Review {i+1}:\n"
        #         feedback_summary += f"  • Product: {product_name}\n"
        #         feedback_summary += f"  • Rating: {rating}/5 stars\n"
        #         if review_title:
        #             feedback_summary += f"  • Title: {review_title}\n"
        #         if review_text_field:
        #             feedback_summary += f"  • Review: {review_text_field[:150]}...\n"
        
        # Create system prompt for generating three variations using Midjourney structure

        system_prompt = """You are an expert at creating high-quality, detailed image generation prompts using the Midjourney prompt structure. 
        Based on the user's input, create THREE different comprehensive prompt variations that follow the Midjourney formula:

        STRUCTURE: Medium, Style, Illustration Type, Photography Style, Vibes & Moods, Artistic Technique, Composition, Scene Setting, Atmosphere

        Key Elements to include:

        1. STYLE: General visual style
        Options: Realistic, Abstract, Minimalist, Maximalist, Modern, Contemporary, Traditional

        2. ILLUSTRATION TYPE:
        Options:
        • Traditional: Classic hand-drawn illustration techniques
        • Vintage/Retro: Retro-inspired travel posters with vibrant colors, bold typography, nostalgic aesthetic
        • Realism: Highly detailed realistic illustrations capturing delicate textures, subtle color variations
        • Fantasy: Mythical scenes with magical creatures, glowing flora, ethereal beings
        • Cartoon: Animated cartoon style with funny scenes and exaggerated expressions
        • Anime: Japanese animation style with unique character designs and energetic movements (use --niji flag)
        • Fashion: Glamorous fashion illustrations with intricate details, statement accessories
        • Line Art: Clean and precise lines forming geometric patterns and visually captivating compositions
        • Flat Graphic Art: Bold flat 2D illustrations in vector graphic style with simple, eye-catching elements
        • Caricature: Exaggerated features capturing unique characteristics in humorous ways

        3. PHOTOGRAPHY STYLE:
        Options:
        • Composition: Rule of thirds, off-center subjects, balanced and visually engaging arrangements
        • Camera Angle: High angle, low angle, bird's eye view, worm's eye view, Dutch angle perspectives
        • Exposure: Overexposure (dreamy effects), underexposure (dramatic mood), long exposure (motion blur)
        • Lighting: Golden hour, blue hour, hard light, soft light, backlighting, rim lighting, dramatic shadows
        • Film Stocks: Cabinet Card, Kodak Tri-X 400, vintage film aesthetics, grain and contrast effects
        • Experimental: Double exposure, light painting, intentional camera movement, abstract techniques
        • Black and White: Classic monochrome with grain and contrast, timeless portraits, high-contrast imagery
        • Portraits: Environmental portraits, studio portraits, candid moments, connection to surroundings

        4. VIBES & MOODS:
        Options:
        • AESTHETICS - Cyberpunk: Neon lights, futuristic urban landscapes, high-tech metropolises, dazzling visuals at night
        • AESTHETICS - Americana: Classic diners, neon signs, vintage cars, nostalgic slice-of-life scenes
        • AESTHETICS - Dark Academia: Scholarly settings, tweed jackets, vintage books, intellectual and moody atmosphere
        • AESTHETICS - Steampunk: Retro-futuristic airships, gears and brass, leather aviator jackets, Victorian-era technology
        • AESTHETICS - Retro Eras: 1920s flapper aesthetic, Art Deco architecture, jazz age glamour, specific decade styles
        • AESTHETICS - Horrorcore: Wicked carnival scenes, macabre elements, nightmarish twists, unsettling atmosphere
        • EMOTIONS - Happiness: Joyful, cheerful, uplifting mood
        • EMOTIONS - Sadness: Melancholic, somber, reflective mood
        • EMOTIONS - Fear: Tension, unease, suspenseful atmosphere
        • ATMOSPHERE - Calm: Peaceful, serene, tranquil setting
        • ATMOSPHERE - Romantic: Intimate, dreamy, tender moments
        • ATMOSPHERE - Gloomy/Unsettling: Eerie, mysterious, foreboding mood

        5. ARTISTIC TECHNIQUE:
        Options:
        • DRAWING: Pencil, Charcoal, Mechanical pencil, Ink drawing
        • PAINTING: Watercolor, Oil painting, Acrylic, Gouache, Tempera
        • SCULPTURE: Clay modeling, Additive techniques, Subtractive carving
        • PRINTMAKING: Screen printing, Woodcut, Engraving, Lithography
        • ART HISTORY PERIODS: Ancient, Medieval, Renaissance, Impressionism, Art Nouveau, Surrealism, Pop Art, Modern
        • 3D ART: Clay, Wood carving, Stone and Marble, Metal casting, Glass, Papercraft, CGI Animation, Isometric view
        • LOGOS: Lettermark, Mascot, Emblem, Icon-based designs

        6. COMPOSITION: Camera framing and angles
        Options:
        • FRAMING: Wide shot, Medium shot, Close-up, Extreme close-up, Full body, Portrait
        • CAMERA ANGLES: High angle (from above), Low angle (from below), Eye level, Bird's eye view, Worm's eye view, Dutch angle (tilted), Aerial view
        • COMPOSITION TECHNIQUES: Rule of thirds (off-center subject), Centered composition, Symmetrical, Asymmetrical, Leading lines, Depth of field (shallow/deep focus)

        7. SCENE SETTING: What the subject is doing, actions, props, and locations
        Include specific details about:
        • Subject's actions and activities
        • Props and objects in the scene
        • Location and environment
        • Interactions and context
        • Environmental details (connection to surroundings)

        8. ATMOSPHERE: Lighting, weather, mood, and emotional tone
        Options:
        • LIGHTING: Golden hour (warm sunset/sunrise glow), Blue hour, Hard light, Soft light, Backlighting, Rim lighting, Dramatic shadows, Studio lighting, Natural light, Neon glow
        • EXPOSURE SETTINGS: Overexposure (dreamy soft effect), Underexposure (dramatic mood), Long exposure (motion blur), Balanced exposure
        • WEATHER: Sunny, Cloudy, Rainy, Foggy, Snowy, Stormy
        • MOOD: Energetic, Mysterious, Nostalgic, Peaceful, Intense, Whimsical

        CRITICAL USER INPUT PRESERVATION RULE (MANDATORY - HIGHEST PRIORITY):
        - ANALYZE the user's prompt FIRST to identify ANY specified elements
        - If the user mentions ANY keyword from Illustration Type, Photography Style, Vibes & Moods, Artistic Technique, Composition, or Atmosphere, you MUST preserve it EXACTLY in ALL three prompts

        KEYWORDS TO DETECT AND PRESERVE:

        ILLUSTRATION TYPE Keywords: Traditional, Vintage, Retro, Realism, Realistic illustration, Fantasy, Cartoon, Anime, Fashion illustration, Line Art, Flat Graphic, Caricature

        PHOTOGRAPHY STYLE Keywords: Portrait photography, Landscape photography, Street photography, Experimental photography, Double exposure, Light painting, Black and White photography, Monochrome, Film photography, Kodak Tri-X, Cabinet Card, High angle, Low angle, Bird's eye view, Worm's eye view, Dutch angle, Aerial view, Overexposed, Underexposed, Long exposure, Motion blur 

        VIBES & MOODS Keywords: Cyberpunk, Americana, Dark Academia, Steampunk, Retro, Horrorcore, Happiness, Sadness, Fear, Calm, Romantic, Gloomy, Unsettling, Eerie, Mysterious

        ARTISTIC TECHNIQUE Keywords: Pencil drawing, Charcoal, Watercolor, Oil painting, Acrylic, Screen printing, Woodcut, Engraving, Ancient, Medieval, Renaissance, Impressionism, Art Nouveau, Surrealism, Pop Art, CGI, Isometric, Lettermark, Mascot, Emblem

        COMPOSITION Keywords: Wide shot, Medium shot, Close-up, Extreme close-up, Full body, Portrait shot, Rule of thirds, Off-center, Centered, Symmetrical, Asymmetrical, Leading lines, Shallow focus, Deep focus

        ATMOSPHERE Keywords: Golden hour, Blue hour, Hard light, Soft light, Backlighting, Rim lighting, Dramatic shadows, Studio lighting, Natural light, Neon glow, Sunny, Cloudy, Rainy, Foggy, Snowy, Stormy

        PRESERVATION RULES' EXAMPLES:
        - If user mentions "cartoon" → Illustration Type MUST be "Cartoon" in ALL three prompts
        - If user mentions "high angle" → Photography Style or Composition MUST include "High angle" in ALL three prompts
        - If user mentions "golden hour" → Atmosphere MUST include "Golden hour lighting" in ALL three prompts
        - If user mentions "cyberpunk" → Vibes & Moods MUST include "Cyberpunk aesthetic" in ALL three prompts
        - If user mentions "watercolor" → Artistic Technique MUST be "Watercolor painting" in ALL three prompts
        - If user mentions "close-up" → Composition MUST include "Close-up" in ALL three prompts
        - If user mentions "anime" → Illustration Type MUST be "Anime" in ALL three prompts
        - If user mentions "double exposure" → Photography Style MUST include "Double exposure" in ALL three prompts
        - If user mentions "dramatic shadows" → Atmosphere MUST include "Dramatic shadows" in ALL three prompts

        IMPORTANT: Do NOT change, modify, substitute, or suggest alternatives for ANY user-specified element. Only elaborate and create variations for elements NOT specified by the user.

        IMPORTANT REQUIREMENTS FOR DETAILED PROMPTS:
        - Each prompt MUST be 50-120 words long with VERY rich, detailed descriptions
        - Use the user's input as the core subject and SIGNIFICANTLY EXPAND upon it
        - Preserve ALL user-specified elements while adding extensive elaboration
        - Include MULTIPLE specific technical terms, artistic details, and photography terminology
        - Add VIVID sensory details: specific colors (not just "blue" but "deep cobalt blue", "warm amber"), detailed textures (rough canvas, smooth silk, weathered leather), precise lighting descriptions
        - Use HIGHLY descriptive adjectives and creative language throughout
        - Describe the scene with cinematic detail: foreground, midground, background elements
        - Add environmental context: time of day, weather conditions, surrounding atmosphere
        - Include subject details: clothing, expressions, poses, interactions
        - Specify materials, surfaces, and physical properties
        - Add emotional and mood descriptors
        - Include technical camera/art specifications when relevant

        For each of the THREE prompts:
        - PRESERVE all user-specified elements across all three prompts
        - Use completely different options ONLY for elements the user did NOT specify
        - Vary unspecified illustration types, photography styles, moods, and techniques
        - Change unspecified composition angles and perspectives
        - Create different scene settings if not specified
        - Include varied lighting conditions and moods if not specified
        - Add detailed environmental and contextual elements
        - Make each prompt comprehensive and visually rich while respecting user constraints

        CRITICAL FORMAT REQUIREMENTS:
        - Return EXACTLY 3 prompts
        - Separate each prompt with "|||" (three pipe characters)
        - Each prompt must be on a single line
        - No explanations, no numbering, no additional text
        - No line breaks within prompts
        - Just the three comprehensive structured prompts separated by "|||"
        - Only include relevant elements in each prompt (not all 9 elements are needed for every prompt)

        MANDATORY FORMAT (adapt based on what's relevant):
        Medium: [type], Style: [description], [Illustration Type/Photography Style/Vibes & Moods/Artistic Technique as relevant]: [details], Composition: [type], Scene Setting: [detailed description], Atmosphere: [lighting and mood]|||[Second prompt]|||[Third prompt]

        Remember: Use "|||" to separate the three prompts, nothing else! And NEVER change elements specified by the user!"""

        user_message = f"""
        User's original prompt: "{user_prompt}"

        
        YOUR PROMPTS MUST BE THIS DETAILED! Expand every element with rich, specific descriptions!
        """
        
        # Merge system prompt and user message into a comprehensive single prompt
        merged_prompt = f"""{system_prompt}
        
        ==================================================================================
        USER'S ORIGINAL INPUT TO EXPAND:
        ==================================================================================
        
        {user_message}
        
        ==================================================================================
        FINAL INSTRUCTIONS:
        ==================================================================================
        
        Based on the user's prompt "{user_prompt}", create THREE highly detailed, comprehensive prompt variations.
        
        Each variation MUST:
        1. Be 50-120 words long with EXTENSIVE descriptive detail
        2. PRESERVE all user-specified elements (style, composition, mood, technique, etc.)
        3. SIGNIFICANTLY EXPAND the user's concept with:
           - Specific colors (deep cobalt blue, warm amber, crimson red, etc.)
           - Detailed textures (rough canvas, smooth silk, weathered leather, etc.)
           - Precise lighting (soft diffused backlight, dramatic rim lighting, etc.)
           - Environmental context (time of day, weather, atmosphere, surroundings)
           - Subject details (clothing, expressions, poses, interactions, accessories)
           - Materials and surfaces (brushed metal, polished marble, aged wood, etc.)
           - Emotional descriptors (serene, energetic, melancholic, etc.)
           - Technical specs (shallow depth of field, f/1.8, 85mm lens, etc.)
           - Foreground, midground, and background elements
        
        Return ONLY the three detailed prompts separated by "|||" (three pipe characters).
        No explanations, just the three comprehensive prompts.
        
        CRITICAL: Each prompt MUST be AT LEAST 80 words with rich, elaborate descriptions. Think like a professional photographer or artist describing their vision in extreme detail. Do NOT give short, simple prompts!
        
        Example detail level needed:
        Instead of: "Charcoal drawing of a boy eating sweets"
        You must write: "Medium: Charcoal drawing executed with soft blending techniques and rich tonal variations across the entire composition, Style: Realistic with expressive mark-making and dramatic high-contrast areas, Illustration Type: Traditional realistic illustration capturing delicate textures and subtle emotional nuances in every stroke, Composition: Medium shot framed at eye level with shallow depth of field technique, placing the young boy as the central focal point against a softly blurred atmospheric background, Scene Setting: A cheerful 8-year-old boy with tousled dark hair and bright sparkling eyes, wearing a casual striped cotton t-shirt, joyfully savoring an assortment of colorful wrapped candies and rich chocolate treats, his small fingers delicately holding a vibrant red lollipop while his expression radiates pure delight and innocent childhood happiness, Atmosphere: Soft diffused natural window light streaming in from the left creating gentle shadows on his face, warm afternoon glow illuminating the scene with a cozy nostalgic mood, capturing the timeless joy of childhood indulgence in a tender heartwarming moment"
        
        NOW CREATE THE THREE PROMPTS:
        """
        
        print("🔄 Calling OpenAI API for three prompt variations...")
        print(f"📝 Merged prompt length: {len(merged_prompt)} characters")
        
        # Initialize OpenAI client
        client = openai.OpenAI(api_key=openai_api_key)
        
        # Retry logic for API call
        max_retries = 3
        for attempt in range(max_retries):
            try:
                # Call OpenAI API with merged prompt
                response = client.chat.completions.create(
                    model="gpt-3.5-turbo",
                    messages=[
                        {"role": "user", "content": merged_prompt}
                    ],
                    max_tokens=1800,  # Further increased for more detailed prompts
                    temperature=0.9,  # Higher temperature for more creative and detailed variations
                    top_p=0.95,  # Keep high for diversity
                    frequency_penalty=0.1,  # Lower to allow natural repetition of important details
                    presence_penalty=0.5  # Higher to encourage varied vocabulary
                )
                break  # Success, exit retry loop
            except Exception as e:
                print(f"⚠️ OpenAI API call attempt {attempt + 1} failed: {str(e)}")
                if attempt == max_retries - 1:
                    raise e  # Re-raise on final attempt
                time.sleep(1)  # Wait before retry
        
        # Parse the response
        response_text = response.choices[0].message.content.strip()
        print(f"🤖 OpenAI Response: {response_text}")
        print(f"📏 Response length: {len(response_text)} characters")
        
        # Split by the separator and clean up
        raw_prompts = response_text.split("|||")
        print(f"🔍 Raw split result: {len(raw_prompts)} parts")
        for i, part in enumerate(raw_prompts):
            print(f"  Part {i+1}: '{part.strip()}' (length: {len(part.strip())})")
        
        prompts = [prompt.strip() for prompt in raw_prompts if prompt.strip()]
        print(f"✅ Cleaned prompts: {len(prompts)} valid prompts")
        
        # Ensure we have exactly 3 prompts
        if len(prompts) != 3:
            print(f"⚠️ Expected 3 prompts, got {len(prompts)}. Creating fallback prompts.")
            
            # If we have some prompts but not 3, try to generate more
            if len(prompts) > 0:
                print(f"📝 Found {len(prompts)} valid prompts, generating additional ones...")
                # Keep existing prompts and generate additional ones
                existing_prompts = prompts.copy()
                additional_needed = 3 - len(prompts)
                
                # Generate additional prompts using a different approach
                additional_prompts = []
                for i in range(additional_needed):
                    additional_prompts.append(
                        f"Medium: {'Digital illustration' if i == 0 else 'Watercolor painting' if i == 1 else 'Charcoal drawing'}, "
                        f"Style: {'Modern artistic with vibrant colors' if i == 0 else 'Impressionistic with flowing brushstrokes' if i == 1 else 'Dramatic black and white with high contrast'}, "
                        f"Composition: {'Close-up portrait with shallow depth of field' if i == 0 else 'Wide shot capturing the full scene' if i == 1 else 'Medium shot with dynamic angles'}, "
                        f"Scene Setting: {user_prompt} in a {'contemporary environment with detailed props and settings' if i == 0 else 'natural outdoor setting with rich environmental details' if i == 1 else 'moody, atmospheric environment with detailed textures'}, "
                        f"Atmosphere: {'Soft, diffused lighting with warm golden tones and gentle shadows' if i == 0 else 'Natural daylight filtering through with gentle shadows and atmospheric perspective' if i == 1 else 'High contrast lighting with deep shadows and dramatic mood'}"
                    )
                
                prompts = existing_prompts + additional_prompts
            else:
                # No valid prompts found, create all 3 from scratch
                prompts = [
                    f"Medium: Digital illustration, Style: Modern artistic with vibrant colors, Composition: Close-up portrait with shallow depth of field, Scene Setting: {user_prompt} in a contemporary environment with detailed props and settings, Atmosphere: Soft, diffused lighting with warm golden tones and gentle shadows",
                    f"Medium: Watercolor painting, Style: Impressionistic with flowing brushstrokes, Composition: Wide shot capturing the full scene, Scene Setting: {user_prompt} in a natural outdoor setting with rich environmental details, Atmosphere: Natural daylight filtering through with gentle shadows and atmospheric perspective",
                    f"Medium: Charcoal drawing, Style: Dramatic black and white with high contrast, Composition: Medium shot with dynamic angles, Scene Setting: {user_prompt} in a moody, atmospheric environment with detailed textures, Atmosphere: High contrast lighting with deep shadows and dramatic mood"
                ]
        
        # Final validation - ensure we have exactly 3 prompts
        prompts = [prompt.strip() for prompt in prompts if prompt.strip()]
        
        # If we still don't have 3 prompts, create them from scratch
        if len(prompts) != 3:
            print(f"⚠️ Final validation failed - still have {len(prompts)} prompts. Creating all 3 from scratch.")
            prompts = [
                f"Medium: Digital illustration, Style: Modern artistic with vibrant colors, Composition: Close-up portrait with shallow depth of field, Scene Setting: {user_prompt} in a contemporary environment with detailed props and settings, Atmosphere: Soft, diffused lighting with warm golden tones and gentle shadows",
                f"Medium: Watercolor painting, Style: Impressionistic with flowing brushstrokes, Composition: Wide shot capturing the full scene, Scene Setting: {user_prompt} in a natural outdoor setting with rich environmental details, Atmosphere: Natural daylight filtering through with gentle shadows and atmospheric perspective",
                f"Medium: Charcoal drawing, Style: Dramatic black and white with high contrast, Composition: Medium shot with dynamic angles, Scene Setting: {user_prompt} in a moody, atmospheric environment with detailed textures, Atmosphere: High contrast lighting with deep shadows and dramatic mood"
            ]
        
        # Final cleanup and validation
        prompts = [prompt.strip() for prompt in prompts if prompt.strip()]
        
        # Ensure we have exactly 3 prompts
        if len(prompts) != 3:
            print(f"❌ CRITICAL ERROR: Still have {len(prompts)} prompts after all attempts!")
            # This should never happen, but just in case
            prompts = prompts[:3] if len(prompts) > 3 else prompts + ["Fallback prompt"] * (3 - len(prompts))
        
        print("=" * 80)
        print("🎯 THREE PROMPT VARIATIONS GENERATED:")
        print("=" * 80)
        for i, prompt in enumerate(prompts, 1):
            print(f"📝 Prompt {i}: {prompt}")
        print("=" * 80)
        
        return prompts
        
    except Exception as e:
        print(f"❌ Error generating three prompts: {str(e)}")
        print(f"💡 Falling back to default prompt variations")
        # Return three variations of the original prompt using Midjourney structure
        return [
            f"Photograph, professional style, medium shot of {user_prompt}, natural lighting, clean composition",
            f"Digital illustration, artistic style, wide shot of {user_prompt}, vibrant colors, detailed atmosphere",
            f"Oil painting, classical style, close-up of {user_prompt}, dramatic lighting, rich textures"
        ]


def refine_prompt_with_openai(base_prompt, additional_details):
    """Refine a selected prompt with additional user details using OpenAI
    
    Args:
        base_prompt (str): The base prompt selected by user
        additional_details (str): Additional details provided by user
        
    Returns:
        str: A more detailed, refined prompt
    """
    try:
        # Get OpenAI API key
        openai_api_key = os.getenv('OPENAI_API_KEY')
        
        if not openai_api_key:
            print("⚠️ OpenAI API key not found - returning combined prompt")
            return f"{base_prompt}. Additional details: {additional_details}"
        
        print("🔄 Refining prompt with OpenAI...")
        print(f"📝 Base prompt length: {len(base_prompt)} characters")
        print(f"➕ Additional details length: {len(additional_details)} characters")
        
        # Initialize OpenAI client
        client = openai.OpenAI(api_key=openai_api_key)
        
        # Create a comprehensive refinement prompt
        refinement_prompt = f"""You are an expert at refining and enhancing image generation prompts for professional-grade AI image generation.

        BASE PROMPT (already detailed):
        {base_prompt}

        ADDITIONAL USER DETAILS TO INCORPORATE:
        {additional_details}

        YOUR TASK:
        Create ONE extremely detailed, refined prompt that intelligently merges the base prompt with the additional details.

        CRITICAL RULES:

        1. **STYLE ELEMENT REPLACEMENT**: If the additional details specify photographic/artistic styles (medium, mood, illustration type, lighting, atmosphere, composition, framing, etc.), REPLACE the corresponding elements in the base prompt with the user's specification. Do NOT keep both versions.

        Examples:
        - If base has "realistic photography" and user adds "cartoon style" → Use "cartoon style"
        - If base has "warm lighting" and user adds "cool blue tones" → Use "cool blue tones"
        - If base has "wide shot" and user adds "close-up portrait" → Use "close-up portrait"
        - If base has "cheerful mood" and user adds "melancholic atmosphere" → Use "melancholic atmosphere"

        2. **ADDITIVE ELEMENTS**: If the additional details add NEW elements (objects, colors, specific details, textures, characters, settings) that don't contradict the base, seamlessly integrate them.

        Examples:
        - Base: "boy eating sweets" + Additional: "add vintage bicycle in background" → Include both
        - Base: "sunset scene" + Additional: "add flying birds" → Include both

        3. **LENGTH & DETAIL**: The refined prompt MUST be 250-300 words. Expand every element with:
        - Specific technical details (aperture, focal length, rendering style)
        - Rich color descriptions (hex values, gradients, color theory terms)
        - Texture and material specifications
        - Precise lighting descriptions (direction, quality, color temperature)
        - Detailed atmosphere and mood descriptors
        - Camera angles and perspective details

        4. **STRUCTURE**: Maintain the professional Midjourney structure:
        Medium → Style → Illustration Type → Composition → Scene Setting → Atmosphere → Photography → VIBES AND MOOD → ARTISTIC TECHNIQUES

        5. **PROFESSIONAL LANGUAGE**: Use photography, cinematography, and art terminology. Be specific and technical.

        EXAMPLE OF DETAIL LEVEL NEEDED:
        "Medium: Ultra-high resolution digital photography captured with professional DSLR using 85mm f/1.4 prime lens with creamy bokeh effect, Style: Cinematic realism with film noir aesthetic incorporating dramatic chiaroscuro lighting techniques and desaturated color grading with selective color pops, Illustration Type: Photorealistic with meticulous attention to micro-details in skin texture pores and fabric weave patterns, Composition: Intimate close-up portrait framed at eye level using rule of thirds with subject positioned in left third creating dynamic negative space, shallow depth of field at f/1.8 creating silky smooth background separation, Scene Setting: A contemplative 25-year-old woman with flowing auburn hair catching golden hour sunlight, wearing a vintage cream linen dress with delicate lace details, holding a weathered leather-bound journal, surrounded by soft-focused wildflowers in warm amber and lavender tones, Atmosphere: Dreamy nostalgic ambiance bathed in warm diffused natural sunset light streaming from camera left at 45-degree angle, creating soft rim lighting on hair strands, color palette dominated by warm golds, soft creams, and muted earth tones with hints of lavender, evoking feelings of peaceful introspection and timeless elegance"

        NOW CREATE THE REFINED PROMPT - Make it highly detailed, 250-300 words, with ALL style changes from the user incorporated:

        REFINED PROMPT:"""

        # Call OpenAI API with retry logic for robustness
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = client.chat.completions.create(
                    model="gpt-3.5-turbo",
                    messages=[
                        {"role": "user", "content": refinement_prompt}
                    ],
                    max_tokens=1200,  # Increased for longer, more detailed prompts
                    temperature=0.8,   # Slightly higher for more creative refinement
                    top_p=0.9,
                    frequency_penalty=0.1,  # Reduce repetition
                    presence_penalty=0.3     # Encourage diverse descriptions
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
        
        # Remove "REFINED PROMPT:" prefix if present
        if refined_prompt.startswith("REFINED PROMPT:"):
            refined_prompt = refined_prompt.replace("REFINED PROMPT:", "").strip()
        
        print("=" * 80)
        print("🎯 REFINED PROMPT GENERATED:")
        print("=" * 80)
        print(refined_prompt)
        print("=" * 80)
        print(f"✅ Refined prompt length: {len(refined_prompt)} characters (~{len(refined_prompt.split())} words)")
        print("=" * 80)
        
        return refined_prompt
        
    except Exception as e:
        print(f"❌ Error refining prompt: {str(e)}")
        import traceback
        print(f"📋 Full traceback: {traceback.format_exc()}")
        # Return combined prompt as fallback
        return f"{base_prompt}. Additional details: {additional_details}"


class PromptGenerationView(APIView):
    """Generate three prompt variations using OpenAI"""
    
    def post(self, request):
        """Generate three different prompt variations based on user input"""
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
            try:
                has_csv_feedback = len(request.FILES.getlist('csv_feedback')) > 0
            except Exception:
                has_csv_feedback = any(key == 'csv_feedback' for key in request.FILES.keys()) or any(key.startswith('csv_feedback_') for key in request.FILES.keys())
            
            if has_csv_feedback:
                return Response(
                    ResponseInfo.error("Prompt generation is only available when no CSV files are provided"),
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Process CSV feedback files if provided (safety path)
            feedback_data = []
            print(f"🔍 Checking for CSV files in request.FILES: {list(request.FILES.keys())}")
            csv_files = []
            try:
                csv_files = list(request.FILES.getlist('csv_feedback'))
            except Exception:
                csv_files = []
            for key, file in request.FILES.items():
                if key.startswith('csv_feedback_'):
                    csv_files.append(file)
            for file in csv_files:
                print(f"📁 Found CSV file: {file.name}, Content-Type: {file.content_type}, Size: {file.size}")
                if (file.content_type == 'text/csv' or file.content_type == 'text/plain' or file.name.endswith('.csv') or file.name.endswith('.txt')):
                    try:
                        data = process_csv_feedback(file)
                        feedback_data.extend(data)
                    except Exception as e:
                        print(f"❌ Error processing CSV feedback: {str(e)}")
                        continue
                else:
                    print(f"⚠️ CSV file found but wrong content type: {file.content_type}")
            
            # Generate three prompt variations using OpenAI
            print("🤖 Generating three prompt variations with OpenAI...")
            prompt_variations = generate_three_prompts_with_openai(prompt, feedback_data)
            
            response_data = {
                "original_prompt": prompt,
                "prompt_variations": prompt_variations,
                "feedback_used": len(feedback_data) > 0,
                "feedback_entries": len(feedback_data)
            }
            
            return Response(
                ResponseInfo.success(response_data, "Three prompt variations generated successfully"),
                status=status.HTTP_200_OK
            )
            
        except Exception as e:
            print(f"❌ Error generating prompt variations: {str(e)}")
            return Response(
                ResponseInfo.error(f"Failed to generate prompt variations: {str(e)}"),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class RefinePromptView(APIView):
    """Refine a selected prompt with additional details"""
    
    def post(self, request):
        """Refine a prompt by incorporating additional user details"""
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
            
            # Refine the prompt using OpenAI
            print(f"🔧 Refining prompt with additional details...")
            print(f"📝 Base prompt: {base_prompt[:100]}...")
            print(f"➕ Additional details: {additional_details}")
            
            refined_prompt = refine_prompt_with_openai(base_prompt, additional_details)
            
            response_data = {
                "base_prompt": base_prompt,
                "additional_details": additional_details,
                "refined_prompt": refined_prompt
            }
            
            return Response(
                ResponseInfo.success(response_data, "Prompt refined successfully"),
                status=status.HTTP_200_OK
            )
            
        except Exception as e:
            print(f"❌ Error refining prompt: {str(e)}")
            import traceback
            print(f"📋 Full traceback: {traceback.format_exc()}")
            return Response(
                ResponseInfo.error(f"Failed to refine prompt: {str(e)}"),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


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
            
            # Process CSV feedback files if provided
            feedback_data = []
            print(f"🔍 Checking for CSV files in request.FILES: {list(request.FILES.keys())}")
            csv_files = []
            try:
                csv_files = list(request.FILES.getlist('csv_feedback'))
            except Exception:
                csv_files = []
            for key, file in request.FILES.items():
                if key.startswith('csv_feedback_'):
                    csv_files.append(file)
            if csv_files:
                print(f"✅ {len(csv_files)} CSV file(s) detected for image generation")
                for file in csv_files:
                    print(f"📁 CSV: {file.name} ({file.content_type}, {file.size} bytes)")
                    if (file.content_type == 'text/csv' or file.content_type == 'text/plain' or file.name.endswith('.csv') or file.name.endswith('.txt')):
                        try:
                            data = process_csv_feedback(file)
                            feedback_data.extend(data)
                        except Exception as e:
                            print(f"❌ Error processing CSV feedback: {str(e)}")
                            continue
                    else:
                        print(f"⚠️ CSV file found but wrong content type: {file.content_type}")
            
            # Generate enhanced prompt using OpenAI if CSV feedback is provided
            final_prompt = prompt  # Default to user's original prompt
            if feedback_data:
                print("🤖 CSV feedback detected - Generating enhanced prompt with OpenAI...")
                print(f"📈 Feedback entries: {len(feedback_data)} (merged across files)")
                final_prompt = generate_enhanced_prompt_with_openai(prompt, feedback_data)
                print(f"🎯 FINAL ENHANCED PROMPT FOR IMAGE GENERATION: {final_prompt}")
            elif reference_images:
                # Reference images provided - use original prompt as-is
                print("🖼️  Reference images detected - Using original user prompt")
                print(f"📸 Number of reference images: {len(reference_images)}")
                final_prompt = prompt
                print(f"🎯 FINAL PROMPT FOR IMAGE GENERATION: {final_prompt}")
            else:
                # No reference images and no CSV - use original prompt as-is
                print("📝 No reference images or CSV feedback - Using original user prompt")
                final_prompt = prompt
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

