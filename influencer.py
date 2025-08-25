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
logger.remove()
logger.add(sys.stderr, format="<yellow>{time:YYYY-MM-DD HH:mm:ss}</yellow> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>", colorize=True)

load_dotenv()
try:
    GOOGLE_API_KEY = os.environ["GOOGLE_API_KEY"]
    genai.configure(api_key=GOOGLE_API_KEY)
    gemini_model = genai.GenerativeModel('gemini-1.5-flash-latest')
    logger.success("Gemini client initialized for influencer.py.")
except KeyError as e:
    logger.critical(f"FATAL: Missing GOOGLE_API_KEY. Please check .env file.")
    sys.exit(1)

# --- CONSTANTS AND HELPERS ---
MARKET_CURRENCY_SYMBOLS = { 'SWEDEN': 'SEK', 'NORWAY': 'NOK', 'DENMARK': 'DKK', 'UK': '£', 'FRANCE': '€', 'NORDICS': '€' }
EUR_TO_LOCAL_RATE = { "EUR": 1.0, "GBP": 0.85, "SEK": 11.30, "NOK": 11.50, "DKK": 7.46 }
LOCAL_CURRENCY_TO_EUR_RATE = {key: 1/value for key, value in EUR_TO_LOCAL_RATE.items()}

BASE_API_URL = os.getenv("BASE_API_URL", "https://lyra-final.onrender.com")
INFLUENCER_API_URL = f"{BASE_API_URL}/api/influencer/query"

def format_currency(amount, market):
    market_upper = str(market).upper()
    symbol = MARKET_CURRENCY_SYMBOLS.get(market_upper, '€')
    if market_upper in ['SWEDEN', 'NORWAY', 'DENMARK']:
        return f"{amount:,.0f} {symbol}"
    else:
        return f"£{amount:,.2f}" if market_upper == 'UK' else f"€{amount:,.2f}"

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
    campaign_table_rows = []
    for c in campaigns:
        market = c.get('market', 'N/A')
        row = (
            f"{c.get('year')}-{c.get('month', 'N/A'):<3} | "
            f"{market:<7} | "
            f"{format_currency(c.get('total_budget_clean', 0), market):>11} | "
            f"{c.get('actual_conversions_clean', 0):<5.0f} | "
            f"{format_currency(c.get('cac_local', 0), market):>9} | "
            f"{c.get('ctr', 0):>5.2%}"
        )
        campaign_table_rows.append(row)
    campaign_table_str = "\n".join(campaign_table_rows)

    prompt = f"""
    You are an expert influencer marketing analyst. A user has requested an analysis of an influencer's performance.

    **INSTRUCTIONS ON CURRENCY:**
    - The "Profile Summary" provides totals in EUR for comparison.
    - The "Campaign Breakdown" table shows each campaign in its LOCAL CURRENCY (e.g., SEK, GBP).
    - Your analysis must reflect this.

    **GENERATE A REPORT WITH THIS EXACT FORMAT:**

    1.  **Profile Summary (Code Block):**
        ```
        Influencer Profile: {influencer_name}
        =========================================================
        Total Campaigns:      {summary_stats['total_campaigns']}
        Markets:              {', '.join(summary_stats['markets'])}
        Total Spend (EUR):    €{summary_stats['total_spend_eur']:,.2f}
        Total Conversions:    {summary_stats['total_conversions']}
        Effective CAC (EUR):  €{summary_stats['effective_cac_eur']:,.2f}
        Average CTR:          {summary_stats['average_ctr']:.2%}
        ```

    2.  **Performance Analysis (Bulleted List):**
        - Provide a concise, top-level analysis. Is this a strong performer?

    3.  **Strengths & Weaknesses (Bulleted List):**
        - List 2-3 key points, referencing specific campaigns and their local currency performance (e.g., "Excellent efficiency in Sweden with a CAC of just 150 SEK.").

    4.  **Campaign Breakdown (Code Block):**
        ```
        Campaign Details (in Local Currency)
        ========================================================================
        Date       | Market  | Budget      | Conv. | CAC       | CTR
        -----------|---------|-------------|-------|-----------|-------
        {campaign_table_str}
        ```
    """
    return prompt

# --- ✅ 2. CORE LOGIC FUNCTION ---
def run_influencer_analysis(say, thread_ts, params, thread_context_store):
    """
    Executes the influencer analysis logic. It receives pre-validated, clean parameters from main.py.
    """
    try:
        influencer_name = params['influencer_name']
        filters = {"influencer_name": influencer_name}
        
        if 'year' in params:
            filters['year'] = params['year']
        if 'month_full' in params:
            filters['month'] = params['month_full']

    except KeyError as e:
        say(f"❌ A required parameter was missing from the routing decision: {e}", thread_ts=thread_ts)
        return

    say(f"🔎 Analyzing performance for *{influencer_name}* with filters: `{filters}`...", thread_ts=thread_ts)

    payload = {"source": "influencer_analytics", "filters": filters}
    api_data = query_api(INFLUENCER_API_URL, payload, "Influencer Analytics")

    if "error" in api_data or not api_data.get("campaigns"):
        error_msg = api_data.get("error", f"No campaigns found for '{influencer_name}' with the specified filters.")
        say(f"❌ {error_msg}", thread_ts=thread_ts)
        return

    campaigns = api_data["campaigns"]
    df = pd.DataFrame(campaigns)
    
    total_spend_eur = sum(
        float(c.get('total_budget_clean', 0)) * LOCAL_CURRENCY_TO_EUR_RATE.get(str(c.get('currency', 'EUR')).upper(), 1.0)
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
        prompt = create_influencer_analysis_prompt(influencer_name, campaigns, summary_stats)
        response = gemini_model.generate_content(prompt)
        ai_analysis = response.text

        # Store full context for potential follow-up questions
        thread_context_store[thread_ts] = {
            'type': 'influencer_analysis',
            'influencer_name': influencer_name,
            'filters': filters,
            'raw_api_data': api_data,
            'summary_stats': summary_stats,
            'bot_response': ai_analysis
        }
        # Refresh its position if it already exists
        if thread_ts in thread_context_store:
            thread_context_store.move_to_end(thread_ts)
        logger.success(f"Full context stored for thread {thread_ts}")

        for chunk in split_message_for_slack(ai_analysis):
            say(text=chunk, thread_ts=thread_ts)

    except Exception as e:
        logger.error(f"Error calling Gemini API for influencer analysis: {e}")
        say(f"❌ AI analysis failed: `{str(e)}`", thread_ts=thread_ts)

# --- ✅ 3. THREAD FOLLOW-UP HANDLER ---
def handle_thread_messages(event, say, context):
    """
    Handles follow-up questions in an influencer analysis thread.
    """
    user_message = event.get("text", "").strip()
    thread_ts = event["thread_ts"]
    
    logger.info(f"Handling follow-up for influencer_analysis in thread {thread_ts}")
    
    try:
        context_prompt = f"""
        You are a helpful marketing analyst assistant. A user is asking a follow-up question about an influencer analysis you already provided. Use the following data to answer them.

        **Original Analysis Context:**
        - Influencer Name: {context.get('influencer_name')}
        - Original Filters: {json.dumps(context.get('filters', {}))}
        
        **Your Previous Analysis (for reference):**
        ---
        {context.get('bot_response', 'No previous analysis was stored.')}
        ---

        **Full Raw Data Available (JSON):**
        {json.dumps(context.get('raw_api_data', {}), indent=2)}

        **User's Follow-up Question:** "{user_message}"

        **Instructions:**
        - Answer the user's question directly using only the provided **Full Raw Data**.
        - Your previous analysis is for context, but base your new answer on the raw data for maximum accuracy.
        - Be concise and to the point.
        - If the data needed is not present, state that clearly.
        - Use correct currency formatting when referencing financial data from the 'campaigns' list.
        """
        
        response = gemini_model.generate_content(context_prompt)
        ai_response = response.text

        for chunk in split_message_for_slack(ai_response):
            say(text=chunk, thread_ts=thread_ts)
            
    except Exception as e:
        logger.error(f"Error handling thread message in influencer.py: {e}")
        say(text="❌ Sorry, I had trouble processing your follow-up.", thread_ts=thread_ts)
