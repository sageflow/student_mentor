from flask import Flask, jsonify
from flask_cors import CORS
import requests
import os
import json
import re
from datetime import date
from typing import Optional, Dict, List, Any
from concurrent.futures import ThreadPoolExecutor
import threading

app = Flask(__name__)
CORS(app)  # Enable CORS for API access

# Configuration - can be set via environment variable or defaults
BASE_URL = os.getenv('API_BASE_URL', 'http://localhost:8080')
DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY', '')
DEEPSEEK_API_URL = os.getenv('DEEPSEEK_API_URL', 'https://api.deepseek.com/v1/chat/completions')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', '')

# Thread pool for async background tasks
executor = ThreadPoolExecutor(max_workers=4)

# JWT token cache
_jwt_token = None
_token_lock = threading.Lock()


def get_jwt_token() -> Optional[str]:
    """
    Get JWT token by authenticating with the Spring Boot backend.
    Caches the token for reuse.
    
    POST /auth/login
    Body: { "username": "admin", "password": "<ADMIN_PASSWORD>" }
    """
    global _jwt_token
    
    # Return cached token if available
    with _token_lock:
        if _jwt_token:
            return _jwt_token
    
    if not ADMIN_PASSWORD:
        print("Warning: ADMIN_PASSWORD environment variable not set. Authentication will fail.")
        return None
    
    try:
        url = f"{BASE_URL}/auth/login"
        payload = {
            "username": "admin",
            "password": ADMIN_PASSWORD
        }
        
        response = requests.post(
            url,
            json=payload,
            headers={'Content-Type': 'application/json'},
            timeout=10
        )
        
        if response.status_code == 200:
            data = response.json()
            # Token might be in 'token', 'accessToken', 'jwt', or directly in response
            token = data.get('token') or data.get('accessToken') or data.get('jwt') or data.get('access_token')
            
            if token:
                with _token_lock:
                    _jwt_token = token
                print("[Auth] Successfully obtained JWT token")
                return token
            else:
                print(f"Warning: Login successful but no token found in response: {data}")
                return None
        else:
            print(f"Warning: Login failed. Status: {response.status_code}, Response: {response.text}")
            return None
            
    except requests.exceptions.RequestException as e:
        print(f"Warning: Error during login: {str(e)}")
        return None


def get_auth_headers() -> Dict[str, str]:
    """
    Get headers with JWT Bearer token for authenticated requests.
    """
    headers = {'Content-Type': 'application/json'}
    
    token = get_jwt_token()
    if token:
        headers['Authorization'] = f'Bearer {token}'
    
    return headers


def clear_jwt_token():
    """
    Clear cached JWT token (call this if token expires).
    """
    global _jwt_token
    with _token_lock:
        _jwt_token = None
    print("[Auth] JWT token cleared")


def get_student_info(student_id):
    """
    Fetch student information from the API.
    
    Returns StudentInfoDTO containing:
    - studentId: Long
    - habitsSummary: HabitsSummaryResponse (summary statistics for last 30 days)
    - physicalProfile: PhysicalProfileResponse (physical profile with height, weight, medical conditions, etc.)
    - interests: StudentInterestDTO (hobbies, professions, accolades)
    - iqScore: BigDecimal (nullable)
    - eqScore: BigDecimal (nullable)
    - oceanScore: OceanScore (Big5 personality test with detailed facets)
    - unresolvedComplaints: List[ComplaintResponse] (SUBMITTED or IN_PROGRESS status)
    - currentWeekPulse: WeeklyPulse (current week's pulse data)
    """
    try:
        url = f"{BASE_URL}/student/info/{student_id}"
        headers = get_auth_headers()
        response = requests.get(url, headers=headers, timeout=10)
        
        # If unauthorized, clear token and retry once
        if response.status_code == 401 or response.status_code == 403:
            clear_jwt_token()
            headers = get_auth_headers()
            response = requests.get(url, headers=headers, timeout=10)
        
        if response.status_code == 200:
            return response.json(), None, 200
        elif response.status_code == 400:
            return None, "Invalid student ID", 400
        elif response.status_code == 404:
            return None, "Student not found", 404
        elif response.status_code == 401 or response.status_code == 403:
            return None, "Authentication failed. Check ADMIN_PASSWORD environment variable.", response.status_code
        else:
            return None, f"Error: {response.status_code} - {response.text}", response.status_code
    except requests.exceptions.RequestException as e:
        return None, f"Connection error: {str(e)}", 500


def score_sleep_quality(quality: Optional[float]) -> float:
    """Score sleep quality (0-2). Higher quality = lower stress = lower score."""
    if quality is None:
        return 1.0  # Default middle score if missing
    
    # Assuming quality is 0-10 scale
    # Clamp to valid range
    quality = max(0.0, min(10.0, quality))
    
    # Lower quality = higher stress = higher score
    if quality >= 8:
        return 0.0  # Excellent sleep
    elif quality >= 6:
        return 1.0  # Good sleep
    else:
        return 2.0  # Poor sleep


def score_sleep_hours(hours: Optional[float]) -> float:
    """Score sleep hours (0-2). Optimal 7-9 hours = lower stress."""
    if hours is None:
        return 1.0
    
    if 7 <= hours <= 9:
        return 0.0  # Optimal
    elif 6 <= hours < 7 or 9 < hours <= 10:
        return 1.0  # Acceptable
    else:
        return 2.0  # Poor


def score_bedtime(bedtime: Optional[float]) -> float:
    """Score bedtime (0-2). Earlier bedtime (22-23) = lower stress."""
    if bedtime is None:
        return 1.0
    
    # Assuming bedtime is in 24-hour format (0-23)
    # Normalize to 0-23 range using modulo (handles both negative and >= 24)
    bedtime = bedtime % 24
    
    if 22 <= bedtime <= 23:
        return 0.0  # Optimal
    elif 21 <= bedtime < 22 or 0 <= bedtime < 1:
        return 1.0  # Acceptable
    else:
        return 2.0  # Late bedtime


def score_wake_time(wake_time: Optional[float]) -> float:
    """Score wake time (0-2). Consistent wake time = lower stress."""
    if wake_time is None:
        return 1.0
    
    # Assuming wake time is in 24-hour format (0-23)
    # Normalize to 0-23 range using modulo (handles both negative and >= 24)
    wake_time = wake_time % 24
    
    if 6 <= wake_time <= 7:
        return 0.0  # Optimal
    elif 5 <= wake_time < 6 or 7 < wake_time <= 8:
        return 1.0  # Acceptable
    else:
        return 2.0  # Irregular


def score_water_intake(liters: Optional[float]) -> float:
    """Score water intake (0-2). Optimal 2-3L = lower stress."""
    if liters is None:
        return 1.0
    
    if 2 <= liters <= 3:
        return 0.0  # Optimal
    elif 1.5 <= liters < 2 or 3 < liters <= 4:
        return 1.0  # Acceptable
    else:
        return 2.0  # Poor


def score_junk_food_frequency(frequency: Optional[float]) -> float:
    """Score junk food frequency (0-2). Lower frequency = lower stress."""
    if frequency is None:
        return 1.0
    
    # Assuming frequency is times per week
    if frequency <= 1:
        return 0.0  # Excellent
    elif frequency <= 3:
        return 1.0  # Acceptable
    else:
        return 2.0  # High frequency


def score_meals_consumed(meals: Optional[float]) -> float:
    """Score meals consumed (0-2). Optimal 3 meals = lower stress."""
    if meals is None:
        return 1.0
    
    if 2.5 <= meals <= 3.5:
        return 0.0  # Optimal
    elif 2 <= meals < 2.5 or 3.5 < meals <= 4:
        return 1.0  # Acceptable
    else:
        return 2.0  # Irregular


def score_exercise_hours(hours: Optional[float]) -> float:
    """Score exercise hours (0-2). Optimal 1-2 hours = lower stress."""
    if hours is None:
        return 1.0
    
    if 1 <= hours <= 2:
        return 0.0  # Optimal
    elif 0.5 <= hours < 1 or 2 < hours <= 3:
        return 1.0  # Acceptable
    else:
        return 2.0  # Too little or too much


def score_calories_burned(calories: Optional[int]) -> float:
    """Score calories burned (0-2). Higher calories = lower stress."""
    if calories is None:
        return 1.0
    
    # Ensure non-negative
    calories = max(0, calories)
    
    # Assuming monthly total
    monthly_target = 10000  # Approximate target for 30 days
    if calories >= monthly_target:
        return 0.0  # Excellent
    elif calories >= monthly_target * 0.7:
        return 1.0  # Good
    else:
        return 2.0  # Low


def score_exercise_type(exercise_type: Optional[str]) -> float:
    """Score exercise type (0-2). More active types = lower stress."""
    if exercise_type is None:
        return 1.0
    
    exercise_type_lower = exercise_type.lower()
    high_intensity = ['running', 'cycling', 'swimming', 'hiit', 'crossfit']
    moderate = ['walking', 'yoga', 'pilates', 'dancing']
    
    if any(term in exercise_type_lower for term in high_intensity):
        return 0.0  # High intensity
    elif any(term in exercise_type_lower for term in moderate):
        return 1.0  # Moderate
    else:
        return 2.0  # Low intensity or unknown


def score_screen_time_hours(hours: Optional[float]) -> float:
    """Score screen time (0-2). Lower hours = lower stress."""
    if hours is None:
        return 1.0
    
    if hours <= 2:
        return 0.0  # Excellent
    elif hours <= 4:
        return 1.0  # Acceptable
    else:
        return 2.0  # High screen time


def score_pre_sleep_screen_time(hours: Optional[float]) -> float:
    """Score pre-sleep screen time (0-2). Lower = lower stress."""
    if hours is None:
        return 1.0
    
    if hours <= 0.5:
        return 0.0  # Excellent
    elif hours <= 1:
        return 1.0  # Acceptable
    else:
        return 2.0  # High pre-sleep screen time


def score_media_duration(hours: Optional[float]) -> float:
    """Score media duration (0-2). Lower = lower stress."""
    if hours is None:
        return 1.0
    
    if hours <= 1:
        return 0.0  # Excellent
    elif hours <= 2:
        return 1.0  # Acceptable
    else:
        return 2.0  # High media consumption


def score_educational_content_count(count: Optional[int]) -> float:
    """Score educational content (0-2). Higher = lower stress."""
    if count is None:
        return 1.0
    
    if count >= 20:
        return 0.0  # Excellent
    elif count >= 10:
        return 1.0  # Good
    else:
        return 2.0  # Low educational content


def score_platform(platform: Optional[str]) -> float:
    """Score platform (0-2). Educational platforms = lower stress."""
    if platform is None:
        return 1.0
    
    platform_lower = platform.lower()
    educational = ['khan academy', 'coursera', 'edx', 'udemy', 'youtube education']
    neutral = ['youtube', 'netflix', 'spotify']
    
    if any(term in platform_lower for term in educational):
        return 0.0  # Educational
    elif any(term in platform_lower for term in neutral):
        return 1.0  # Neutral
    else:
        return 2.0  # Potentially problematic


def calculate_habits_stress_score(habits_summary: Optional[Dict[str, Any]]) -> float:
    """
    Calculate stress score from HabitsSummaryResponse (0-30).
    Each field scores 0-2, total of 15 fields.
    """
    if not habits_summary:
        return 15.0  # Default middle score if missing
    
    score = 0.0
    
    # Sleep summary (4 fields)
    score += score_sleep_quality(habits_summary.get('averageSleepQuality'))
    score += score_sleep_hours(habits_summary.get('averageSleepHours'))
    score += score_bedtime(habits_summary.get('averageBedtime'))
    score += score_wake_time(habits_summary.get('averageWakeTime'))
    
    # Diet summary (3 fields)
    score += score_water_intake(habits_summary.get('averageWaterIntake'))
    score += score_junk_food_frequency(habits_summary.get('averageJunkFoodFrequency'))
    score += score_meals_consumed(habits_summary.get('averageMealsConsumed'))
    
    # Exercise summary (3 fields)
    score += score_exercise_hours(habits_summary.get('averageExerciseHours'))
    score += score_calories_burned(habits_summary.get('totalCaloriesBurned'))
    score += score_exercise_type(habits_summary.get('mostCommonExerciseType'))
    
    # Screen time summary (2 fields)
    score += score_screen_time_hours(habits_summary.get('averageScreenTimeHours'))
    score += score_pre_sleep_screen_time(habits_summary.get('averagePreSleepScreenTime'))
    
    # Media consumption summary (3 fields)
    score += score_media_duration(habits_summary.get('averageMediaDuration'))
    score += score_educational_content_count(habits_summary.get('educationalContentCount'))
    score += score_platform(habits_summary.get('mostUsedPlatform'))
    
    return min(30.0, max(0.0, score))  # Ensure 0-30 range


def analyze_complaints_with_deepseek(complaints: List[Dict[str, Any]]) -> float:
    """
    Analyze complaints using Deepseek API and return stress score (0-30).
    """
    if not complaints or len(complaints) == 0:
        return 0.0  # No complaints = no stress from complaints
    
    if not DEEPSEEK_API_KEY:
        # Fallback: simple heuristic if API key not configured
        return min(30.0, len(complaints) * 5.0)  # 5 points per complaint, max 30
    
    try:
        # Combine all complaint descriptions
        complaint_texts = []
        for complaint in complaints:
            desc = complaint.get('description', '')
            if desc:
                complaint_texts.append(desc)
        
        if not complaint_texts:
            return 0.0
        
        combined_text = "\n\n".join(complaint_texts)
        
        # Prepare prompt for Deepseek
        prompt = f"""Analyze the following student complaints and assess the stress level they indicate.
        
Complaints:
{combined_text}

Rate the stress level indicated by these complaints on a scale of 0-30, where:
- 0-10: Low stress (minor issues, easily resolvable)
- 11-20: Moderate stress (some concerns, manageable)
- 21-30: High stress (serious issues, significant concerns)

Respond with ONLY a number between 0 and 30, no other text."""
        
        # Call Deepseek API
        headers = {
            'Authorization': f'Bearer {DEEPSEEK_API_KEY}',
            'Content-Type': 'application/json'
        }
        
        payload = {
            'model': 'deepseek-chat',
            'messages': [
                {'role': 'user', 'content': prompt}
            ],
            'temperature': 0.3,
            'max_tokens': 10
        }
        
        response = requests.post(
            DEEPSEEK_API_URL,
            headers=headers,
            json=payload,
            timeout=10
        )
        
        if response.status_code == 200:
            result = response.json()
            content = result.get('choices', [{}])[0].get('message', {}).get('content', '').strip()
            
            # Extract number from response
            try:
                score = float(content)
                return min(30.0, max(0.0, score))
            except ValueError:
                # Fallback if parsing fails
                return min(30.0, len(complaints) * 5.0)
        else:
            # Fallback on API error
            return min(30.0, len(complaints) * 5.0)
            
    except Exception as e:
        # Fallback on any error
        print(f"Error calling Deepseek API: {e}")
        return min(30.0, len(complaints) * 5.0)


def calculate_pulse_stress_score(pulse: Optional[Dict[str, Any]]) -> float:
    """
    Calculate stress score from WeeklyPulse (6-30).
    Invert the rating (high pulse = low stress) and multiply by 6.
    """
    if not pulse or pulse.get('rating') is None:
        return 18.0  # Default middle score if missing
    
    rating = pulse.get('rating')
    
    # Validate rating is positive
    if rating <= 0:
        return 18.0  # Default middle score for invalid rating
    
    # Assuming rating is on a scale (e.g., 1-5 or 1-10)
    # We need to invert it: high rating = low stress = low score
    # If rating is 1-5 scale: inverted = (6 - rating) * 6 = 30-6
    # If rating is 1-10 scale: inverted = (11 - rating) * 2.4 = 24-2.4, then add 6 = 30-8.4
    
    # Try to detect scale (assuming 1-5 is most common for pulse)
    # If rating >= 6, assume 1-10 scale, else assume 1-5 scale
    if rating >= 6:
        # 1-10 scale: map to 6-30 range
        # Rating 1 (best) -> score 30, Rating 10 (worst) -> score 6
        # Formula: 30 - (rating - 1) * (24/9) = 30 - (rating - 1) * 2.67
        score = 30 - (rating - 1) * (24 / 9)
    else:
        # 1-5 scale: map to 6-30 range
        # Rating 1 (best) -> score 30, Rating 5 (worst) -> score 6
        inverted_rating = 6 - rating
        score = inverted_rating * 6
    
    return min(30.0, max(6.0, score))  # Ensure 6-30 range


def calculate_stress_score(student_info: Dict[str, Any]) -> float:
    """
    Calculate overall stress score (0-90) from student information.
    
    Components:
    - HabitsSummaryResponse: 0-30
    - ComplaintResponse list (via Deepseek): 0-30
    - WeeklyPulse (inverted): 6-30
    
    Total: 0-90
    """
    habits_score = calculate_habits_stress_score(student_info.get('habitsSummary'))
    complaints_score = analyze_complaints_with_deepseek(student_info.get('unresolvedComplaints') or [])
    pulse_score = calculate_pulse_stress_score(student_info.get('currentWeekPulse'))
    
    total_score = habits_score + complaints_score + pulse_score
    
    return min(90.0, max(0.0, total_score))


def stress_score_to_percentage(stress_score: float) -> int:
    """
    Convert stress score (0-90) to percentage (0-100).
    """
    percentage = int((stress_score / 90.0) * 100)
    return min(100, max(0, percentage))


def get_stress_color(stress_score: float) -> str:
    """
    Determine stress color based on stress score (0-90).
    
    Color mapping:
    - Green (0-30): Low stress
    - Yellow (31-60): Moderate stress
    - Orange (61-75): High stress
    - Red (76-90): Very high stress
    """
    if stress_score <= 30:
        return "GREEN"
    elif stress_score <= 60:
        return "YELLOW"
    elif stress_score <= 75:
        return "ORANGE"
    else:
        return "RED"


def generate_personalized_habits(student_info: Dict[str, Any]) -> List[str]:
    """
    Generate 2-3 personalized daily habits for a student using Deepseek API.
    
    Uses the following student data:
    - PhysicalProfileResponse: Physical profile (height, weight, medical conditions, etc.)
    - StudentInterestDTO: Hobbies, professions, accolades
    - iqScore: IQ test score
    - eqScore: EQ test score
    - OceanScore: Big5 personality traits
    
    Returns a list of guidance strings (descriptions only).
    """
    if not DEEPSEEK_API_KEY:
        # Fallback: return semi-personalized habits based on available data (for testing without API key)
        habits = []
        
        # Handle None values safely using 'or {}'
        interests = student_info.get('interests') or {}
        hobbies = interests.get('hobbies') or []
        
        # Add hobby-based habit if available
        if hobbies and len(hobbies) > 0:
            hobby = hobbies[0] if isinstance(hobbies[0], str) else str(hobbies[0])
            habits.append(f"Dedicate 30 minutes daily to practice {hobby.lower().replace('_', ' ')} to develop your skills and passion")
        else:
            habits.append("Engage in at least 30 minutes of physical activity daily to boost energy and mood")
        
        # Add habit based on habits summary (handle None safely)
        habits_summary = student_info.get('habitsSummary') or {}
        screen_time = habits_summary.get('averageScreenTimeHours')
        water_intake = habits_summary.get('averageWaterIntake')
        
        if screen_time is not None and screen_time > 4:
            habits.append("Take a 10-minute break every hour from screens to rest your eyes and stretch")
        else:
            habits.append("Practice 10 minutes of mindfulness meditation before starting your day")
        
        if water_intake is not None and water_intake < 2:
            habits.append("Drink at least 8 glasses of water throughout the day, keeping a water bottle nearby")
        else:
            habits.append("Read for 20 minutes before bed instead of using electronic devices")
        
        return habits[:3]  # Return max 3 habits
    
    try:
        # Extract relevant data (handle None values safely)
        physical_profile = student_info.get('physicalProfile') or {}
        interests = student_info.get('interests') or {}
        iq_score = student_info.get('iqScore')
        eq_score = student_info.get('eqScore')
        ocean_score = student_info.get('oceanScore') or {}
        
        # Build comprehensive prompt
        prompt_parts = []
        
        prompt_parts.append("""You are a wellness coach helping to create personalized daily habits for a student. 
Generate 2-3 specific, actionable daily habits that are tailored to this student's profile.

IMPORTANT: Respond ONLY with a JSON array of habit descriptions (strings only).
Each description should be a clear, actionable statement (max 200 characters).

Format your response as valid JSON only, no additional text or explanation.

Student Profile:
""")
        
        # Add physical profile
        if physical_profile:
            prompt_parts.append("\nPhysical Profile:\n")
            height_feet = physical_profile.get('heightFeet')
            height_inches = physical_profile.get('heightInches')
            if height_feet is not None and height_inches is not None:
                # Convert to cm for better context (1 foot = 30.48 cm, 1 inch = 2.54 cm)
                height_cm = (height_feet * 30.48) + (height_inches * 2.54)
                prompt_parts.append(f"- Height: {height_feet}'{height_inches}\" ({height_cm:.1f} cm)\n")
            elif height_feet is not None:
                height_cm = height_feet * 30.48
                prompt_parts.append(f"- Height: {height_feet}' ({height_cm:.1f} cm)\n")
            
            body_weight = physical_profile.get('bodyWeightKg')
            if body_weight is not None:
                prompt_parts.append(f"- Weight: {body_weight} kg\n")
            
            if physical_profile.get('textToSpeechNeeded'):
                prompt_parts.append("- Text-to-Speech Support: Needed\n")
            if physical_profile.get('motorSupportNeeded'):
                prompt_parts.append("- Motor Support: Needed\n")
            
            medical_condition = physical_profile.get('medicalCondition')
            if medical_condition:
                prompt_parts.append(f"- Medical Condition: {medical_condition}\n")
            medical_notes = physical_profile.get('medicalConditionNotes')
            if medical_notes:
                prompt_parts.append(f"- Medical Notes: {medical_notes}\n")
        
        # Add interests
        if interests:
            prompt_parts.append("\nInterests:\n")
            if interests.get('hobbies'):
                prompt_parts.append(f"- Hobbies: {', '.join(interests.get('hobbies', []))}\n")
            if interests.get('professions'):
                prompt_parts.append(f"- Career Interests: {', '.join(interests.get('professions', []))}\n")
            if interests.get('accolades'):
                prompt_parts.append(f"- Achievements: {', '.join(interests.get('accolades', []))}\n")
        
        # Add IQ and EQ scores
        if iq_score is not None:
            prompt_parts.append(f"\nIQ Score: {iq_score}\n")
        if eq_score is not None:
            prompt_parts.append(f"EQ Score: {eq_score}\n")
        
        # Add OCEAN personality traits (key traits only)
        if ocean_score:
            prompt_parts.append("\nPersonality Traits (OCEAN Big5):\n")
            # Extract key traits
            key_traits = []
            if ocean_score.get('openness') is not None or ocean_score.get('imagination') is not None:
                imagination = ocean_score.get('imagination')
                artistic = ocean_score.get('artisticInterests')
                intellect = ocean_score.get('intellect')
                # Only calculate if at least one value is not None
                if any(v is not None for v in [imagination, artistic, intellect]):
                    values = [v for v in [imagination, artistic, intellect] if v is not None]
                    openness_avg = sum(values) / len(values)
                    key_traits.append(f"Openness: {openness_avg:.1f}/100")
            
            if ocean_score.get('conscientiousness') is not None or ocean_score.get('selfEfficacy') is not None:
                self_eff = ocean_score.get('selfEfficacy')
                order = ocean_score.get('orderliness')
                achieve = ocean_score.get('achievementStriving')
                # Only calculate if at least one value is not None
                if any(v is not None for v in [self_eff, order, achieve]):
                    values = [v for v in [self_eff, order, achieve] if v is not None]
                    consc_avg = sum(values) / len(values)
                    key_traits.append(f"Conscientiousness: {consc_avg:.1f}/100")
            
            if ocean_score.get('extraversion') is not None or ocean_score.get('friendliness') is not None:
                friend = ocean_score.get('friendliness')
                activity = ocean_score.get('activityLevel')
                cheer = ocean_score.get('cheerfulness')
                # Only calculate if at least one value is not None
                if any(v is not None for v in [friend, activity, cheer]):
                    values = [v for v in [friend, activity, cheer] if v is not None]
                    extra_avg = sum(values) / len(values)
                    key_traits.append(f"Extraversion: {extra_avg:.1f}/100")
            
            if ocean_score.get('neuroticism') is not None or ocean_score.get('anxiety') is not None:
                anxiety = ocean_score.get('anxiety')
                depression = ocean_score.get('depression')
                vulnerability = ocean_score.get('vulnerability')
                # Only calculate if at least one value is not None
                if any(v is not None for v in [anxiety, depression, vulnerability]):
                    values = [v for v in [anxiety, depression, vulnerability] if v is not None]
                    neuro_avg = sum(values) / len(values)
                    key_traits.append(f"Neuroticism: {neuro_avg:.1f}/100")
            
            if key_traits:
                prompt_parts.append("- " + ", ".join(key_traits) + "\n")
        
        prompt_parts.append("""
\nBased on this profile, generate 2-3 personalized daily habits that:
1. Align with the student's interests, physical profile, and personality
2. Are specific, measurable, and achievable
3. Promote overall wellbeing and stress reduction
4. Consider physical attributes when relevant (e.g., if student has basketball as hobby and height is 190 cm, suggest "Practice 5 slam dunks")

Remember: Respond with ONLY a JSON array of strings, no other text.
Example format:
[
  "Start each day with 10 minutes of meditation focusing on breath",
  "Drink 8 glasses of water throughout the day, tracking intake"
]
""")
        
        prompt = "".join(prompt_parts)
        
        # Call Deepseek API
        headers = {
            'Authorization': f'Bearer {DEEPSEEK_API_KEY}',
            'Content-Type': 'application/json'
        }
        
        payload = {
            'model': 'deepseek-chat',
            'messages': [
                {'role': 'user', 'content': prompt}
            ],
            'temperature': 0.7,
            'max_tokens': 500
        }
        
        response = requests.post(
            DEEPSEEK_API_URL,
            headers=headers,
            json=payload,
            timeout=15
        )
        
        if response.status_code == 200:
            result = response.json()
            content = result.get('choices', [{}])[0].get('message', {}).get('content', '').strip()
            
            # Try to parse JSON from response
            # Sometimes the response might have markdown code blocks
            json_match = re.search(r'\[.*\]', content, re.DOTALL)
            if json_match:
                content = json_match.group(0)
            
            try:
                habits = json.loads(content)
                # Validate structure - expect list of strings
                if isinstance(habits, list) and len(habits) > 0:
                    validated_habits = []
                    for habit in habits[:3]:  # Limit to 3 habits
                        if isinstance(habit, str) and habit.strip():
                            validated_habits.append(str(habit)[:200])
                        elif isinstance(habit, dict) and 'description' in habit:
                            # Handle legacy format with title/description
                            validated_habits.append(str(habit['description'])[:200])
                    if validated_habits:
                        return validated_habits
            except json.JSONDecodeError:
                print(f"Warning: Failed to parse habits JSON. Response: {content}")
        
        # Fallback on error
        return [
            "Engage in at least 30 minutes of physical activity daily",
            "Practice 5 minutes of deep breathing exercises each morning"
        ]
        
    except Exception as e:
        print(f"Error generating habits with Deepseek: {e}")
        # Fallback habits
        return [
            "Engage in at least 30 minutes of physical activity daily",
            "Practice 5 minutes of deep breathing exercises each morning"
        ]


def save_guidances(student_id: int, guidances: List[str], guidance_date: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
    """
    Save guidances for a student for a specific date.
    
    POST /guidance/{studentId}
    Body: {
        "guidances": ["guidance1", "guidance2", "guidance3"],
        "date": "2024-01-15"
    }
    
    Returns list of GuidanceResponse objects if successful.
    """
    try:
        url = f"{BASE_URL}/guidance/{student_id}"
        
        # Use today's date if not provided
        if guidance_date is None:
            guidance_date = date.today().isoformat()
        
        payload = {
            "guidances": guidances,
            "date": guidance_date
        }
        
        headers = get_auth_headers()
        response = requests.post(
            url,
            json=payload,
            headers=headers,
            timeout=10
        )
        
        if response.status_code == 201:
            return response.json()
        else:
            print(f"Warning: Failed to save guidances. Status: {response.status_code}, Response: {response.text}")
            return None
            
    except requests.exceptions.RequestException as e:
        print(f"Warning: Error saving guidances: {str(e)}")
        return None


def generate_wellbeing_gist(student_info: Dict[str, Any]) -> str:
    """
    Generate a wellbeing gist paragraph using Deepseek API.
    
    Uses the following student data:
    - habitsSummary: HabitsSummaryResponse (summary statistics for last 30 days)
    - unresolvedComplaints: List[ComplaintResponse] (SUBMITTED or IN_PROGRESS status)
    - currentWeekPulse: WeeklyPulse (current week's pulse data)
    
    Returns a paragraph describing how the student is doing.
    """
    # Handle None values safely
    habits_summary = student_info.get('habitsSummary') or {}
    unresolved_complaints = student_info.get('unresolvedComplaints') or []
    current_week_pulse = student_info.get('currentWeekPulse') or {}
    
    if not DEEPSEEK_API_KEY:
        # Fallback: generate a basic wellbeing gist based on available data (for testing without API key)
        gist_parts = []
        
        # Analyze habits
        if habits_summary:
            sleep_hours = habits_summary.get('averageSleepHours')
            exercise_hours = habits_summary.get('averageExerciseHours')
            screen_time = habits_summary.get('averageScreenTimeHours')
            
            if sleep_hours:
                if sleep_hours >= 7:
                    gist_parts.append(f"You're getting a healthy {sleep_hours:.1f} hours of sleep on average, which is great for your wellbeing.")
                else:
                    gist_parts.append(f"Your average sleep of {sleep_hours:.1f} hours could be improved - aim for 7-9 hours for optimal health.")
            
            if exercise_hours:
                if exercise_hours >= 1:
                    gist_parts.append(f"Your exercise routine of {exercise_hours:.1f} hours daily shows good commitment to physical health.")
                else:
                    gist_parts.append("Consider adding more physical activity to your routine for better overall wellness.")
            
            if screen_time and screen_time > 4:
                gist_parts.append("Your screen time is on the higher side - taking regular breaks can help reduce eye strain and improve focus.")
        
        # Analyze complaints
        if unresolved_complaints and len(unresolved_complaints) > 0:
            gist_parts.append(f"You have {len(unresolved_complaints)} unresolved concern(s) being addressed. Remember, seeking help is a sign of strength.")
        else:
            gist_parts.append("It's positive that you don't have any pending concerns at the moment.")
        
        # Analyze pulse
        if current_week_pulse and current_week_pulse.get('rating'):
            rating = current_week_pulse.get('rating')
            if rating >= 4:
                gist_parts.append("Your recent mood rating suggests you're feeling good - keep up the positive momentum!")
            elif rating >= 2:
                gist_parts.append("Your recent mood has been moderate. Remember to take time for activities you enjoy.")
            else:
                gist_parts.append("Your recent mood rating indicates you might be going through a tough time. Consider reaching out to someone you trust.")
        
        if gist_parts:
            return " ".join(gist_parts)
        else:
            return "Based on the available data, you appear to be maintaining a balanced lifestyle. Keep focusing on healthy habits and don't hesitate to seek support when needed. Remember that small consistent efforts lead to big improvements in overall wellbeing."
    
    try:
        # Build comprehensive prompt
        prompt_parts = []
        
        prompt_parts.append("""You are a student wellness advisor. Based on the following student data, write a brief, empathetic paragraph (3-5 sentences) summarizing how the student is doing overall. Be supportive and constructive.

Student Data:
""")
        
        # Add habits summary
        if habits_summary:
            prompt_parts.append("\nHabits Summary (Last 30 days):\n")
            if habits_summary.get('averageSleepQuality') is not None:
                prompt_parts.append(f"- Sleep Quality: {habits_summary.get('averageSleepQuality')}/10\n")
            if habits_summary.get('averageSleepHours') is not None:
                prompt_parts.append(f"- Average Sleep Hours: {habits_summary.get('averageSleepHours')} hours\n")
            if habits_summary.get('averageBedtime') is not None:
                prompt_parts.append(f"- Average Bedtime: {habits_summary.get('averageBedtime')}\n")
            if habits_summary.get('averageWakeTime') is not None:
                prompt_parts.append(f"- Average Wake Time: {habits_summary.get('averageWakeTime')}\n")
            if habits_summary.get('averageWaterIntake') is not None:
                prompt_parts.append(f"- Average Water Intake: {habits_summary.get('averageWaterIntake')}L/day\n")
            if habits_summary.get('averageJunkFoodFrequency') is not None:
                prompt_parts.append(f"- Junk Food Frequency: {habits_summary.get('averageJunkFoodFrequency')} times/week\n")
            if habits_summary.get('averageMealsConsumed') is not None:
                prompt_parts.append(f"- Average Meals: {habits_summary.get('averageMealsConsumed')}/day\n")
            if habits_summary.get('averageExerciseHours') is not None:
                prompt_parts.append(f"- Average Exercise: {habits_summary.get('averageExerciseHours')} hours/day\n")
            if habits_summary.get('mostCommonExerciseType'):
                prompt_parts.append(f"- Preferred Exercise: {habits_summary.get('mostCommonExerciseType')}\n")
            if habits_summary.get('averageScreenTimeHours') is not None:
                prompt_parts.append(f"- Average Screen Time: {habits_summary.get('averageScreenTimeHours')} hours/day\n")
            if habits_summary.get('averagePreSleepScreenTime') is not None:
                prompt_parts.append(f"- Pre-Sleep Screen Time: {habits_summary.get('averagePreSleepScreenTime')} hours\n")
            if habits_summary.get('educationalContentCount') is not None:
                prompt_parts.append(f"- Educational Content Consumed: {habits_summary.get('educationalContentCount')} items\n")
        else:
            prompt_parts.append("\nNo habits data available.\n")
        
        # Add unresolved complaints
        if unresolved_complaints and len(unresolved_complaints) > 0:
            prompt_parts.append(f"\nUnresolved Complaints ({len(unresolved_complaints)} total):\n")
            for i, complaint in enumerate(unresolved_complaints[:5], 1):  # Limit to first 5
                desc = complaint.get('description', 'No description')
                status = complaint.get('status', 'Unknown')
                prompt_parts.append(f"- Complaint {i}: {desc} (Status: {status})\n")
        else:
            prompt_parts.append("\nNo unresolved complaints.\n")
        
        # Add current week pulse
        if current_week_pulse:
            prompt_parts.append("\nCurrent Week Pulse:\n")
            if current_week_pulse.get('rating') is not None:
                prompt_parts.append(f"- Overall Rating: {current_week_pulse.get('rating')}\n")
            if current_week_pulse.get('feedback'):
                prompt_parts.append(f"- Feedback: {current_week_pulse.get('feedback')}\n")
        else:
            prompt_parts.append("\nNo pulse data available for this week.\n")
        
        prompt_parts.append("""
Write a supportive, personalized paragraph (3-5 sentences) about how this student is doing. Focus on:
1. Overall wellbeing based on habits
2. Any concerns from complaints
3. Recent mood/pulse data
4. Encouragement and constructive observations

Respond with ONLY the paragraph, no additional formatting or labels.""")
        
        prompt = "".join(prompt_parts)
        
        # Call Deepseek API
        headers = {
            'Authorization': f'Bearer {DEEPSEEK_API_KEY}',
            'Content-Type': 'application/json'
        }
        
        payload = {
            'model': 'deepseek-chat',
            'messages': [
                {'role': 'user', 'content': prompt}
            ],
            'temperature': 0.7,
            'max_tokens': 300
        }
        
        response = requests.post(
            DEEPSEEK_API_URL,
            headers=headers,
            json=payload,
            timeout=15
        )
        
        if response.status_code == 200:
            result = response.json()
            content = result.get('choices', [{}])[0].get('message', {}).get('content', '').strip()
            if content:
                return content
        
        # Fallback on error
        return "We're currently unable to generate a detailed wellbeing assessment. Please check back later."
        
    except Exception as e:
        print(f"Error generating wellbeing gist with Deepseek: {e}")
        return "We're currently unable to generate a detailed wellbeing assessment. Please check back later."


def save_wellbeing_data(student_id: int, stress_score: float, wellbeing_gist: str) -> Optional[Dict[str, Any]]:
    """
    Save predictive wellbeing data to the backend API.
    
    POST /wellbeing/{studentId}
    Body: {
        "stressPercentage": int (0-100),
        "stressColour": string,
        "wellbeingGist": string
    }
    """
    try:
        url = f"{BASE_URL}/wellbeing/{student_id}"
        
        stress_percentage = stress_score_to_percentage(stress_score)
        stress_colour = get_stress_color(stress_score)
        
        payload = {
            "stressPercentage": stress_percentage,
            "stressColour": stress_colour,
            "wellbeingGist": wellbeing_gist
        }
        
        headers = get_auth_headers()
        response = requests.post(
            url,
            json=payload,
            headers=headers,
            timeout=10
        )
        
        if response.status_code == 201:
            return response.json()
        else:
            print(f"Warning: Failed to save wellbeing data. Status: {response.status_code}, Response: {response.text}")
            return None
            
    except requests.exceptions.RequestException as e:
        print(f"Warning: Error saving wellbeing data: {str(e)}")
        return None


@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'api_base_url': BASE_URL,
        'auth_configured': bool(ADMIN_PASSWORD),
        'deepseek_configured': bool(DEEPSEEK_API_KEY)
    }), 200


def process_wellbeing_async(student_id: int, student_info: Dict[str, Any]):
    """
    Background task to calculate and submit wellbeing data.
    Runs asynchronously after returning student info to the user.
    """
    try:
        print(f"[Async] Starting wellbeing calculation for student {student_id}")
        
        # Calculate stress score
        stress_score = calculate_stress_score(student_info)
        
        # Generate wellbeing gist using Deepseek
        wellbeing_gist = generate_wellbeing_gist(student_info)
        
        # Submit wellbeing data to Spring Boot backend
        wellbeing_response = save_wellbeing_data(student_id, stress_score, wellbeing_gist)
        
        if wellbeing_response:
            print(f"[Async] Successfully saved wellbeing data for student {student_id}")
        else:
            print(f"[Async] Failed to save wellbeing data for student {student_id}")
            
    except Exception as e:
        print(f"[Async] Error processing wellbeing for student {student_id}: {str(e)}")


def process_guidance_async(student_id: int, student_info: Dict[str, Any]):
    """
    Background task to generate and submit personalized guidances.
    Runs asynchronously after returning student info to the user.
    """
    try:
        print(f"[Async] Starting guidance generation for student {student_id}")
        
        # Generate personalized guidances
        guidances = generate_personalized_habits(student_info)
        
        # Submit guidances to Spring Boot backend
        if guidances:
            guidance_response = save_guidances(student_id, guidances)
            
            if guidance_response:
                print(f"[Async] Successfully saved guidances for student {student_id}")
            else:
                print(f"[Async] Failed to save guidances for student {student_id}")
        else:
            print(f"[Async] No guidances generated for student {student_id}")
            
    except Exception as e:
        print(f"[Async] Error processing guidance for student {student_id}: {str(e)}")


@app.route('/student-mentor/<int:student_id>', methods=['GET'])
def process_student(student_id):
    """
    Main entry point for the Student Mentor AI application.
    
    Triggers async background tasks to:
    1. Calculate stress percentage & wellbeing gist, then submit to POST /wellbeing/{studentId}
    2. Generate personalized guidances, then submit to POST /guidance/{studentId}
    
    Returns nothing (void) - processing happens in the background.
    """
    # Step 1: Fetch student info from Spring Boot backend
    student_info, error, status_code = get_student_info(student_id)
    
    if error:
        return jsonify({
            'error': error
        }), status_code
    
    # Step 2: Trigger async background tasks for wellbeing and guidance processing
    # These run in parallel and don't block the response
    executor.submit(process_wellbeing_async, student_id, student_info)
    executor.submit(process_guidance_async, student_id, student_info)
    
    # Return 202 Accepted - processing will happen asynchronously
    return '', 202


if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    debug = os.getenv('FLASK_DEBUG', 'False').lower() == 'true'
    app.run(host='0.0.0.0', port=port, debug=debug)
