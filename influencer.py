# ======================================================
# FILE: influencer.py (FINAL - BUGS FIXED)
# ======================================================
import os
import sys
import json
from dotenv import load_dotenv
import requests
import google.generativeai as genai
from loguru import logger
import pandas as pd

# --- 1. CONFIGURATION & INITIALIZATION ---
logger.remove(); logger.add(sys.stderr, format="<yellow>{time:YYYY-MM-DD HH:mm:ss}</yellow> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>", colorize=True)
load_dotenv()
try:
    GOOGLE_API_KEY = os.environ["GOOGLE_API_KEY"]; genai.configure(api_key=GOOGLE_API_KEY)
    gemini_model = genai.GenerativeModel('gemini-1.5-flash-latest'); logger.success("Gemini client initialized for influencer.py.")
except KeyError as e:
    logger.critical(f"FATAL: Missing GOOGLE_API_KEY. Please check .env file."); sys.exit(1)

# --- CONSTANTS AND HELPERS ---
BASE_API_URL = os.getenv("BASE_API_URL", "http://127.0.0.1:10000"); UNIFIED_API_URL = f"{BASE_API_URL}/api/influencer/query"
RATES = { "EUR": 1.0, "GBP": 0.85, "SEK": 11.30, "NOK": 11.50, "DKK": 7.46 }

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
        response = requests.post(url, json=payload, timeout=60); response.raise_for_status(); return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"{endpoint_name} API Connection Error: {e}"); return {"error": f"Could not connect to the {endpoint_name} API."}

def create_prompt(user_query, influencer_name, summary_stats, campaigns, is_deep_dive):
    return f"""
    You are Nova, a graceful and helpful marketing analyst assistant.
    {"Generate a comprehensive deep-dive performance report for the influencer." if is_deep_dive else "Provide a concise, direct answer to the user's question about the influencer."}
    **Data Context for Influencer '{influencer_name}':**
    - Summary Stats: {json.dumps(summary_stats)}
    - Full Campaign Data: {json.dumps(campaigns)}
    **User's Request:** "{user_query if user_query else "A full analysis."}"
    **Instructions:** Frame your response as a helpful analyst. If data is sparse or missing, note it gracefully. Use bold formatting for key metrics.
    """

# --- CORE LOGIC FUNCTION ---
def run_influencer_analysis(say, thread_ts, params, thread_context_store, user_query=None):
    try:
        influencer_name = params['influencer_name']
        filters = {"influencer_name": influencer_name}
        if 'year' in params and params['year']: filters['year'] = params['year']
    except KeyError as e:
        say(f"A required parameter was missing: {e}", thread_ts=thread_ts); return

    payload = {"source": "influencer_analytics", "view": "influencer_performance", "filters": filters}
    api_data = query_api(UNIFIED_API_URL, payload, "Influencer Analytics")

    if "error" in api_data or not api_data.get("campaigns"):
        say(f"No campaigns found for '{influencer_name}' with the specified filters.", thread_ts=thread_ts); return

    campaigns = api_data["campaigns"]; df = pd.DataFrame(campaigns)
    total_spend_eur = sum(float(c.get('total_budget_clean', 0)) / RATES.get(str(c.get('currency', 'EUR')).upper(), 1.0) for c in campaigns)
    total_conversions = df['actual_conversions_clean'].sum()
    summary_stats = {
        "influencer_name": influencer_name, "total_campaigns": len(df), "markets": list(df['market'].unique()),
        "total_spend_eur": total_spend_eur, "total_conversions": int(total_conversions),
        "effective_cac_eur": total_spend_eur / total_conversions if total_conversions > 0 else 0,
        "average_ctr": df['ctr'].mean() if 'ctr' in df.columns and not df['ctr'].empty else 0.0
    }

    try:
        is_deep_dive = not user_query or any(kw in user_query.lower() for kw in ["deep dive", "details", "analyse"])
        prompt = create_prompt(user_query, influencer_name, summary_stats, campaigns, is_deep_dive)
        
        response = gemini_model.generate_content(prompt)
        ai_answer = response.text

        thread_context_store[thread_ts] = {
            'type': 'influencer_analysis', 'params': params,
            'raw_api_data': api_data, 'bot_response': ai_answer
        }

        for chunk in split_message_for_slack(ai_answer): say(text=chunk, thread_ts=thread_ts)
    except Exception as e:
        logger.error(f"Error calling Gemini API for influencer analysis: {e}"); say(f"AI analysis failed: `{str(e)}`", thread_ts=thread_ts)

# --- THREAD FOLLOW-UP HANDLER ---
def handle_thread_messages(event, say, client, context):
    user_message = event.get("text", "").strip()
    thread_ts = event["thread_ts"]
    logger.info(f"Handling follow-up for influencer_analysis in thread {thread_ts}")
    try:
        context_prompt = f"""
        You are a helpful marketing analyst assistant.
        **Current Context:** An analysis of influencer **{context['params'].get('influencer_name')}** with filters: {json.dumps(context.get('params', {}))}.
        **Available Data:** You have the full JSON data for this specific influencer analysis: {json.dumps(context.get('raw_api_data', {}))}
        
        **User's Follow-up:** "{user_message}"
        
        **Instructions:**
        1. Answer the user's question **ONLY** using the data from the current context.
        2. If the user asks about a different influencer or a comparison that requires new data, you MUST state that you don't have that data in your current context. Example: "I can't answer that, as my current context is only for {context['params'].get('influencer_name')}. To analyze another influencer, please start a new request like '@nova analyse influencer [name]'."
        """
        response = gemini_model.generate_content(context_prompt)
        ai_response = response.text
        for chunk in split_message_for_slack(ai_response): say(text=chunk, thread_ts=thread_ts)
    except Exception as e: 
        logger.error(f"Error handling thread message in influencer.py: {e}"); say(text="Sorry, I had trouble with your follow-up.", thread_ts=thread_ts)
