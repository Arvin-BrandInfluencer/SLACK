
# ================================================
# FILE: trend.py (Refactored for Unified Context)
# ================================================
import os
import sys
import json
import requests
from dotenv import load_dotenv
import google.generativeai as genai
from loguru import logger

# --- 1. CONFIGURATION & INITIALIZATION ---
logger.remove()
logger.add(sys.stderr, format="<yellow>{time:YYYY-MM-DD HH:mm:ss}</yellow> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>", colorize=True)

load_dotenv()
try:
    GOOGLE_API_KEY = os.environ["GOOGLE_API_KEY"]
    genai.configure(api_key=GOOGLE_API_KEY)
    model = genai.GenerativeModel('gemini-1.5-flash-latest')
    logger.success("Gemini client initialized for trend.py.")
except KeyError as e:
    logger.critical(f"FATAL: Missing GOOGLE_API_KEY. Please check .env file.")
    sys.exit(1)

# --- CONSTANTS AND HELPERS ---
BASE_API_URL = os.getenv("BASE_API_URL", "https://lyra-final.onrender.com")
CURRENCY_MAP = { 'SWEDEN': 'SEK', 'NORWAY': 'NOK', 'DENMARK': 'DKK', 'UK': 'GBP', 'FRANCE': 'EUR' }

def get_currency_symbol(market):
    return CURRENCY_MAP.get(str(market).upper(), 'EUR')

def split_message_for_slack(message: str, max_length: int = 2800) -> list:
    """Splits a long message into chunks for Slack, respecting code blocks."""
    if len(message) <= max_length:
        return [message]
    
    chunks, current_chunk, in_code_block = [], "", False
    for line in message.split('\n'):
        if line.strip().startswith('```'):
            in_code_block = not in_code_block
        
        if len(current_chunk) + len(line) + 1 > max_length:
            if in_code_block and current_chunk:
                current_chunk += "\n```"
                in_code_block = False
            if current_chunk:
                chunks.append(current_chunk)
            current_chunk = "```\n" + line + "\n" if in_code_block else line + "\n"
        else:
            current_chunk += line + "\n"
            
    if current_chunk:
        chunks.append(current_chunk)
        
    return chunks

# --- ✅ 2. CORE LOGIC FUNCTION ---
def run_influencer_trend(say, thread_ts, params, thread_context_store):
    """
    Executes the influencer trend analysis. It receives pre-validated, clean parameters from main.py.
    """
    # Parameters are received clean from main.py, no need for validation or mapping here.
    # We only need to build the filters dictionary from the provided params.
    filters = {}
    if 'market' in params:
        filters['market'] = params['market']
    if 'year' in params:
        filters['year'] = params['year']
    if 'month_full' in params: # The influencer_analytics API view expects the full month name for filtering
        filters['month'] = params['month_full']
    if 'tier' in params:
        filters['tier'] = params['tier']

    say(f"🔎 Fetching influencer trend data with filters: `{filters}`...", thread_ts=thread_ts)
    
    url = f"{BASE_API_URL}/api/influencer/query"
    payload = { "source": "influencer_analytics", "view": "discovery_tiers", "filters": filters }
    
    try:
        response = requests.post(url, json=payload, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        all_influencers = []
        # The API can return one specific tier or all tiers in a dictionary
        if data.get("source") == "discovery_tier_specific":
            all_influencers = data.get("items", [])
        else: # If it returns the dictionary of gold/silver/bronze
            for tier_name in ["gold", "silver", "bronze"]:
                if isinstance(data.get(tier_name), list):
                    all_influencers.extend(data[tier_name])
        
        if not all_influencers:
            say("❌ No trend data found for the specified filters.", thread_ts=thread_ts)
            return
        
        say(f"📊 Found **{len(all_influencers)}** influencers. Compiling leaderboards...", thread_ts=thread_ts)
        
        thread_context_store[thread_ts] = { 'type': 'influencer_trend', 'filters': filters, 'data': all_influencers }
        
        currency = get_currency_symbol(filters.get('market', 'FRANCE'))
        
        by_conversions = sorted(all_influencers, key=lambda x: x.get('total_conversions', 0), reverse=True)[:25]
        conv_table = "```\nTOP 25 INFLUENCERS BY CONVERSIONS\n"
        conv_table += f"Rank | Name                    | Conversions | CAC (€)      | Spend (€)\n" # Spend is always EUR in summary
        conv_table += "-" * 75 + "\n"
        for i, inf in enumerate(by_conversions, 1):
            name = inf.get('influencer_name', 'N/A')[:20]
            conv = inf.get('total_conversions', 0)
            cac = inf.get('effective_cac_eur', 0)
            spend = inf.get('total_spend_eur', 0)
            conv_table += f"{i:2d}   | {name:<20} | {conv:8.0f}    | {cac:8.2f}   | {spend:10.2f}\n"
        conv_table += "```"

        for chunk in split_message_for_slack(conv_table):
            say(text=chunk, thread_ts=thread_ts)
        
        # Additional tables (like by_cac) can be generated here if needed

        prompt = f"""
        Analyze this influencer trend data for {filters.get('market', 'all markets')}.
        Data includes {len(all_influencers)} total influencers.
        The top performer by conversions is {by_conversions[0]['influencer_name']} with {int(by_conversions[0]['total_conversions'])} conversions.
        Provide a 2-3 sentence executive summary and one key strategic recommendation based on this data.
        """
        
        ai_response = model.generate_content(prompt)
        summary_text = f"🧠 **AI Executive Summary:**\n{ai_response.text}"
        for chunk in split_message_for_slack(summary_text):
            say(text=chunk, thread_ts=thread_ts)
        
    except requests.exceptions.RequestException as e:
        logger.error(f"API Error in trend.py: {e}")
        say(f"❌ API Connection Error: Could not fetch trend data.", thread_ts=thread_ts)
    except Exception as e:
        logger.error(f"An unexpected error occurred in trend.py: {e}", exc_info=True)
        say(f"❌ An unexpected error occurred: {str(e)}", thread_ts=thread_ts)

# --- ✅ 3. THREAD FOLLOW-UP HANDLER ---
def handle_thread_messages(event, say, context):
    """
    Handles follow-up questions in a trend analysis thread.
    """
    user_message = event.get("text", "").strip()
    thread_ts = event["thread_ts"]
    
    logger.info(f"Handling follow-up for influencer_trend in thread {thread_ts}")
    
    try:
        context_prompt = f"""
        You are a helpful marketing analyst assistant. A user is asking a follow-up question about an influencer trend report you already provided. Use the following data to answer them.

        **Original Report Context:**
        - Filters Used: {json.dumps(context.get('filters', {}))}
        
        **Available Data (JSON, showing first 15 records):**
        {json.dumps(context.get('data', [])[:15], indent=2)}

        **User's Follow-up Question:** "{user_message}"

        **Instructions:**
        - Answer the user's question directly using only the provided JSON data.
        - Be concise and to the point.
        - If the data needed is not present in the sample, state that you can only analyze the top records shown.
        """
        
        response = model.generate_content(context_prompt)
        ai_response = response.text

        for chunk in split_message_for_slack(ai_response):
            say(text=chunk, thread_ts=thread_ts)
            
    except Exception as e:
        logger.error(f"Error handling thread message in trend.py: {e}")
        say(text="❌ Sorry, I had trouble processing your follow-up.", thread_ts=thread_ts)
