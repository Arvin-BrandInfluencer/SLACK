# ================================================
# FILE: month.py (FINAL - BUGS FIXED)
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
    logger.success("Gemini client initialized for month.py.")
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

def create_prompt(user_query, market, month, year, target_budget_local, actual_data, is_full_review):
    return f"""
    You are Nova, a marketing analyst.
    {"Generate a comprehensive monthly performance review." if is_full_review else "Provide a concise, direct answer to the user's question."}
    **Data Context for {market.upper()} - {month.upper()} {year}:**
    {json.dumps({"Target Budget": format_currency(target_budget_local, market), "Actuals": actual_data}, indent=2)}
    **User's Request:** "{user_query if user_query else "A full monthly review."}"
    **Instructions:** Analyze the request and data. Formulate a clear, well-structured response using bold for key metrics. If data is missing, state it clearly.Present insights naturally without mentioning "based on the data provided".
    """

# --- CORE LOGIC FUNCTION ---
def run_monthly_review(say, thread_ts, params, thread_context_store, user_query=None):
    try:
        market, month_abbr, month_full, year = params['market'], params['month_abbr'], params['month_full'], params['year']
    except KeyError as e:
        say(f"A required parameter was missing: {e}.", thread_ts=thread_ts); return

    target_payload = {"source": "dashboard", "filters": {"market": market, "year": year}}
    target_data = query_api(UNIFIED_API_URL, target_payload, "Dashboard (Targets)")
    if "error" in target_data:
        say(f"API Error: `{target_data['error']}`", thread_ts=thread_ts); return
    
    # CORRECTED: Made the month abbreviation comparison case-insensitive to fix the target budget lookup.
    target_budget_local = next((float(m.get("target_budget_clean", 0)) for m in target_data.get("monthly_detail", []) if str(m.get("month", "")).lower() == str(month_abbr).lower()), 0)
    
    actuals_payload = {"source": "influencer_analytics", "view": "monthly_breakdown", "filters": {"market": market, "month": month_full, "year": year}}
    actual_data_response = query_api(UNIFIED_API_URL, actuals_payload, "Influencer Analytics (Monthly)")
    if "error" in actual_data_response:
        say(f"API Error: `{actual_data_response['error']}`", thread_ts=thread_ts); return

    if not actual_data_response.get("monthly_data"):
        say(f"No performance data found for {market.upper()} {month_full} {year}.", thread_ts=thread_ts); return
    actual_data = actual_data_response["monthly_data"][0]

    try:
        is_full_review = not user_query or any(kw in user_query.lower() for kw in ["review", "summary", "analysis"])
        prompt = create_prompt(user_query, market, month_full, year, target_budget_local, actual_data, is_full_review)
        
        response = gemini_model.generate_content(prompt)
        ai_answer = response.text
        
        thread_context_store[thread_ts] = {
            'type': 'monthly_review', 'params': params,
            'raw_target_data': target_data, 'raw_actual_data': actual_data_response, 'bot_response': ai_answer
        }
        
        for chunk in split_message_for_slack(ai_answer): say(text=chunk, thread_ts=thread_ts)
    except Exception as e:
        logger.error(f"Error during AI review generation: {e}"); say(f"An error occurred generating the AI summary: {str(e)}", thread_ts=thread_ts)
    logger.success(f"Review completed for {market}-{month_full}-{year}")

# --- THREAD FOLLOW-UP HANDLER ---
def handle_thread_messages(event, say, client, context):
    user_message = event.get("text", "").strip()
    thread_ts = event["thread_ts"]
    logger.info(f"Handling follow-up for monthly_review in thread {thread_ts}")
    try:
        context_prompt = f"""
        You are a helpful marketing analyst assistant.
        **Current Context:** A Monthly Review for **{context['params']['market']}** for **{context['params']['month_full']} {context['params']['year']}**.
        **Available Data:** You have the full JSON data for this specific review: {json.dumps({'targets': context.get('raw_target_data', {}), 'actuals': context.get('raw_actual_data', {})})}
        
        **User's Follow-up:** "{user_message}"
        
        **Instructions:**
        1. Answer the user's question **ONLY** using the data provided in the "Available Data" section.
        2. If the user asks about a different month, market, or requires a comparison to data not present, you MUST state that you don't have that data in your current context. Example: "I can't answer that, as my current context is only for the June UK review. To compare with November, you would need to ask me to run a new analysis for November."
        3. Present your answer naturally, without phrases like "based on the provided data".
        """
        response = gemini_model.generate_content(context_prompt)
        ai_response = response.text
        for chunk in split_message_for_slack(ai_response): say(text=chunk, thread_ts=thread_ts)
    except Exception as e:
        logger.error(f"Error handling thread message in month.py: {e}"); say(text="Sorry, I encountered an error.", thread_ts=thread_ts)
