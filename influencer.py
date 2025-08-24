import os
import sys
import json
from dotenv import load_dotenv
import requests
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
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
    SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
    SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]
    GOOGLE_API_KEY = os.environ["GOOGLE_API_KEY"]
    app = App(token=SLACK_BOT_TOKEN)
    logger.success("Slack App initialized.")
    genai.configure(api_key=GOOGLE_API_KEY)
    gemini_model = genai.GenerativeModel('gemini-1.5-flash-latest')
    logger.success("Gemini client initialized successfully.")
except KeyError as e:
    logger.critical(f"FATAL: Missing environment variable: {e}. Please check your .env file.")
    sys.exit(1)

# --- Currency Configuration (for formatting and EUR aggregation) ---
# Maps the `market` field from the API to its currency symbol
MARKET_CURRENCY_SYMBOLS = {
    'SWEDEN': 'SEK', 'NORWAY': 'NOK', 'DENMARK': 'DKK',
    'UK': '£', 'FRANCE': '€', 'NORDICS': '€'
}
# Used ONLY for calculating the EUR total in the summary
LOCAL_CURRENCY_TO_EUR_RATE = {
    "EUR": 1.0, "GBP": 1/0.85, "SEK": 1/11.30, "NOK": 1/11.50, "DKK": 1/7.46
}

def format_currency(amount, market):
    """Formats a number with the correct currency symbol based on the market."""
    market_upper = str(market).upper()
    symbol = MARKET_CURRENCY_SYMBOLS.get(market_upper, '€')
    
    # Use integer formatting for Nordic currencies
    if market_upper in ['SWEDEN', 'NORWAY', 'DENMARK']:
        return f"{amount:,.0f} {symbol}"
    else:
        # Use standard two-decimal formatting for others (GBP, EUR)
        return f"{symbol}{amount:,.2f}"

# --- In-Memory Data Storage for Thread Context ---
thread_context_store = {}

# --- API Endpoint Configuration ---
BASE_API_URL = os.getenv("BASE_API_URL", "https://lyra-final.onrender.com")
INFLUENCER_API_URL = f"{BASE_API_URL}/api/influencer/query"
# (Keep other URLs if you still have /monthly-review command)

# --- 2. HELPER FUNCTIONS ---

USER_INPUT_TO_ABBR_MAP = {
    'january': 'Jan', 'february': 'Feb', 'march': 'Mar', 'april': 'Apr',
    'may': 'May', 'june': 'Jun', 'july': 'Jul', 'august': 'Aug',
    'september': 'Sep', 'october': 'Oct', 'november': 'Nov', 'december': 'Dec',
    'jan': 'Jan', 'feb': 'Feb', 'mar': 'Mar', 'apr': 'Apr', 'jun': 'Jun',
    'jul': 'Jul', 'aug': 'Aug', 'sep': 'Sep', 'oct': 'Oct', 'nov': 'Nov', 'dec': 'Dec'
}

def split_message_for_slack(message: str, max_length: int = 2800) -> list:
    """Splits long messages for Slack, preserving code blocks."""
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
    """Generic function to query an API endpoint."""
    logger.info(f"Querying {endpoint_name} API at {url} with payload: {payload}")
    try:
        response = requests.post(url, json=payload, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"{endpoint_name} API Connection Error: {e}")
        return {"error": f"Could not connect to the {endpoint_name} API."}

def create_influencer_analysis_prompt(influencer_name, campaigns, summary_stats):
    """Creates a Gemini prompt for detailed influencer analysis, respecting local currencies."""
    
    # Pre-format the campaign details table for the prompt using local currencies
    campaign_table_rows = []
    for c in campaigns:
        market = c.get('market', 'N/A')
        # Here, we use the market from EACH campaign to format its values
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

    **IMPORTANT INSTRUCTIONS ON CURRENCY:**
    - The "Profile Summary" section provides aggregated totals in EUR for consistent comparison.
    - The "Campaign Breakdown" table shows the performance of EACH campaign in its original LOCAL CURRENCY (e.g., SEK, GBP, EUR).
    - Your analysis must reflect this. When discussing a specific campaign, use its local currency. When discussing overall performance, you can refer to the EUR totals.

    **INFLUENCER DATA:**
    - Name: {influencer_name}
    - Summary Stats (in EUR): {json.dumps(summary_stats, indent=2)}
    - Full Campaign Data (in Local Currencies): {json.dumps(campaigns, indent=2)}

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
        - Mention their efficiency (CAC) and effectiveness (conversion volume), noting any differences between markets.

    3.  **Strengths (Bulleted List):**
        - List 2-3 key strengths, referencing specific campaigns and their local currency performance (e.g., "Excellent efficiency in the Swedish market, achieving a CAC of just 150 SEK in the May campaign.").

    4.  **Areas for Improvement (Bulleted List):**
        - List 1-2 potential weaknesses, again referencing local currency data (e.g., "The UK campaign had a high CAC of £95.50, suggesting lower performance in that market.").

    5.  **Campaign Breakdown (Code Block):**
        ```
        Campaign Details (in Local Currency)
        ========================================================================
        Date       | Market  | Budget      | Conv. | CAC       | CTR
        -----------|---------|-------------|-------|-----------|-------
        {campaign_table_str}
        ```
    """
    return prompt

# --- 3. SLACK COMMAND HANDLERS ---

@app.command("/analyse-influencer")
def handle_analyse_influencer_command(ack, say, command):
    ack()
    text = command.get('text', '').strip()
    
    parts = [p.strip() for p in text.split('-') if p.strip()]
    if not parts:
        say("Format: `/analyse-influencer name - [year] - [month]` (e.g., `/analyse-influencer stylebyanna - 2025 - January`)")
        return
    
    influencer_name = parts[0]
    filters = {"influencer_name": influencer_name}
    
    try:
        if len(parts) > 1: filters['year'] = int(parts[1])
        if len(parts) > 2:
            month_abbr = USER_INPUT_TO_ABBR_MAP.get(parts[2].lower())
            if not month_abbr: return say(f"Invalid month: '{parts[2]}'.")
            filters['month'] = month_abbr
    except ValueError:
        return say("Invalid format. The year must be a number.")

    say(f"🔎 Analyzing performance for *{influencer_name}*...")

    payload = {"source": "influencer_analytics", "filters": filters}
    api_data = query_api(INFLUENCER_API_URL, payload, "Influencer Analytics")

    if "error" in api_data or not api_data.get("campaigns"):
        error_msg = api_data.get("error", f"No campaigns found for '{influencer_name}' with the specified filters.")
        return say(f"❌ {error_msg}")

    campaigns = api_data["campaigns"]
    df = pd.DataFrame(campaigns)
    
    # Calculate aggregated stats in EUR for the summary
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
        prompt = create_influencer_analysis_prompt(influencer_name, campaigns, summary_stats)
        response = gemini_model.generate_content(prompt)
        ai_analysis = response.text

        initial_response = say(f"📊 *Performance Analysis for {influencer_name}*")
        thread_ts = initial_response['ts']

        # Store context for follow-up questions
        thread_context_store[thread_ts] = {
            'type': 'influencer_analysis', 'influencer_name': influencer_name,
            'filters': filters, 'campaigns': campaigns, 'summary_stats': summary_stats
        }

        for chunk in split_message_for_slack(ai_analysis):
            say(text=chunk, thread_ts=thread_ts)

    except Exception as e:
        logger.error(f"Error calling Gemini API for influencer analysis: {e}")
        say(f"❌ AI analysis failed: `{str(e)}`")


# --- THREAD MESSAGE HANDLER FOR FOLLOW-UP QUESTIONS ---
# This remains generic and can handle context from any command
@app.event("message")
def handle_thread_messages(event, say):
    if not event.get("thread_ts") or event.get("bot_id"): return
        
    thread_ts = event["thread_ts"]
    user_message = event.get("text", "").strip()
    
    if thread_ts not in thread_context_store: return
    
    stored_data = thread_context_store[thread_ts]
    
    # Build a generic context prompt that can adapt to different data types
    context_prompt = f"""
    You are a helpful marketing analyst assistant. A user is asking a follow-up question in a Slack thread. Use the following context data to answer them.

    **ORIGINAL ANALYSIS CONTEXT:**
    - Analysis Type: {stored_data.get('type', 'N/A')}
    - Original Filters: {json.dumps(stored_data.get('filters', {}))}
    
    **AVAILABLE DATA (JSON):**
    {json.dumps(stored_data, indent=2)}

    **USER'S FOLLOW-UP QUESTION:** "{user_message}"

    **INSTRUCTIONS:**
    - Answer the user's question directly using the provided JSON data.
    - Be concise and to the point.
    - If you perform a calculation, briefly explain it.
    - If the data needed to answer is not present, state that clearly.
    - Use correct currency formatting if referencing financial data.
    """
    
    try:
        response = gemini_model.generate_content(context_prompt)
        for chunk in split_message_for_slack(response.text):
            say(text=chunk, thread_ts=thread_ts)
    except Exception as e:
        logger.error(f"Error in thread follow-up: {e}")
        say("Sorry, I had trouble processing that follow-up.", thread_ts=thread_ts)


# --- 4. APP EXECUTION ---
if __name__ == "__main__":
    logger.info("🎯 Starting Slack Bot with Influencer Analysis...")
    try:
        handler = SocketModeHandler(app, SLACK_APP_TOKEN)
        logger.success("🚀 Bot is running and connected to Slack!")
        handler.start()
    except Exception as e:
        logger.critical(f"Failed to start the bot: {e}")
        sys.exit(1)
