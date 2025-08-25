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
    model = genai.GenerativeModel('gemini-1.5-flash-latest')
    logger.success("Gemini client initialized for trend.py.")
except KeyError as e:
    logger.critical(f"FATAL: Missing GOOGLE_API_KEY. Please check .env file.")
    sys.exit(1)

# --- CONSTANTS AND HELPERS ---
BASE_API_URL = os.getenv("BASE_API_URL", "https://lyra-final.onrender.com")
CURRENCY_MAP = {
    'SWEDEN': 'SEK', 'NORWAY': 'NOK', 'DENMARK': 'DKK',
    'UK': 'GBP', 'FRANCE': 'EUR'
}

def get_currency_symbol(market):
    """Gets the currency symbol for a given market, defaulting to EUR."""
    return CURRENCY_MAP.get(str(market).upper(), 'EUR')

# --- ✅ 2. CORE LOGIC FUNCTION ---
def run_influencer_trend(say, thread_ts, params, thread_context_store):
    """
    Executes the influencer trend analysis and posts leaderboards to a specific thread.
    """
    filters = {}
    try:
        # All parameters are optional
        if market := params.get('market', '').strip():
            filters['market'] = market
        if year := params.get('year', '').strip():
            filters['year'] = int(year)
        if month := params.get('month', '').strip():
            filters['month'] = month.capitalize()
        if tier := params.get('tier', '').strip():
            filters['tier'] = tier.lower()
    except (ValueError, AttributeError) as e:
        say(f"❌ There was an issue with the parameters for the trend analysis. Error: {e}", thread_ts=thread_ts)
        return

    say(f"🔎 Fetching influencer trend data with filters: `{filters}`...", thread_ts=thread_ts)
    
    url = f"{BASE_API_URL}/api/influencer/query"
    payload = {
        "source": "influencer_analytics",
        "view": "discovery_tiers", 
        "filters": filters
    }
    
    try:
        response = requests.post(url, json=payload, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        all_influencers = []
        if data.get("source") == "discovery_tier_specific":
            all_influencers = data.get("items", [])
        else:
            for tier_name, tier_data in data.items():
                if isinstance(tier_data, list):
                    all_influencers.extend(tier_data)
        
        if not all_influencers:
            say("❌ No trend data found for the specified filters.", thread_ts=thread_ts)
            return
        
        say(f"📊 Found **{len(all_influencers)}** influencers. Compiling leaderboards...", thread_ts=thread_ts)
        
        thread_context_store[thread_ts] = {
            'type': 'influencer_trend',
            'filters': filters,
            'data': all_influencers
        }
        
        currency = get_currency_symbol(filters.get('market', 'FRANCE'))
        
        # Best by Conversions
        by_conversions = sorted(all_influencers, key=lambda x: x.get('total_conversions', 0), reverse=True)[:25]
        conv_table = "```\nTOP 25 INFLUENCERS BY CONVERSIONS\n"
        conv_table += f"Rank | Name                    | Conversions | CAC ({currency})  | Spend ({currency})\n"
        conv_table += "-" * 75 + "\n"
        for i, inf in enumerate(by_conversions, 1):
            name = inf.get('influencer_name', 'N/A')[:20]
            conv = inf.get('total_conversions', 0)
            cac = inf.get('effective_cac_eur', 0)
            spend = inf.get('total_spend_eur', 0)
            conv_table += f"{i:2d}   | {name:<20} | {conv:8.0f}    | {cac:8.2f}   | {spend:10.2f}\n"
        conv_table += "```"
        say(text=conv_table, thread_ts=thread_ts)
        
        # Best by CAC
        with_conversions = [x for x in all_influencers if x.get('total_conversions', 0) > 0 and x.get('effective_cac_eur', 0) > 0]
        if with_conversions:
            by_cac = sorted(with_conversions, key=lambda x: x.get('effective_cac_eur', float('inf')))[:15]
            cac_table = "```\nTOP 15 INFLUENCERS BY CAC (Lowest is Best)\n"
            cac_table += f"Rank | Name                    | CAC ({currency})  | Conversions\n"
            cac_table += "-" * 60 + "\n"
            for i, inf in enumerate(by_cac, 1):
                name = inf.get('influencer_name', 'N/A')[:20]
                cac = inf.get('effective_cac_eur', 0)
                conv = inf.get('total_conversions', 0)
                cac_table += f"{i:2d}   | {name:<20} | {cac:8.2f}   | {conv:8.0f}\n"
            cac_table += "```"
            say(text=cac_table, thread_ts=thread_ts)

        # WORST PERFORMERS
        zero_conv = [x for x in all_influencers if x.get('total_conversions', 0) == 0]
        worst_by_spend = sorted(zero_conv, key=lambda x: x.get('total_spend_eur', 0), reverse=True)[:15]
        if worst_by_spend:
            worst_table = "```\nTOP 15 BUDGET WASTE (0 Conversions)\n"
            worst_table += f"Rank | Name                    | Spend ({currency}) | Views\n"
            worst_table += "-" * 60 + "\n"
            for i, inf in enumerate(worst_by_spend, 1):
                name = inf.get('influencer_name', 'N/A')[:20]
                spend = inf.get('total_spend_eur', 0)
                views = inf.get('total_views', 0)
                worst_table += f"{i:2d}   | {name:<20} | {spend:9.2f}   | {views:8.0f}\n"
            worst_table += "```"
            say(text=worst_table, thread_ts=thread_ts)
        
        prompt = f"""
        Analyze this influencer trend data for {filters.get('market', 'Unknown market')}.
        Total Influencers Found: {len(all_influencers)}
        Best by Conversions: {by_conversions[0]['influencer_name']} ({by_conversions[0]['total_conversions']} conversions)
        Best by CAC: {by_cac[0]['influencer_name']} ({currency}{by_cac[0]['effective_cac_eur']:.2f}) if with_conversions else 'N/A'
        
        Provide a 2-3 sentence executive summary highlighting the most important finding, followed by one key recommendation.
        """
        
        ai_response = model.generate_content(prompt)
        say(text=f"🧠 **AI Executive Summary:**\n{ai_response.text}", thread_ts=thread_ts)
        
    except requests.exceptions.RequestException as e:
        logger.error(f"API Error in trend.py: {e}")
        say(f"❌ API Connection Error: Could not fetch trend data.", thread_ts=thread_ts)
    except Exception as e:
        logger.error(f"An unexpected error occurred in trend.py: {e}", exc_info=True)
        say(f"❌ An unexpected error occurred: {str(e)}", thread_ts=thread_ts)

# --- ✅ 3. THREAD FOLLOW-UP HANDLER (Placeholder) ---
def handle_thread_messages(event, say, context):
    """
    Placeholder for handling follow-up questions in a trend analysis thread.
    """
    thread_ts = event["thread_ts"]
    say(
        text="I'm sorry, I don't support follow-up questions for trend reports just yet. Please start a new request by mentioning me.",
        thread_ts=thread_ts
    )
