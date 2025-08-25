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
BASE_API_URL = os.getenv("BASE_API_URL", "https://lyra-final.onrender.com")
TARGET_API_URL = f"{BASE_API_URL}/api/dashboard/targets"
ACTUALS_API_URL = f"{BASE_API_URL}/api/monthly_breakdown"

def get_currency_info(market):
    return MARKET_CURRENCY_CONFIG.get(str(market).upper(), {'rate': 1.0, 'symbol': '€', 'name': 'EUR'})

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
    target_budget_local = target_data.get("kpis", {}).get("total_target_budget", 0)
    metrics = actual_data.get("metrics", {})
    actual_spend_eur = metrics.get("budget_spent_eur", 0)
    actual_spend_local = convert_eur_to_local(actual_spend_eur, market)
    influencers = actual_data.get("influencers", [])
    total_conversions = metrics.get("conversions", 0)
    total_influencers = len(influencers)
    avg_budget_per_influencer_local = actual_spend_local / total_influencers if total_influencers > 0 else 0
    avg_cac_local = actual_spend_local / total_conversions if total_conversions > 0 else 0
    budget_utilization = (actual_spend_local / target_budget_local * 100) if target_budget_local > 0 else 0
    influencer_performance = [{'name': inf.get('name', 'Unknown'), 'budget': inf.get('budget_local', 0), 'conversions': inf.get('conversions', 0), 'cac': inf.get('cac_local', 0)} for inf in influencers]
    performers_with_conversions = [p for p in influencer_performance if p['conversions'] > 0 and p['cac'] > 0]
    performers_with_zero_conversions = [p for p in influencer_performance if p['conversions'] == 0 or p['cac'] == 0]
    best_cac_performer = min(performers_with_conversions, key=lambda x: x['cac']) if performers_with_conversions else None
    worst_performer = None
    if performers_with_zero_conversions:
        worst_performer = max(performers_with_zero_conversions, key=lambda x: x['budget'])
    elif performers_with_conversions:
        worst_performer = max(performers_with_conversions, key=lambda x: x['cac'])
    sorted_by_conversions = sorted(influencer_performance, key=lambda x: x['conversions'], reverse=True)
    most_conversions_performer = sorted_by_conversions[0] if sorted_by_conversions else None
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
        performance_emoji = "⚫"
        if p['conversions'] > 0 and p['cac'] > 0:
            performance_emoji = "🟢" if p['cac'] <= avg_cac_local else ("🟡" if p['cac'] <= avg_cac_local * 1.5 else "🔴")
        cac_display = format_currency(p['cac'], market) if p['conversions'] > 0 and p['cac'] > 0 else 'N/A'
        row = (f"{p['name']:<18} | {format_currency(p['budget'], market):>11} | {p['conversions']:<4} | {cac_display:>8} | {performance_emoji}")
        top_10_table_rows.append(row)
    top_10_table_str = "\n".join(top_10_table_rows)
    prompt = f"""
    You are a performance marketing analyst. Generate a comprehensive and concise monthly performance review for Slack using the data provided. Use code block formatting for clarity.

    **PERFORMANCE DATA FOR {market.upper()} - {month.upper()} {year}:**
    
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
    - Based on the data, what were the key takeaways?
    - Highlight the impact of zero-conversion influencers on overall performance.
    - Was the budget utilized effectively?

    5. **Next Month Recommendations (2-3 points):**
    - Actionable suggestions prioritizing conversion-generating influencers.
    - Recommend pausing/reducing budget for zero-conversion performers.
    """
    return prompt

# --- ✅ 2. CORE LOGIC FUNCTION ---
def run_monthly_review(say, thread_ts, params, thread_context_store):
    """
    Executes the monthly review logic. It receives pre-validated, clean parameters from main.py.
    """
    try:
        # Parameters are received clean from main.py, no need for validation or mapping here.
        market = params['market']
        month_abbr = params['month_abbr']
        month_full = params['month_full']
        year = params['year']
    except KeyError as e:
        say(f"❌ A required parameter was missing from the routing decision: {e}", thread_ts=thread_ts)
        return

    say(f"Generating review for *{market.upper()}* - *{month_full} {year}*...", thread_ts=thread_ts)

    say("Step 1/3: Fetching target data...", thread_ts=thread_ts)
    # The `market` and `month_abbr` are already in the correct case-sensitive format from the router
    target_data = query_api(TARGET_API_URL, {"filters": {"market": market, "month": month_abbr, "year": year}}, "Targets")
    if "error" in target_data:
        say(f"❌ Target API Error: `{target_data['error']}`", thread_ts=thread_ts)
        return
    
    say("Step 2/3: Fetching monthly performance data...", thread_ts=thread_ts)
    # The `market` and `month_full` are also in the correct format
    actual_data = query_api(ACTUALS_API_URL, {"filters": {"market": market, "month": month_full, "year": year}}, "Actuals")
    if "error" in actual_data:
        say(f"❌ Actuals API Error: `{actual_data['error']}`", thread_ts=thread_ts)
        return

    if not actual_data.get("influencers"):
        say(f"✅ **No performance data found** for {market.upper()} {month_full} {year}. No influencers were active in this period.", thread_ts=thread_ts)
        return

    say("Step 3/3: Analyzing data and generating review...", thread_ts=thread_ts)
    try:
        prompt = create_monthly_review_prompt(market, month_full, year, target_data, actual_data)
        response = gemini_model.generate_content(prompt)
        ai_review = response.text
        
        thread_context_store[thread_ts] = {
            'type': 'monthly_review',
            'market': market,
            'month': month_full,
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

        for chunk in split_message_for_slack(ai_review):
            say(text=chunk, thread_ts=thread_ts)
            
    except Exception as e:
        logger.error(f"Error during AI review generation: {e}")
        say(f"❌ An error occurred while generating the AI summary: `{str(e)}`", thread_ts=thread_ts)
    
    logger.success(f"Review completed for {market}-{month_full}-{year}")

# --- ✅ 3. THREAD FOLLOW-UP HANDLER ---
def handle_thread_messages(event, say, context):
    """
    Handles follow-up questions in a monthly review thread.
    """
    user_message = event.get("text", "").strip()
    thread_ts = event["thread_ts"]
    
    logger.info(f"Handling follow-up for monthly_review in thread {thread_ts}")

    try:
        context_prompt = f"""
        You are a helpful marketing analyst assistant. A user is asking a follow-up question about a monthly review you already provided. Use the following data to answer them.

        **Original Review Context:**
        - Market: {context['market'].upper()}
        - Period: {context['month']} {context['year']}
        
        **Available Data (JSON):**
        - Target Data: {json.dumps(context['target_data'])}
        - Actual Performance Data: {json.dumps(context['actual_data'])}

        **User's Follow-up Question:** "{user_message}"

        **Instructions:**
        - Answer the user's question directly using only the provided JSON data.
        - Be concise and to the point.
        - If the data needed is not present, state that clearly.
        - Use correct currency formatting for the market ({context['market'].upper()}).
        """
        
        response = gemini_model.generate_content(context_prompt)
        ai_response = response.text
        
        for chunk in split_message_for_slack(ai_response):
            say(text=chunk, thread_ts=thread_ts)
            
    except Exception as e:
        logger.error(f"Error handling thread message in month.py: {e}")
        say(text="❌ Sorry, I encountered an error processing your follow-up question.", thread_ts=thread_ts)
