# ======================================================
# FILE: influencer.py (Refactored for Unified Context)
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

# --- Loguru Configuration ---
logger.remove()
logger.add(
    sys.stderr,
    format="<yellow>{time:YYYY-MM-DD HH:mm:ss}</yellow> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>",
    colorize=True
)

# --- Environment & Client Initialization ---
load_dotenv()
try:
    GOOGLE_API_KEY = os.environ["GOOGLE_API_KEY"]
    genai.configure(api_key=GOOGLE_API_KEY)
    gemini_model = genai.GenerativeModel('gemini-1.5-flash-latest')
    logger.success("Gemini client initialized for influencer.py.")
except KeyError as e:
    logger.critical(f"FATAL: Missing GOOGLE_API_KEY. Please check .env file.")
    sys.exit(1)

# --- CONSTANTS AND HELPERS (Unchanged) ---
MARKET_CURRENCY_SYMBOLS = {
    'SWEDEN': 'SEK', 'NORWAY': 'NOK', 'DENMARK': 'DKK',
    'UK': '£', 'FRANCE': '€', 'NORDICS': '€'
}
LOCAL_CURRENCY_TO_EUR_RATE = {
    "EUR": 1.0, "GBP": 1/0.85, "SEK": 1/11.30, "NOK": 1/11.50, "DKK": 1/7.46
}
BASE_API_URL = os.getenv("BASE_API_URL", "https://lyra-final.onrender.com")
INFLUENCER_API_URL = f"{BASE_API_URL}/api/influencer/query"
USER_INPUT_TO_ABBR_MAP = {
    'january': 'Jan', 'february': 'Feb', 'march': 'Mar', 'april': 'Apr',
    'may': 'May', 'june': 'Jun', 'july': 'Jul', 'august': 'Aug',
    'september': 'Sep', 'october': 'Oct', 'november': 'Nov', 'december': 'Dec',
    'jan': 'Jan', 'feb': 'Feb', 'mar': 'Mar', 'apr': 'Apr', 'jun': 'Jun',
    'jul': 'Jul', 'aug': 'Aug', 'sep': 'Sep', 'oct': 'Oct', 'nov': 'Nov', 'dec': 'Dec'
}

# --- All helper functions (format_currency, split_message_for_slack, etc.) remain the same ---
def format_currency(amount, market):
    market_upper = str(market).upper()
    symbol = MARKET_CURRENCY_SYMBOLS.get(market_upper, '€')
    if market_upper in ['SWEDEN', 'NORWAY', 'DENMARK']:
        return f"{amount:,.0f} {symbol}"
    else:
        return f"{symbol}{amount:,.2f}"

def split_message_for_slack(message: str, max_length: int = 2800) -> list:
    if len(message) <= max_length: return [message]
    chunks, current_chunk, in_code_block = [], "", False
    for line in message.split('\n'):
        if line.strip().startswith('```'): in_code_block = not in_code_block
        if len(current_chunk) + len(line) + 1 > max_length:
            if in_code_block and current_chunk: current_chunk += "\n```"; in_code_block = False
            if current_chunk: chunks.append(current_chunk)
            current_chunk = "```\n" + line + "\n" if in_code_block else line + "\n"
        else:
            current_chunk += line + "\n"
    if current_chunk: chunks.append(current_chunk)
    return chunks

def query_api(url: str, payload: dict, endpoint_name: str) -> dict:
    logger.info(f"Querying {endpoint_name} API at {url} with payload: {payload}")
    try:
        response = requests.post(url, json=payload, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"{endpoint_name} API Connection Error: {e}")
        return {"error": f"Could not connect to the {endpoint_name} API."}

def create_influencer_analysis_prompt(influencer_name, campaigns, summary_stats):
    # This entire complex prompt function remains exactly the same.
    # ... (code for the prompt function is identical to your original file)
    return "..." # Placeholder for your very detailed prompt string

# --- ✅ 2. CORE LOGIC FUNCTION ---
# This is called by main.py for both @mentions and slash commands.
def run_influencer_analysis(say, thread_ts, params, thread_context_store):
    """
    Executes the influencer analysis logic and posts the results to a specific thread.
    This function is now stateless and relies on inputs from main.py.
    """
    try:
        influencer_name = params.get('influencer_name', '').strip()
        if not influencer_name:
            say("❌ I need an influencer's name to run an analysis.", thread_ts=thread_ts)
            return
            
        filters = {"influencer_name": influencer_name}
        
        # Optional parameters
        if 'year' in params and params['year']:
            filters['year'] = int(params['year'])
        if 'month' in params and params['month']:
            month_abbr = USER_INPUT_TO_ABBR_MAP.get(str(params['month']).lower())
            if not month_abbr:
                say(f"❌ Invalid month provided: '{params['month']}'.", thread_ts=thread_ts)
                return
            filters['month'] = month_abbr

    except (ValueError, AttributeError) as e:
        say(f"❌ There was an issue with the parameters provided for the analysis. Please check them. Error: {e}", thread_ts=thread_ts)
        return

    # --- API Calls and Analysis ---
    say(f"🔎 Analyzing performance for *{influencer_name}*...", thread_ts=thread_ts)

    payload = {"source": "influencer_analytics", "filters": filters}
    api_data = query_api(INFLUENCER_API_URL, payload, "Influencer Analytics")

    if "error" in api_data or not api_data.get("campaigns"):
        error_msg = api_data.get("error", f"No campaigns found for '{influencer_name}' with the specified filters.")
        say(f"❌ {error_msg}", thread_ts=thread_ts)
        return

    campaigns = api_data["campaigns"]
    df = pd.DataFrame(campaigns)
    
    total_spend_eur = sum(
        c.get('total_budget_clean', 0) * LOCAL_CURRENCY_TO_EUR_RATE.get(c.get('currency', 'EUR'), 1.0)
        for c in campaigns
    )
    total_conversions = df['actual_conversions_clean'].sum()
    
    summary_stats = {
        "total_campaigns": len(df),
        "markets": list(df['market'].unique()),
        "total_spend_eur": total_spend_eur,
        "total_conversions": int(total_conversions),
        "effective_cac_eur": total_spend_eur / total_conversions if total_conversions > 0 else 0,
        "average_ctr": df['ctr'].mean() if 'ctr' in df.columns else 0.0
    }

    try:
        # Assuming create_influencer_analysis_prompt is defined above
        prompt = create_influencer_analysis_prompt(influencer_name, campaigns, summary_stats)
        response = gemini_model.generate_content(prompt)
        ai_analysis = response.text

        # --- Store context in the UNIFIED store from main.py ---
        thread_context_store[thread_ts] = {
            'type': 'influencer_analysis',
            'influencer_name': influencer_name,
            'filters': filters,
            'campaigns': campaigns,
            'summary_stats': summary_stats
        }
        logger.success(f"Context stored for thread {thread_ts}")

        # Post the final report in chunks to the thread
        for chunk in split_message_for_slack(ai_analysis):
            say(text=chunk, thread_ts=thread_ts)

    except Exception as e:
        logger.error(f"Error calling Gemini API for influencer analysis: {e}")
        say(f"❌ AI analysis failed: `{str(e)}`", thread_ts=thread_ts)


# --- ✅ 3. THREAD FOLLOW-UP HANDLER ---
# This is called by main.py when a user replies in a thread managed by this module.
def handle_thread_messages(event, say, context):
    """
    Handles follow-up questions in an influencer analysis thread.
    It receives the specific context for this thread from main.py.
    """
    user_message = event.get("text", "").strip()
    thread_ts = event["thread_ts"]
    
    logger.info(f"Handling follow-up for influencer_analysis in thread {thread_ts}")
    
    try:
        # Create context-aware prompt using the `context` dictionary
        context_prompt = f"""
        You are a helpful marketing analyst assistant. A user is asking a follow-up question about an influencer analysis you already provided. Use the following data to answer them.

        **Original Analysis Context:**
        - Influencer Name: {context.get('influencer_name')}
        - Original Filters: {json.dumps(context.get('filters', {}))}
        
        **Available Data (JSON):**
        {json.dumps(context, indent=2)}

        **User's Follow-up Question:** "{user_message}"

        **Instructions:**
        - Answer the user's question directly using only the provided JSON data.
        - Be concise and to the point.
        - If you perform a calculation, briefly explain it.
        - If the data needed to answer is not present, state that clearly.
        - Use correct currency formatting when referencing financial data from the 'campaigns' list.
        """
        
        response = gemini_model.generate_content(context_prompt)
        ai_response = response.text

        for chunk in split_message_for_slack(ai_response):
            say(text=chunk, thread_ts=thread_ts)
            
    except Exception as e:
        logger.error(f"Error handling thread message in influencer.py: {e}")
        say(text="❌ Sorry, I had trouble processing your follow-up.", thread_ts=thread_ts)
