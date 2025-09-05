# ================================================
# FILE: trend.py (FINAL - BUGS FIXED)
# ================================================
import os
import sys
import json
import requests
from dotenv import load_dotenv
import google.generativeai as genai
from loguru import logger

# --- 1. CONFIGURATION & INITIALIZATION ---
logger.remove(); logger.add(sys.stderr, format="<yellow>{time:YYYY-MM-DD HH:mm:ss}</yellow> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>", colorize=True)
load_dotenv()
try:
    GOOGLE_API_KEY = os.environ["GOOGLE_API_KEY"]; genai.configure(api_key=GOOGLE_API_KEY)
    model = genai.GenerativeModel('gemini-1.5-flash-latest'); logger.success("Gemini client initialized for trend.py.")
except KeyError as e:
    logger.critical(f"FATAL: Missing GOOGLE_API_KEY. Please check .env file."); sys.exit(1)

# --- CONSTANTS AND HELPERS ---
BASE_API_URL = os.getenv("BASE_API_URL", "http://127.0.0.1:10000"); UNIFIED_API_URL = f"{BASE_API_URL}/api/influencer/query"

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
        logger.error(f"{endpoint_name} API Connection Error: {e}"); return {"error": "I'm sorry, I couldn't connect to the main database at the moment."}

def create_leaderboard_reports(all_influencers, filters):
    reports = {}; filter_str = " | ".join(f"{k.title()}: {v}" for k, v in filters.items() if v)
    by_conversions = sorted(all_influencers, key=lambda x: x.get('total_conversions', 0), reverse=True)[:15]
    conv_table = f"```\n🏆 TOP 15 BY CONVERSIONS ({filter_str})\n" + "Rank | Name                 | Conversions | CAC (€) | Spend (€)\n" + "-"*65 + "\n"
    for i, inf in enumerate(by_conversions, 1):
        conv_table += f"{i:2d} | {inf.get('influencer_name', 'N/A')[:20]:<20} | {int(inf.get('total_conversions', 0)):>11} | {inf.get('effective_cac_eur', 0):>7.2f} | {inf.get('total_spend_eur', 0):>9.2f}\n"
    reports['conversions'] = conv_table + "```"
    with_conv = [x for x in all_influencers if x.get('total_conversions', 0) > 0 and x.get('effective_cac_eur', 0) > 0]
    by_cac = sorted(with_conv, key=lambda x: x.get('effective_cac_eur', float('inf')))[:15]
    cac_table = f"```\n💰 TOP 15 BY CAC (Lowest Cost) ({filter_str})\n" + "Rank | Name                 | CAC (€)   | Conversions\n" + "-"*55 + "\n"
    for i, inf in enumerate(by_cac, 1):
        cac_table += f"{i:2d} | {inf.get('influencer_name', 'N/A')[:20]:<20} | {inf.get('effective_cac_eur', 0):>7.2f} | {int(inf.get('total_conversions', 0)):>11}\n"
    reports['cac'] = cac_table + "```"
    return reports

# --- CORE LOGIC FUNCTION ---
def run_influencer_trend(say, thread_ts, params, thread_context_store, user_query=None):
    filters = {}; data = {}
    try:
        filters = {k: v for k, v in params.items() if k in ['market', 'year', 'month_full', 'tier']}
        if 'month_full' in filters: filters['month'] = filters.pop('month_full')
        
        payload = { "source": "influencer_analytics", "view": "discovery_tiers", "filters": filters }
        data = query_api(UNIFIED_API_URL, payload, "Influencer Trends")
        if "error" in data:
            say(f"{data['error']} Please try again shortly.", thread_ts=thread_ts); return
        
        all_influencers = data.get("items", [])
        if not all_influencers:
            say(f"I couldn't find any trend data for the filters: `{filters}`. You might want to try a broader search.", thread_ts=thread_ts); return
        
        logger.info("Generating full trend leaderboards.")
        leaderboards = create_leaderboard_reports(all_influencers, filters)
        say(f"Of course! Here are the influencer trend leaderboards for your requested filters.", thread_ts=thread_ts)
        for report_text in leaderboards.values():
            say(text=report_text, thread_ts=thread_ts)
        logger.success(f"Trend analysis completed for filters: {filters}")
    except Exception as e:
        logger.error(f"An unexpected error occurred in trend.py: {e}", exc_info=True)
        say(f"I'm sorry, a system error occurred while preparing your trend report.", thread_ts=thread_ts)
    finally:
        thread_context_store[thread_ts] = {'type': 'influencer_trend', 'params': params, 'raw_api_data': data, 'bot_response': "Leaderboard reports were generated."}

# --- THREAD FOLLOW-UP HANDLER ---
def handle_thread_messages(event, say, client, context):
    user_message = event.get("text", "").strip(); thread_ts = event["thread_ts"]
    logger.info(f"Handling follow-up for influencer_trend in thread {thread_ts}")
    try:
        context_prompt = f"""
        You are a helpful marketing analyst assistant.
        **Current Context:** An Influencer Trend report for the filters: **{json.dumps(context.get('params', {}))}**.
        **Available Data:** You have the full JSON data for this specific trend report: {json.dumps(context.get('raw_api_data', {}))}
        
        **User's Follow-up Message:** "{user_message}"
        
        **Your Task - Follow these steps in order:**
        1.  **Analyze and Answer:** Answer the user's question by analyzing the **Available Data** for the current trend report.
        2.  **State Missing Data:** If the question asks for something not in the data, or requires comparing to data outside of the current filters, you MUST state that you don't have that data in your current context. Example: "I can't answer that, as my current context is only for the trend report with filters {json.dumps(context.get('params', {}))}. To see data for a different market or month, please ask me to run a new trend analysis."
        """
        response = model.generate_content(context_prompt)
        ai_response = response.text.strip()
        if not ai_response:
            ai_response = "My apologies, I had trouble formulating a response to that. Could you please rephrase?"
        
        for chunk in split_message_for_slack(ai_response): 
            say(text=chunk, thread_ts=thread_ts)
    except Exception as e:
        logger.error(f"Error handling thread message in trend.py: {e}"); say(text="My apologies, I had trouble processing that follow-up.", thread_ts=thread_ts)
