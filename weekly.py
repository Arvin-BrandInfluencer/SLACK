# ================================================
# FILE: weekly.py (FINAL VERSION)
# ================================================
import os
import sys
import json
from dotenv import load_dotenv
import requests
import google.generativeai as genai
from loguru import logger

# --- 1. CONFIGURATION & INITIALIZATION ---
logger.remove()
logger.add(sys.stderr, format="<yellow>{time:YYYY-MM-DD HH:mm:ss}</yellow> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>", colorize=True)
load_dotenv()
try:
    GOOGLE_API_KEY = os.environ["GOOGLE_API_KEY"]
    genai.configure(api_key=GOOGLE_API_KEY)
    gemini_model = genai.GenerativeModel('gemini-1.5-flash-latest')
    logger.success("Gemini client initialized for weekly.py.")
except KeyError as e:
    logger.critical(f"FATAL: Missing GOOGLE_API_KEY. Please check .env file.")
    sys.exit(1)

# --- CONSTANTS AND HELPERS ---
MARKET_CURRENCY_CONFIG = { 'SWEDEN': {'rate': 11.30, 'symbol': 'SEK', 'name': 'SEK'}, 'NORWAY': {'rate': 11.50, 'symbol': 'NOK', 'name': 'NOK'}, 'DENMARK': {'rate': 7.46, 'symbol': 'DKK', 'name': 'DKK'}, 'UK': {'rate': 0.85, 'symbol': '£', 'name': 'GBP'}, 'FRANCE': {'rate': 1.0, 'symbol': '€', 'name': 'EUR'}, }
BASE_API_URL = os.getenv("BASE_API_URL", "http://127.0.0.1:10000")
UNIFIED_API_URL = f"{BASE_API_URL}/api/influencer/query"
def get_currency_info(market): return MARKET_CURRENCY_CONFIG.get(str(market).upper(), {'rate': 1.0, 'symbol': '€', 'name': 'EUR'})

def format_currency(amount, market):
    currency_info = get_currency_info(market)
    symbol = currency_info['symbol']
    try:
        safe_amount = float(amount or 0.0)
        if currency_info['name'] in ['SEK', 'NOK', 'DKK']: return f"{safe_amount:,.0f} {symbol}"
        else: return f"{symbol}{safe_amount:,.2f}"
    except (ValueError, TypeError): return f"{symbol}0.00"

def split_message_for_slack(message: str, max_length: int = 2800) -> list:
    if not message: return []
    if len(message) <= max_length: return [message]
    chunks, current_chunk = [], ""
    for line in message.split('\n'):
        if len(current_chunk) + len(line) + 1 > max_length:
            if current_chunk.strip(): chunks.append(current_chunk)
            current_chunk = line + "\n"
        else:
            current_chunk += line + "\n"
    if current_chunk.strip(): chunks.append(current_chunk)
    return chunks

def query_api(url: str, payload: dict, endpoint_name: str) -> dict:
    logger.info(f"Querying {endpoint_name} API at {url} with payload: {payload}")
    try:
        response = requests.post(url, json=payload, timeout=60)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"{endpoint_name} API Connection Error: {e}")
        return {"error": f"Could not connect to the {endpoint_name} API."}

def create_prompt(user_query, market, start_date, end_date, api_data):
    return f"""
    You are Nova, a marketing analyst.
    Generate a concise performance review for the specified date range.
    **Data Context for {market.upper()} from {start_date} to {end_date}:**
    {json.dumps(api_data, indent=2)}
    
    **User's Request:** "{user_query}"
    
    **Instructions:**
    1.  Analyze the provided data which includes a summary and a detailed list of campaigns.
    2.  Provide a clear, well-structured performance summary. Use bold for key metrics like **Total Spend**, **Total Conversions**, and **Average CAC**.
    3.  Identify the **top-performing influencer** from the 'details' list based on their total conversions or efficiency (low CAC). Highlight their contribution.
    4.  If the data is empty or shows no activity, state that clearly.
    5.  Present insights naturally without mentioning "based on the data provided".
    """

# --- CORE LOGIC FUNCTION ---
def run_weekly_review(say, thread_ts, params, thread_context_store, user_query=None):
    try:
        # --- MODIFIED: More explicit check for required parameters ---
        market = params['market']
        start_date = params['start_date']
        end_date = params['end_date']
        year = params.get('year', 2025)
    except KeyError as e:
        say(f"A required parameter ({e}) was missing for the weekly review.", thread_ts=thread_ts); return

    payload = {
        "source": "influencer_analytics",
        "view": "custom_range_breakdown",
        "filters": {
            "market": market,
            "year": year,
            "date_from": start_date,
            "date_to": end_date
        }
    }
    api_data = query_api(UNIFIED_API_URL, payload, "Weekly Breakdown")
    if "error" in api_data:
        say(f"API Error: `{api_data['error']}`", thread_ts=thread_ts); return
    
    if not api_data.get("summary") or not api_data.get("details"):
        say(f"No performance data found for {market.upper()} between {start_date} and {end_date}.", thread_ts=thread_ts); return

    try:
        prompt = create_prompt(user_query, market, start_date, end_date, api_data)
        response = gemini_model.generate_content(prompt)
        ai_answer = response.text
        
        thread_context_store[thread_ts] = {
            'type': 'weekly_review', 
            'params': params,
            'raw_api_data': api_data, 
            'bot_response': ai_answer
        }
        
        for chunk in split_message_for_slack(ai_answer): say(text=chunk, thread_ts=thread_ts)
    except Exception as e:
        logger.error(f"Error during AI weekly review generation: {e}"); say(f"An error occurred generating the AI summary: {str(e)}", thread_ts=thread_ts)
    logger.success(f"Weekly review completed for {market} from {start_date} to {end_date}")

# --- THREAD FOLLOW-UP HANDLER ---
def handle_thread_messages(event, say, client, context):
    user_message = event.get("text", "").strip()
    thread_ts = event["thread_ts"]
    logger.info(f"Handling follow-up for weekly_review in thread {thread_ts}")
    try:
        params = context['params']
        context_prompt = f"""
        You are a helpful marketing analyst assistant.
        **Current Context:** A performance review for **{params['market']}** for the period **{params['start_date']} to {params['end_date']}**.
        **Available Data:** You have the full JSON data for this specific review: {json.dumps(context.get('raw_api_data', {}))}
        
        **User's Follow-up:** "{user_message}"
        
        **Instructions:**
        1. Answer the user's question **ONLY** using the data provided in the "Available Data" section.
        2. If the user asks about a different time period, market, or requires a comparison to data not present, you MUST state that you don't have that data in your current context. Example: "I can't answer that, as my current context is only for the review of {params['start_date']} to {params['end_date']}. To compare with another period, you would need to ask me to run a new report."
        3. Present your answer naturally.
        """
        response = gemini_model.generate_content(context_prompt)
        ai_response = response.text
        for chunk in split_message_for_slack(ai_response): say(text=chunk, thread_ts=thread_ts)
    except Exception as e:
        logger.error(f"Error handling thread message in weekly.py: {e}"); say(text="Sorry, I encountered an error.", thread_ts=thread_ts)
