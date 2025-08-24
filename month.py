import os
import sys
import json
from dotenv import load_dotenv
import requests
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
import google.generativeai as genai
from loguru import logger

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

# --- Market-Specific Currency Configuration ---
MARKET_CURRENCY_CONFIG = {
    'SWEDEN':  {'rate': 11.30, 'symbol': 'SEK', 'name': 'SEK'},
    'NORWAY':  {'rate': 11.50, 'symbol': 'NOK', 'name': 'NOK'},
    'DENMARK': {'rate': 7.46,  'symbol': 'DKK', 'name': 'DKK'},
    'UK':      {'rate': 0.85,  'symbol': '£',   'name': 'GBP'},
    'FRANCE':  {'rate': 1.0,   'symbol': '€',   'name': 'EUR'},
}

def get_currency_info(market):
    """Get currency conversion rate and symbol for a market, defaulting to EUR."""
    return MARKET_CURRENCY_CONFIG.get(market.upper(), {'rate': 1.0, 'symbol': '€', 'name': 'EUR'})

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

# --- In-Memory Data Storage ---
review_data_store = {}

# --- API Endpoint Configuration ---
BASE_API_URL = os.getenv("BASE_API_URL", "https://lyra-final.onrender.com")
TARGET_API_URL = f"{BASE_API_URL}/api/dashboard/targets"
ACTUALS_API_URL = f"{BASE_API_URL}/api/monthly_breakdown"

# --- 2. HELPER FUNCTIONS ---

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

def split_message_for_slack(message: str, max_length: int = 2800) -> list:
    """Split long messages into chunks that fit within Slack's limits, preserving code blocks."""
    if len(message) <= max_length:
        return [message]
    
    chunks, current_chunk, in_code_block = [], "", False
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
            current_chunk = "```\n" + line + "\n" if in_code_block else line + "\n"
        else:
            current_chunk += line + "\n"
    
    if current_chunk.strip():
        chunks.append(current_chunk.strip())
    
    return chunks

def query_api(url: str, payload: dict, endpoint_name: str) -> dict:
    logger.info(f"Querying {endpoint_name} API at {url}")
    try:
        response = requests.post(url, json=payload, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"{endpoint_name} API Connection Error: {e}")
        return {"error": f"Could not connect to the {endpoint_name} API. Please check if the service is running."}

def create_monthly_review_prompt(market, month, year, target_data, actual_data):
    """Create LLM prompt for monthly performance review with market-specific currency and improved CAC logic."""
    
    # --- Data Extraction ---
    target_budget_local = target_data.get("kpis", {}).get("total_target_budget", 0)  # Already in local currency
    metrics = actual_data.get("metrics", {})
    actual_spend_eur = metrics.get("budget_spent_eur", 0)
    actual_spend_local = convert_eur_to_local(actual_spend_eur, market)
    influencers = actual_data.get("influencers", [])
    total_conversions = metrics.get("conversions", 0)
    total_influencers = len(influencers)

    # --- Metric Calculation in Local Currency ---
    avg_budget_per_influencer_local = actual_spend_local / total_influencers if total_influencers > 0 else 0
    avg_cac_local = actual_spend_local / total_conversions if total_conversions > 0 else 0
    budget_utilization = (actual_spend_local / target_budget_local * 100) if target_budget_local > 0 else 0

    # --- Influencer Performance Analysis ---
    influencer_performance = [{'name': inf.get('name', 'Unknown'), 'budget': inf.get('budget_local', 0),
                               'conversions': inf.get('conversions', 0), 'cac': inf.get('cac_local', 0)}
                              for inf in influencers]

    # --- UPDATED LOGIC: Identify Top/Worst Performers ---
    performers_with_conversions = [p for p in influencer_performance if p['conversions'] > 0 and p['cac'] > 0]
    performers_with_zero_conversions = [p for p in influencer_performance if p['conversions'] == 0 or p['cac'] == 0]

    # Best CAC: Only consider influencers with non-zero CAC (i.e., those who actually converted)
    best_cac_performer = min(performers_with_conversions, key=lambda x: x['cac']) if performers_with_conversions else None
    
    # Worst performer: Prioritize zero conversions/CAC as worst performance
    worst_performer = None
    if performers_with_zero_conversions:
        # Among zero conversion performers, pick the one who wasted the most budget
        worst_performer = max(performers_with_zero_conversions, key=lambda x: x['budget'])
    elif performers_with_conversions:
        # If everyone converted, the worst is the highest CAC
        worst_performer = max(performers_with_conversions, key=lambda x: x['cac'])

    sorted_by_conversions = sorted(influencer_performance, key=lambda x: x['conversions'], reverse=True)
    most_conversions_performer = sorted_by_conversions[0] if sorted_by_conversions else None

    # --- Build formatted strings for the prompt ---
    best_cac_str = f"{best_cac_performer['name']} - {format_currency(best_cac_performer['cac'], market)}" if best_cac_performer else 'N/A (No conversions)'
    most_conv_str = f"{most_conversions_performer['name']} - {most_conversions_performer['conversions']} conversions" if most_conversions_performer else 'N/A'
    
    worst_performer_str = 'N/A'
    if worst_performer:
        if worst_performer['conversions'] == 0 or worst_performer['cac'] == 0:
            worst_performer_str = f"{worst_performer['name']} (0 conv. for {format_currency(worst_performer['budget'], market)})"
        else:
            worst_performer_str = f"{worst_performer['name']} - {format_currency(worst_performer['cac'], market)}"

    top_10_table_rows = []
    for p in sorted_by_conversions[:10]:
        performance_emoji = "⚫" # Default for zero conversions/CAC
        if p['conversions'] > 0 and p['cac'] > 0:
            performance_emoji = "🟢" if p['cac'] <= avg_cac_local else ("🟡" if p['cac'] <= avg_cac_local * 1.5 else "🔴")
        
        cac_display = format_currency(p['cac'], market) if p['conversions'] > 0 and p['cac'] > 0 else 'N/A'
        row = (f"{p['name']:<18} | {format_currency(p['budget'], market):>11} | {p['conversions']:<4} | "
               f"{cac_display:>8} | {performance_emoji}")
        top_10_table_rows.append(row)
    top_10_table_str = "\n".join(top_10_table_rows)

    # --- Construct the Final Prompt ---
    prompt = f"""
    You are a performance marketing analyst. Generate a comprehensive and concise monthly performance review for Slack using the data provided. Use code block formatting for clarity.

    **PERFORMANCE DATA FOR {market.upper()} - {month.upper()} {year}:**
    
    **IMPORTANT CONTEXT FOR ANALYSIS:**
    - Zero CAC means zero conversions, which represents the WORST performance (wasted budget)
    - Best CAC should only consider influencers who actually generated conversions (non-zero CAC)
    - Worst performers are those with zero conversions who consumed budget without results
    
    **FORMAT REQUIREMENTS:**
    Generate a crisp, actionable response with these sections:

    1. **Performance Summary (Code Block):**
    ```
    Monthly Performance Review - {market.upper()} {month.upper()} {year}
    ================================================================
    Target Budget:        {format_currency(target_budget_local, market)}
    Actual Spend:         {format_currency(actual_spend_local, market)} ({budget_utilization:.1f}%)
    Total Conversions:    {total_conversions}
    Total Influencers:    {total_influencers}
    Average CAC:          {format_currency(avg_cac_local, market)}
    Avg Budget/Influencer:{format_currency(avg_budget_per_influencer_local, market)}
    ```

    2. **Performance Highlights & Areas for Improvement (Code Block):**
    ```
    Key Performers
    ================================================================
    🏆 Best CAC:         {best_cac_str}
    💰 Most Conversions:  {most_conv_str}
    ⚠️  Worst Performer:  {worst_performer_str}
    ```
    
    3. **Top 10 Influencers Performance Table (Code Block):**
    ```
    Top 10 Influencer Performance (by Conversions)
    ================================================================
    Name               | Budget      | Conv | CAC      | Performance
    -------------------|-------------|------|----------|-------------
    {top_10_table_str}
    ```

    4. **Key Learnings (3-4 bullet points):**
    - Based on the data, what were the key takeaways? Focus on conversion efficiency vs budget waste.
    - Highlight the impact of zero-conversion influencers on overall performance.
    - Was the budget utilized effectively? Consider performers with zero conversions as worst performance.
    - What patterns can be seen in converting vs non-converting influencers?

    5. **Next Month Recommendations (2-3 points):**
    - Actionable suggestions prioritizing conversion-generating influencers.
    - Recommend pausing/reducing budget for zero-conversion performers.
    - Suggest reallocating budget from worst performers (zero conversions) to best CAC performers.

    Keep the language professional, concise, and data-driven. Remember: Zero conversions = worst performance, regardless of spend.
    """
    return prompt

# --- 3. SLACK COMMAND HANDLER ---

@app.command("/monthly-review")
def handle_monthly_review_command(ack, say, command):
    ack()
    text = command.get('text', '').strip()
    
    parts = text.split('-')
    if len(parts) != 3:
        say("Format: `/monthly-review Market-Month-Year` (e.g., `/monthly-review Sweden-December-2025`)")
        return

    try:
        market, raw_month_input, year = parts[0].strip(), parts[1].strip(), int(parts[2].strip())
        month_abbr = USER_INPUT_TO_ABBR_MAP.get(raw_month_input.lower())
        if not month_abbr:
            say(f"Invalid month '{raw_month_input}'. Use full name or 3-letter abbreviation.")
            return
        month_full = ABBR_TO_FULL_MONTH_MAP.get(month_abbr)
        say(f"Generating review for *{market.upper()}* - *{raw_month_input.capitalize()} {year}*...")
    except (ValueError, IndexError):
        say("Invalid format. Use `/monthly-review Market-Month-Year`")
        return

    say("Step 1/3: Fetching target data...")
    target_data = query_api(TARGET_API_URL, {"filters": {"market": market, "month": month_abbr, "year": year}}, "Targets")
    if "error" in target_data: return say(f"❌ Target API Error: `{target_data['error']}`")
    
    say("Step 2/3: Fetching monthly performance data...")
    actual_data = query_api(ACTUALS_API_URL, {"filters": {"market": market, "month": month_full, "year": year}}, "Actuals")
    if "error" in actual_data: return say(f"❌ Actuals API Error: `{actual_data['error']}`")

    if not actual_data.get("influencers"):
        return say(f"✅ **No performance data found** for {market.upper()} {raw_month_input.capitalize()} {year}. No influencers were active.")

    say("Step 3/3: Analyzing performance and generating review...")
    try:
        prompt = create_monthly_review_prompt(market, raw_month_input, year, target_data, actual_data)
        response = gemini_model.generate_content(prompt)
        ai_review = response.text
        
        # Send initial response in thread
        initial_response = say(text=f"📊 **Monthly Review - {market.upper()} {raw_month_input.capitalize()} {year}**")
        thread_ts = initial_response['ts']
        
        # Store data in memory for follow-up questions
        review_key = f"{market.lower()}_{raw_month_input.lower()}_{year}"
        review_data_store[thread_ts] = {
            'key': review_key,
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
        
        message_chunks = split_message_for_slack(ai_review)
        
        for chunk in message_chunks:
            say(text=chunk, thread_ts=thread_ts)
            
    except Exception as e:
        logger.error(f"Error calling Gemini API: {e}")
        say(f"❌ AI review generation failed: `{str(e)}`")
    
    logger.success(f"Review completed for {market}-{raw_month_input}-{year}")

# --- THREAD MESSAGE HANDLER FOR FOLLOW-UP QUESTIONS ---

@app.event("message")
def handle_thread_messages(event, say):
    """Handle follow-up questions in review threads using stored data context."""
    
    # Only respond to messages in threads, not DMs or main channel
    if not event.get("thread_ts"):
        return
    
    # Skip bot messages
    if event.get("bot_id"):
        return
        
    thread_ts = event["thread_ts"]
    user_message = event.get("text", "").strip()
    
    # Check if we have stored data for this thread
    if thread_ts not in review_data_store:
        return  # Not a review thread we created
    
    stored_data = review_data_store[thread_ts]
    
    try:
        # Create context-aware prompt for follow-up questions
        context_prompt = f"""
        You are a performance marketing analyst assistant. A user is asking follow-up questions about a monthly review you previously generated.

        **CONTEXT DATA:**
        Market: {stored_data['market'].upper()}
        Month: {stored_data['month'].capitalize()} {stored_data['year']}
        
        **SUMMARY METRICS:**
        - Target Budget: {format_currency(stored_data['metrics']['target_budget_local'], stored_data['market'])}
        - Actual Spend: {format_currency(stored_data['metrics']['actual_spend_local'], stored_data['market'])}
        - Total Conversions: {stored_data['metrics']['total_conversions']}
        - Total Influencers: {stored_data['metrics']['total_influencers']}
        
        **AVAILABLE DETAILED DATA:**
        - Target Data: {json.dumps(stored_data['target_data'], indent=2)}
        - Performance Data: {json.dumps(stored_data['actual_data'], indent=2)}

        **USER QUESTION:** "{user_message}"

        **INSTRUCTIONS:**
        - Answer the user's question using the available data
        - Be specific and reference actual numbers/names from the data
        - Keep responses concise but informative
        - If the question requires analysis not possible with available data, explain what's missing
        - Use proper currency formatting for the market
        - Reference specific influencer names when relevant

        Provide a helpful, data-driven response:
        """
        
        response = gemini_model.generate_content(context_prompt)
        ai_response = response.text
        
        # Split response if too long
        message_chunks = split_message_for_slack(ai_response)
        
        for chunk in message_chunks:
            say(text=chunk, thread_ts=thread_ts)
            
        logger.info(f"Responded to follow-up question in thread {thread_ts}")
        
    except Exception as e:
        logger.error(f"Error handling thread message: {e}")
        say(text="❌ Sorry, I encountered an error processing your question. Please try rephrasing.", thread_ts=thread_ts)

# --- 4. APP EXECUTION ---
if __name__ == "__main__":
    logger.info("🎯 Starting Monthly Review Slack Bot...")
    try:
        handler = SocketModeHandler(app, SLACK_APP_TOKEN)
        logger.success("🚀 Bot is running and connected to Slack!")
        handler.start()
    except Exception as e:
        logger.critical(f"Failed to start the bot: {e}")
        sys.exit(1)
