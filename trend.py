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
    filters = {}
    if 'market' in params: filters['market'] = params['market']
    if 'year' in params: filters['year'] = params['year']
    if 'month_full' in params: filters['month'] = params['month_full']
    if 'tier' in params: filters['tier'] = params['tier']

    say(f"🔎 Fetching influencer trend data with filters: `{filters}`...", thread_ts=thread_ts)
    
    url = f"{BASE_API_URL}/api/influencer/query"
    payload = { "source": "influencer_analytics", "view": "discovery_tiers", "filters": filters }
    
    try:
        response = requests.post(url, json=payload, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        all_influencers = []
        if data.get("source") == "discovery_tier_specific":
            all_influencers = data.get("items", [])
        else:
            for tier_name in ["gold", "silver", "bronze"]:
                if isinstance(data.get(tier_name), list):
                    all_influencers.extend(data[tier_name])
        
        if not all_influencers:
            say("❌ No trend data found for the specified filters.", thread_ts=thread_ts)
            return
        
        say(f"📊 Found **{len(all_influencers)}** influencers. Compiling leaderboards...", thread_ts=thread_ts)
        
        bot_response_parts = {}

        # --- 1. Best by Conversions (Top 25) ---
        by_conversions = sorted(all_influencers, key=lambda x: x.get('total_conversions', 0), reverse=True)[:25]
        conv_table = "```\nTOP 25 INFLUENCERS BY CONVERSIONS\n"
        conv_table += f"Rank | Name                    | Conversions | CAC (€)      | Spend (€)\n"
        conv_table += "-" * 75 + "\n"
        for i, inf in enumerate(by_conversions, 1):
            name = inf.get('influencer_name', 'N/A')[:20]
            conv = inf.get('total_conversions', 0)
            cac = inf.get('effective_cac_eur', 0)
            spend = inf.get('total_spend_eur', 0)
            conv_table += f"{i:2d}   | {name:<20} | {conv:8.0f}    | {cac:8.2f}   | {spend:10.2f}\n"
        conv_table += "```"
        bot_response_parts['conversions_leaderboard'] = conv_table
        for chunk in split_message_for_slack(conv_table):
            say(text=chunk, thread_ts=thread_ts)
        
        # --- 2. Best by CAC (Top 15) ---
        with_conversions = [x for x in all_influencers if x.get('total_conversions', 0) > 0 and x.get('effective_cac_eur', 0) > 0]
        by_cac = sorted(with_conversions, key=lambda x: x.get('effective_cac_eur', float('inf')))[:15]
        cac_table = "```\nBEST 15 INFLUENCERS BY CAC (Lowest Cost, Non-Zero Only)\n"
        cac_table += f"Rank | Name                    | CAC (€)    | Conversions | CTR      | CVR\n"
        cac_table += "-" * 75 + "\n"
        for i, inf in enumerate(by_cac, 1):
            name = inf.get('influencer_name', 'N/A')[:20]
            cac = inf.get('effective_cac_eur', 0)
            conv = inf.get('total_conversions', 0)
            ctr = inf.get('avg_ctr', 0) * 100
            cvr = inf.get('avg_cvr', 0) * 100
            cac_table += f"{i:2d}   | {name:<20} | {cac:8.2f}   | {conv:8.0f}    | {ctr:6.3f}%  | {cvr:6.3f}%\n"
        cac_table += "```"
        bot_response_parts['cac_leaderboard'] = cac_table
        for chunk in split_message_for_slack(cac_table):
            say(text=chunk, thread_ts=thread_ts)
        
        # --- 3. Best by CTR (Top 15) ---
        by_ctr = sorted(all_influencers, key=lambda x: x.get('avg_ctr', 0), reverse=True)[:15]
        ctr_table = "```\nBEST 15 INFLUENCERS BY CTR (Click-Through Rate)\n"
        ctr_table += "Rank | Name                    | CTR      | Views      | Clicks   | Conversions\n"
        ctr_table += "-" * 80 + "\n"
        for i, inf in enumerate(by_ctr, 1):
            name = inf.get('influencer_name', 'N/A')[:20]
            ctr = inf.get('avg_ctr', 0) * 100
            views = inf.get('total_views', 0)
            clicks = inf.get('total_clicks', 0)
            conv = inf.get('total_conversions', 0)
            ctr_table += f"{i:2d}   | {name:<20} | {ctr:6.3f}%  | {views:8.0f}   | {clicks:6.0f}   | {conv:8.0f}\n"
        ctr_table += "```"
        bot_response_parts['ctr_leaderboard'] = ctr_table
        for chunk in split_message_for_slack(ctr_table):
            say(text=chunk, thread_ts=thread_ts)

        # --- 4. Best by Video Views (Top 15) ---
        by_views = sorted(all_influencers, key=lambda x: x.get('total_views', 0), reverse=True)[:15]
        views_table = "```\nBEST 15 INFLUENCERS BY VIDEO VIEWS\n"
        views_table += f"Rank | Name                    | Views      | CTR      | Conversions | Spend (€)\n"
        views_table += "-" * 85 + "\n"
        for i, inf in enumerate(by_views, 1):
            name = inf.get('influencer_name', 'N/A')[:20]
            views = inf.get('total_views', 0)
            ctr = inf.get('avg_ctr', 0) * 100
            conv = inf.get('total_conversions', 0)
            spend = inf.get('total_spend_eur', 0)
            views_table += f"{i:2d}   | {name:<20} | {views:8.0f}   | {ctr:6.3f}%  | {conv:8.0f}    | {spend:10.2f}\n"
        views_table += "```"
        bot_response_parts['views_leaderboard'] = views_table
        for chunk in split_message_for_slack(views_table):
            say(text=chunk, thread_ts=thread_ts)
        
        # --- 5. Worst Performers (Top 15 by spend with 0 conversions) ---
        zero_conv = [x for x in all_influencers if x.get('total_conversions', 0) == 0]
        worst_by_spend = sorted(zero_conv, key=lambda x: x.get('total_spend_eur', 0), reverse=True)[:15]
        if worst_by_spend:
            worst_table = "```\nWORST 15 PERFORMERS (Zero Conversions, Sorted by Spend)\n"
            worst_table += f"Rank | Name                    | Spend (€)  | Views      | Clicks   | CTR\n"
            worst_table += "-" * 80 + "\n"
            for i, inf in enumerate(worst_by_spend, 1):
                name = inf.get('influencer_name', 'N/A')[:20]
                spend = inf.get('total_spend_eur', 0)
                views = inf.get('total_views', 0)
                clicks = inf.get('total_clicks', 0)
                ctr = inf.get('avg_ctr', 0) * 100
                worst_table += f"{i:2d}   | {name:<20} | {spend:9.2f}   | {views:8.0f}   | {clicks:6.0f}   | {ctr:6.3f}%\n"
            worst_table += "```"
            bot_response_parts['worst_performers_leaderboard'] = worst_table
            for chunk in split_message_for_slack(worst_table):
                say(text=chunk, thread_ts=thread_ts)
        
        # --- 6. AI Executive Summary ---
        total_wasted_spend = sum(inf.get('total_spend_eur', 0) for inf in zero_conv)
        prompt = f"""
        You are an expert marketing analyst. Based on this influencer performance data for {filters.get('market', 'all markets')}:
        
        - Total Influencers Analyzed: {len(all_influencers)}
        - Top Performer (Conversions): {by_conversions[0]['influencer_name']} with {by_conversions[0]['total_conversions']} conversions.
        - Most Cost-Effective (CAC): {by_cac[0]['influencer_name'] if by_cac else 'N/A'} with a CAC of €{by_cac[0]['effective_cac_eur']:.2f} if by_cac else 'N/A'.
        - Budget Waste: {len(zero_conv)} influencers generated 0 conversions, wasting a total of €{total_wasted_spend:,.2f}.
        
        Provide a concise, data-driven executive summary with the following structure:
        1.  **Overall Summary:** A 2-sentence overview of the performance.
        2.  **Key Insight:** What is the most important finding regarding conversion efficiency or inefficiency?
        3.  **Red Flag:** What is the biggest warning sign from this data (e.g., budget waste)?
        4.  **Actionable Recommendation:** What is the single most important action to take based on these findings?
        """
        
        ai_response = model.generate_content(prompt)
        summary_text = f"🧠 **AI EXECUTIVE SUMMARY:**\n{ai_response.text}"
        bot_response_parts['ai_summary'] = summary_text
        for chunk in split_message_for_slack(summary_text):
            say(text=chunk, thread_ts=thread_ts)
        
        # Storing FULL context
        thread_context_store[thread_ts] = {
            'type': 'influencer_trend',
            'filters': filters,
            'raw_api_data': data,
            'bot_response': bot_response_parts
        }
        # Refresh its position if it already exists
        if thread_ts in thread_context_store:
            thread_context_store.move_to_end(thread_ts)
        logger.success(f"Full context stored for thread {thread_ts}")

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
        
        **Your Previous Analysis (for reference):**
        ---
        {json.dumps(context.get('bot_response', {}), indent=2)}
        ---

        **Full Raw Data Available (JSON):**
        {json.dumps(context.get('raw_api_data', {}), indent=2)}

        **User's Follow-up Question:** "{user_message}"

        **Instructions:**
        - Answer the user's question directly using the **Full Raw Data**.
        - Your previous analysis is for context, but base your new answer on the raw data for maximum accuracy.
        - If the user asks for something that was in your original summary (e.g., "who was the top performer?"), you can use the 'bot_response' data to answer quickly.
        - If they ask a new question that requires calculation (e.g., "what's the average spend for gold tier?"), compute it from the 'raw_api_data'.
        - Be concise and to the point.
        """
        
        response = model.generate_content(context_prompt)
        ai_response = response.text

        for chunk in split_message_for_slack(ai_response):
            say(text=chunk, thread_ts=thread_ts)
            
    except Exception as e:
        logger.error(f"Error handling thread message in trend.py: {e}")
        say(text="❌ Sorry, I had trouble processing your follow-up.", thread_ts=thread_ts)
