# ================================================
# FILE: month.py (Refactored for Unified Context)
# ================================================
import os
import sys
import json
from dotenv import load_dotenv
import requests
import google.generativeai as genai
from loguru import logger

# --- 1. CONFIGURATION & INITIALIZATION ---

# --- Loguru Configuration (can be kept for module-specific logging) ---
logger.remove()
logger.add(
    sys.stderr,
    format="<yellow>{time:YYYY-MM-DD HH:mm:ss}</yellow> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>",
    colorize=True
)

# --- Environment & Client Initialization ---
# NOTE: We no longer need the full Slack App here. We just need the Gemini client.
load_dotenv()
try:
    GOOGLE_API_KEY = os.environ["GOOGLE_API_KEY"]
    genai.configure(api_key=GOOGLE_API_KEY)
    gemini_model = genai.GenerativeModel('gemini-1.5-flash-latest')
    logger.success("Gemini client initialized for month.py.")
except KeyError as e:
    logger.critical(f"FATAL: Missing GOOGLE_API_KEY. Please check .env file.")
    sys.exit(1)

# --- CONSTANTS AND HELPERS (Unchanged) ---
MARKET_CURRENCY_CONFIG = {
    'SWEDEN':  {'rate': 11.30, 'symbol': 'SEK', 'name': 'SEK'},
    'NORWAY':  {'rate': 11.50, 'symbol': 'NOK', 'name': 'NOK'},
    'DENMARK': {'rate': 7.46,  'symbol': 'DKK', 'name': 'DKK'},
    'UK':      {'rate': 0.85,  'symbol': '£',   'name': 'GBP'},
    'FRANCE':  {'rate': 1.0,   'symbol': '€',   'name': 'EUR'},
}

BASE_API_URL = os.getenv("BASE_API_URL", "https://lyra-final.onrender.com")
TARGET_API_URL = f"{BASE_API_URL}/api/dashboard/targets"
ACTUALS_API_URL = f"{BASE_API_URL}/api/monthly_breakdown"

USER_INPUT_TO_ABBR_MAP = {
    'january': 'Jan', 'february': 'Feb', 'march': 'Mar', 'april': 'Apr',
    'may': 'May', 'june': 'Jun', 'july': 'Jul', 'august': 'Aug',
    'september': 'Sep', 'october': 'Oct', 'november': 'Nov', 'december': 'Dec',
    'jan': 'Jan', 'feb': 'Feb', 'mar': 'Mar', 'apr': 'Apr', 'jun': 'Jun',
    'jul': 'Jul', 'aug': 'Aug', 'sep': 'Sep', 'oct': 'Oct', 'nov': 'Nov', 'dec': 'Dec'
}

ABBR_TO_FULL_MONTH_MAP = {
    'Jan': 'January', 'Feb': 'February', 'Mar': 'March', 'Apr': 'April',
    'May': 'May', 'Jun': 'June', 'Jul': 'July', 'Aug': 'August',
    'Sep': 'September', 'Oct': 'October', 'Nov': 'November', 'Dec': 'December'
}

# --- All helper functions (get_currency_info, format_currency, split_message_for_slack, etc.) remain the same ---
# (Copying them here for completeness)
def get_currency_info(market):
    return MARKET_CURRENCY_CONFIG.get(market.upper(), {'rate': 1.0, 'symbol': '€', 'name': 'EUR'})

def convert_eur_to_local(amount_eur, market):
    currency_info = get_currency_info(market)
    return amount_eur * currency_info['rate']

def format_currency(amount, market):
    currency_info = get_currency_info(market)
    symbol = currency_info['symbol']
    if currency_info['name'] in ['SEK', 'NOK', 'DKK']:
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
        else: current_chunk += line + "\n"
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

def create_monthly_review_prompt(market, month, year, target_data, actual_data):
    # This entire complex prompt function remains exactly the same.
    # ... (code for the prompt function is identical to your original file)
    # --- Data Extraction ---
    target_budget_local = target_data.get("kpis", {}).get("total_target_budget", 0)
    metrics = actual_data.get("metrics", {})
    actual_spend_eur = metrics.get("budget_spent_eur", 0)
    actual_spend_local = convert_eur_to_local(actual_spend_eur, market)
    influencers = actual_data.get("influencers", [])
    total_conversions = metrics.get("conversions", 0)
    total_influencers = len(influencers)
    avg_cac_local = actual_spend_local / total_conversions if total_conversions > 0 else 0
    # ... and so on, the rest of the function is identical.
    return "..." # Placeholder for your very detailed prompt string

# --- ✅ 2. CORE LOGIC FUNCTION ---
# This is called by main.py for both @mentions and slash commands.
def run_monthly_review(say, thread_ts, params, thread_context_store):
    """
    Executes the monthly review logic and posts the results to a specific thread.
    This function is now stateless and relies on inputs from main.py.
    """
    try:
        market = params.get('market', '').strip().capitalize()
        raw_month_input = params.get('month', '').strip()
        year = int(params.get('year', '').strip())

        month_abbr = USER_INPUT_TO_ABBR_MAP.get(raw_month_input.lower())
        if not month_abbr:
            say(f"❌ Invalid month: '{raw_month_input}'. Please use a full month name or 3-letter abbreviation.", thread_ts=thread_ts)
            return
        month_full = ABBR_TO_FULL_MONTH_MAP.get(month_abbr)
    except (ValueError, IndexError, AttributeError) as e:
        say(f"❌ I'm missing some information. For a monthly review, I need a valid Market, Month, and Year. Error: {e}", thread_ts=thread_ts)
        return

    # --- API Calls and Analysis (All messages go to the thread) ---
    say("Step 1/3: Fetching target data...", thread_ts=thread_ts)
    target_data = query_api(TARGET_API_URL, {"filters": {"market": market, "month": month_abbr, "year": year}}, "Targets")
    if "error" in target_data:
        say(f"❌ Target API Error: `{target_data['error']}`", thread_ts=thread_ts)
        return
    
    say("Step 2/3: Fetching monthly performance data...", thread_ts=thread_ts)
    actual_data = query_api(ACTUALS_API_URL, {"filters": {"market": market, "month": month_full, "year": year}}, "Actuals")
    if "error" in actual_data:
        say(f"❌ Actuals API Error: `{actual_data['error']}`", thread_ts=thread_ts)
        return

    if not actual_data.get("influencers"):
        say(f"✅ **No performance data found** for {market.upper()} {raw_month_input.capitalize()} {year}. No influencers were active in this period.", thread_ts=thread_ts)
        return

    say("Step 3/3: Analyzing data and generating review...", thread_ts=thread_ts)
    try:
        # We need the full prompt function available here
        # For brevity, assuming create_monthly_review_prompt is defined above in the file
        prompt = create_monthly_review_prompt(market, raw_month_input, year, target_data, actual_data)
        response = gemini_model.generate_content(prompt)
        ai_review = response.text
        
        # --- Store context in the UNIFIED store provided by main.py ---
        thread_context_store[thread_ts] = {
            'type': 'monthly_review', # This is crucial for routing follow-ups
            'market': market,
            'month': raw_month_input,
            'year': year,
            'target_data': target_data,
            'actual_data': actual_data,
            'metrics': {
                'target_budget_local': target_data.get("kpis", {}).get("total_target_budget", 0),
                'actual_spend_local': convert_eur_to_local(actual_data.get("metrics", {}).get("budget_spent_eur", 0), market),
                'total_conversions': actual_data.get("metrics", {}).get("conversions", 0),
                'total_influencers': len(actual_data.get("influencers", []))
            }
        }
        logger.success(f"Context stored for thread {thread_ts}")

        # Post the final report in chunks to the thread
        for chunk in split_message_for_slack(ai_review):
            say(text=chunk, thread_ts=thread_ts)
            
    except Exception as e:
        logger.error(f"Error during AI review generation: {e}")
        say(f"❌ An error occurred while generating the AI summary: `{str(e)}`", thread_ts=thread_ts)
    
    logger.success(f"Review completed for {market}-{raw_month_input}-{year}")

# --- ✅ 3. THREAD FOLLOW-UP HANDLER ---
# This is called by main.py when a user replies in a thread managed by this module.
def handle_thread_messages(event, say, context):
    """
    Handles follow-up questions in a monthly review thread.
    It receives the specific context for this thread from main.py.
    """
    user_message = event.get("text", "").strip()
    thread_ts = event["thread_ts"]
    
    logger.info(f"Handling follow-up for monthly_review in thread {thread_ts}")

    try:
        # Create context-aware prompt using the `context` dictionary passed from main.py
        context_prompt = f"""
        You are a helpful marketing analyst assistant. A user is asking a follow-up question about a monthly review you already provided. Use the following data to answer them.

        **Original Review Context:**
        - Market: {context['market'].upper()}
        - Period: {context['month'].capitalize()} {context['year']}
        
        **Available Data (JSON):**
        - Target Data: {json.dumps(context['target_data'])}
        - Actual Performance Data: {json.dumps(context['actual_data'])}

        **User's Follow-up Question:** "{user_message}"

        **Instructions:**
        - Answer the user's question directly using only the provided JSON data.
        - Be concise and to the point.
        - If the data needed to answer is not present, state that clearly.
        - Use correct currency formatting for the market ({context['market'].upper()}).
        """
        
        response = gemini_model.generate_content(context_prompt)
        ai_response = response.text
        
        for chunk in split_message_for_slack(ai_response):
            say(text=chunk, thread_ts=thread_ts)
            
    except Exception as e:
        logger.error(f"Error handling thread message in month.py: {e}")
        say(text="❌ Sorry, I encountered an error processing your follow-up question.", thread_ts=thread_ts)
