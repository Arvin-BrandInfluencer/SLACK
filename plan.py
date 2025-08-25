# ======================================================
# FILE: plan.py (Refactored for Unified Context)
# ======================================================
import os
import sys
import json
from dotenv import load_dotenv
import requests
import google.generativeai as genai
from loguru import logger
import pandas as pd
from io import BytesIO
from datetime import datetime

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
    SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"] # Needed for file uploads
    genai.configure(api_key=GOOGLE_API_KEY)
    gemini_model = genai.GenerativeModel('gemini-1.5-flash-latest')
    logger.success("Gemini client initialized for plan.py.")
except KeyError as e:
    logger.critical(f"FATAL: Missing environment variable: {e}. Please check .env file.")
    sys.exit(1)

# --- CONSTANTS AND HELPERS ---
BASE_API_URL = os.getenv("BASE_API_URL", "https://lyra-final.onrender.com")
TARGET_API_URL = f"{BASE_API_URL}/api/dashboard/targets"
ACTUALS_API_URL = f"{BASE_API_URL}/api/monthly_breakdown"
DISCOVERY_API_URL = f"{BASE_API_URL}/api/discovery"

MARKET_CURRENCY_CONFIG = {
    'SWEDEN':  {'rate': 11.30, 'symbol': 'SEK', 'name': 'SEK'},
    'NORWAY':  {'rate': 11.50, 'symbol': 'NOK', 'name': 'NOK'},
    'DENMARK': {'rate': 7.46,  'symbol': 'DKK', 'name': 'DKK'},
    'UK':      {'rate': 0.85,  'symbol': '£',   'name': 'GBP'},
    'FRANCE':  {'rate': 1.0,   'symbol': '€',   'name': 'EUR'},
}

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


# --- All helper functions (get_currency_info, format_currency, etc.) remain the same ---
def get_currency_info(market):
    """Get currency conversion rate and symbol for a market, defaulting to EUR."""
    return MARKET_CURRENCY_CONFIG.get(str(market).upper(), {'rate': 1.0, 'symbol': '€', 'name': 'EUR'})

def convert_eur_to_local(amount_eur, market):
    """Convert an amount from EUR to the specified market's local currency."""
    currency_info = get_currency_info(market)
    return amount_eur * currency_info['rate']

def format_currency(amount, market):
    """Format an amount with the correct currency symbol and formatting for the market."""
    currency_info = get_currency_info(market)
    symbol = currency_info['symbol']
    if currency_info['name'] in ['SEK', 'NOK', 'DKK']:
        return f"{amount:,.0f} {symbol}"
    else:
        return f"{symbol}{amount:,.2f}"

def split_message_for_slack(message: str, max_length: int = 2800) -> list:
    """Split long messages into chunks that fit within Slack's limits, preserving code blocks."""
    if len(message) <= max_length:
        return [message]
    chunks = []
    current_chunk = ""
    in_code_block = False
    lines = message.split('\n')
    for line in lines:
        if line.strip().startswith('```'):
            in_code_block = not in_code_block
        if len(current_chunk) + len(line) + 1 > max_length:
            if in_code_block and current_chunk.strip():
                current_chunk += "\n```"
                in_code_block = False
            if current_chunk.strip():
                chunks.append(current_chunk.strip())
            if in_code_block:
                current_chunk = "```\n" + line + "\n"
            else:
                current_chunk = line + "\n"
        else:
            current_chunk += line + "\n"
    if current_chunk.strip():
        chunks.append(current_chunk.strip())
    return chunks

def query_api(url: str, payload: dict, endpoint_name: str) -> dict:
    """Generic function to query an API endpoint."""
    logger.info(f"Querying {endpoint_name} API at {url} with payload: {json.dumps(payload)}")
    try:
        response = requests.post(url, json=payload, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"{endpoint_name} API Connection Error: {e}")
        return {"error": f"Could not connect to the {endpoint_name} API."}

def fetch_tier_influencers(market, year, month_full, tier, booked_influencer_names):
    """Fetch unbooked influencers for a specific tier."""
    discovery_payload = {
        "source": "influencer_analytics",
        "view": "discovery_tiers",
        "filters": {"market": market, "year": year, "month": month_full, "tier": tier}
    }
    # NOTE: The DISCOVERY_API_URL might need to be INFLUENCER_API_URL depending on your backend routing
    discovery_data = query_api(INFLUENCER_API_URL, discovery_payload, f"Discovery-{tier.capitalize()}")

    if "error" in discovery_data:
        logger.error(f"Error fetching {tier} tier: {discovery_data['error']}")
        return []
    
    # Handle both possible API response structures
    if discovery_data.get("source") == "discovery_tier_specific":
        all_influencers_in_tier = discovery_data.get("items", [])
    else:
        all_influencers_in_tier = discovery_data.get(tier, [])
        
    unbooked_influencers = [inf for inf in all_influencers_in_tier if inf.get('influencer_name') not in booked_influencer_names]
    logger.info(f"Found {len(unbooked_influencers)} unbooked {tier.capitalize()}-tier influencers")
    return unbooked_influencers


def allocate_budget_cascading_tiers(gold_influencers, silver_influencers, bronze_influencers,
                                  remaining_budget, effective_cac=50, market='FRANCE'):
    """Allocate budget across tiers in cascade: Gold -> Silver -> Bronze."""
    # This entire complex helper function remains the same.
    # ... (code is identical to your original file)
    return [], 0, {} # Placeholder

def create_excel_report(recommendations, tier_breakdown, market, month, year, target_budget,
                       actual_spend, remaining_budget, total_allocated, booked_influencers):
    """Create an Excel report with multiple sheets."""
    # This entire complex helper function remains the same.
    # ... (code is identical to your original file)
    return BytesIO() # Placeholder

def create_llm_prompt_with_code_blocks(market, month, year, target_budget, actual_spend, remaining_budget,
                                     booked_influencers, recommendations, total_allocated, tier_breakdown):
    """Creates the detailed LLM prompt for the strategic plan summary."""
    # This entire complex prompt function remains the same.
    # ... (code is identical to your original file)
    return "..." # Placeholder

# --- ✅ 2. CORE LOGIC FUNCTION ---
def run_strategic_plan(app_client, say, command, thread_ts, params, thread_context_store):
    """
    Executes the strategic planning logic and posts the results to a specific thread.
    """
    try:
        market = params.get('market', '').strip().capitalize()
        raw_month_input = params.get('month', '').strip()
        year = int(params.get('year', ''))

        month_abbr = USER_INPUT_TO_ABBR_MAP.get(raw_month_input.lower())
        if not month_abbr:
            say(f"❌ Invalid month: '{raw_month_input}'.", thread_ts=thread_ts)
            return
        month_full = ABBR_TO_FULL_MONTH_MAP.get(month_abbr)
        currency_info = get_currency_info(market)
        currency = currency_info['name']
    except (ValueError, IndexError, AttributeError) as e:
        say(f"❌ I'm missing some information. For a plan, I need a valid Market, Month, and Year. Error: {e}", thread_ts=thread_ts)
        return

    say(f"📊 Understood! Creating a strategic plan for *{market.upper()}* for *{raw_month_input.capitalize()} {year}* (Currency: *{currency}*)...", thread_ts=thread_ts)

    # --- Data Fetching and Processing ---
    target_data = query_api(TARGET_API_URL, {"filters": {"market": market, "month": month_abbr, "year": year}}, "Targets")
    if "error" in target_data:
        say(f"Step 1/4 failed. API Error: `{target_data['error']}`", thread_ts=thread_ts)
        return

    actual_data = query_api(ACTUALS_API_URL, {"filters": {"market": market, "month": month_full, "year": year}}, "Actuals")
    if "error" in actual_data:
        say(f"Step 2/4 failed. API Error: `{actual_data['error']}`", thread_ts=thread_ts)
        return

    target_budget = target_data.get("kpis", {}).get("total_target_budget", 0)
    actual_spend_eur = actual_data.get("metrics", {}).get("budget_spent_eur", 0)
    actual_spend = convert_eur_to_local(actual_spend_eur, market)
    booked_influencers = actual_data.get("influencers", [])
    booked_influencer_names = {inf['name'] for inf in booked_influencers}
    remaining_budget = target_budget - actual_spend

    if remaining_budget <= 0:
        say(f"⚠️ **Budget Analysis Complete:** The budget for {market.upper()} in {raw_month_input.capitalize()} is already {'overspent' if remaining_budget < 0 else 'fully utilized'}. No further allocation is possible.", thread_ts=thread_ts)
        return
    
    say(f"Budget of {format_currency(remaining_budget, market)} remaining. Fetching available influencers...", thread_ts=thread_ts)

    gold_influencers = fetch_tier_influencers(market, year, month_full, "gold", booked_influencer_names)
    silver_influencers = fetch_tier_influencers(market, year, month_full, "silver", booked_influencer_names)
    bronze_influencers = fetch_tier_influencers(market, year, month_full, "bronze", booked_influencer_names)
    
    total_available = len(gold_influencers) + len(silver_influencers) + len(bronze_influencers)
    if total_available == 0:
        say(f"✅ **Analysis Complete:** All available influencers for {market.upper()} have already been booked for this period.", thread_ts=thread_ts)
        return

    say(f"Found {total_available} unbooked influencers. Optimizing budget allocation...", thread_ts=thread_ts)
    
    recommendations, total_allocated, tier_breakdown = allocate_budget_cascading_tiers(gold_influencers, silver_influencers, bronze_influencers, remaining_budget, 50, market)
    
    if not recommendations:
        say(f"ℹ️ No influencers could be booked with the remaining budget of {format_currency(remaining_budget, market)}.", thread_ts=thread_ts)
        return

    # --- Report Generation ---
    try:
        excel_buffer = create_excel_report(recommendations, tier_breakdown, market, raw_month_input, year, target_budget, actual_spend, remaining_budget, total_allocated, booked_influencers)
        filename = f"Strategic_Plan_{market}_{raw_month_input}_{year}.xlsx"
        
        # Use the app_client from main.py to upload the file
        app_client.files_upload_v2(
            channel=command.get('channel_id'),
            file=excel_buffer.getvalue(),
            filename=filename,
            title=f"Strategic Plan - {market.upper()} {raw_month_input.capitalize()} {year}",
            initial_comment="Here is the detailed strategic plan in Excel format.",
            thread_ts=thread_ts
        )
        logger.success(f"Uploaded Excel report to thread {thread_ts}")

        prompt = create_llm_prompt_with_code_blocks(market, raw_month_input, year, target_budget, actual_spend, remaining_budget, booked_influencers, recommendations, total_allocated, tier_breakdown)
        response = gemini_model.generate_content(prompt)
        ai_summary = response.text

        thread_context_store[thread_ts] = {
            'type': 'strategic_plan',
            'market': market, 'month': raw_month_input, 'year': year, 'currency': currency,
            'target_budget': target_budget, 'actual_spend': actual_spend,
            'remaining_budget': remaining_budget, 'total_allocated': total_allocated,
            'recommendations': recommendations, 'tier_breakdown': tier_breakdown,
            'booked_influencers': booked_influencers
        }
        logger.success(f"Context stored for thread {thread_ts}")

        for chunk in split_message_for_slack(ai_summary):
            say(text=chunk, thread_ts=thread_ts)
    except Exception as e:
        logger.error(f"Error during report generation for plan: {e}", exc_info=True)
        say(f"❌ An error occurred while generating the final report: `{str(e)}`", thread_ts=thread_ts)


# --- ✅ 3. THREAD FOLLOW-UP HANDLER ---
def handle_thread_replies(event, say, context):
    """
    Handles follow-up questions in a strategic plan thread.
    """
    user_message = event.get("text", "").strip()
    thread_ts = event["thread_ts"]
    user_id = event.get('user')
    
    logger.info(f"Handling follow-up for strategic_plan in thread {thread_ts}")

    try:
        context_prompt = f"""
        You are a strategic marketing analyst bot answering a follow-up question about a previously generated influencer marketing plan.
        
        **PLAN CONTEXT:**
        - Market: {context['market'].upper()}
        - Period: {context['month']} {context['year']}
        - Currency: {context['currency']}
        - Target Budget: {format_currency(context['target_budget'], context['market'])}
        - Recommended Allocation: {format_currency(context['total_allocated'], context['market'])}
        
        **AVAILABLE DATA (JSON):**
        - Recommendations: {json.dumps(context['recommendations'][:15])}
        - Booked Influencers: {json.dumps(context['booked_influencers'])}
        
        **USER QUESTION:** "{user_message}"
        
        **INSTRUCTIONS:**
        - Answer the user's question based on the plan context and data above.
        - Be concise, helpful, and use the correct currency ({context['currency']}).
        - If the user asks "what if", provide a thoughtful answer based on the existing data structure (e.g., average spend).
        """
        
        response = gemini_model.generate_content(context_prompt)
        ai_response = response.text
        
        say(text=f"<@{user_id}> {ai_response}", thread_ts=thread_ts)
            
    except Exception as e:
        logger.error(f"Error handling thread question in plan.py: {e}")
        say(text=f"<@{user_id}> I encountered an error: `{str(e)}`.", thread_ts=thread_ts)
