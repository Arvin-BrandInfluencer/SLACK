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
logger.remove()
logger.add(sys.stderr, format="<yellow>{time:YYYY-MM-DD HH:mm:ss}</yellow> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>", colorize=True)

load_dotenv()
try:
    GOOGLE_API_KEY = os.environ["GOOGLE_API_KEY"]
    genai.configure(api_key=GOOGLE_API_KEY)
    gemini_model = genai.GenerativeModel('gemini-1.5-flash-latest')
    logger.success("Gemini client initialized for plan.py.")
except KeyError as e:
    logger.critical(f"FATAL: Missing GOOGLE_API_KEY. Please check .env file.")
    sys.exit(1)

# --- CONSTANTS AND HELPERS ---
BASE_API_URL = os.getenv("BASE_API_URL", "https://lyra-final.onrender.com")
TARGET_API_URL = f"{BASE_API_URL}/api/dashboard/targets"
ACTUALS_API_URL = f"{BASE_API_URL}/api/monthly_breakdown"
DISCOVERY_API_URL = f"{BASE_API_URL}/api/discovery"

MARKET_CURRENCY_CONFIG = { 'SWEDEN': {'rate': 11.30, 'symbol': 'SEK', 'name': 'SEK'}, 'NORWAY': {'rate': 11.50, 'symbol': 'NOK', 'name': 'NOK'}, 'DENMARK': {'rate': 7.46, 'symbol': 'DKK', 'name': 'DKK'}, 'UK': {'rate': 0.85, 'symbol': '£', 'name': 'GBP'}, 'FRANCE': {'rate': 1.0, 'symbol': '€', 'name': 'EUR'}, }

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
    chunks = []
    current_chunk = ""
    in_code_block = False
    lines = message.split('\n')
    for line in lines:
        if line.strip().startswith('```'): in_code_block = not in_code_block
        if len(current_chunk) + len(line) + 1 > max_length:
            if in_code_block and current_chunk.strip():
                current_chunk += "\n```"
                in_code_block = False
            if current_chunk.strip(): chunks.append(current_chunk.strip())
            if in_code_block:
                current_chunk = "```\n" + line + "\n"
            else:
                current_chunk = line + "\n"
        else:
            current_chunk += line + "\n"
    if current_chunk.strip(): chunks.append(current_chunk.strip())
    return chunks

def query_api(url: str, payload: dict, endpoint_name: str) -> dict:
    logger.info(f"Querying {endpoint_name} API at {url} with payload: {json.dumps(payload)}")
    try:
        response = requests.post(url, json=payload, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"{endpoint_name} API Connection Error: {e}")
        return {"error": f"Could not connect to the {endpoint_name} API."}

def fetch_tier_influencers(market, year, tier, booked_influencer_names):
    discovery_payload = { "filters": {"market": market, "year": year, "tier": tier} }
    discovery_data = query_api(DISCOVERY_API_URL, discovery_payload, f"Discovery-{tier.capitalize()}")
    if "error" in discovery_data:
        logger.error(f"Error fetching {tier} tier: {discovery_data['error']}")
        return []
    all_influencers_in_tier = discovery_data.get("influencers", [])
    unbooked = [inf for inf in all_influencers_in_tier if inf.get('influencerName') not in booked_influencer_names]
    logger.info(f"Found {len(unbooked)} unbooked {tier.capitalize()}-tier influencers")
    return unbooked

def allocate_budget_cascading_tiers(gold, silver, bronze, budget, cac=50, market='France'):
    recs, allocated = [], 0
    tier_breakdown = {'Gold': [], 'Silver': [], 'Bronze': []}
    tiers_data = [('Gold', gold), ('Silver', silver), ('Bronze', bronze)]
    for name, influencers in tiers_data:
        if allocated >= budget * 0.98: break
        sorted_inf = sorted(influencers, key=lambda x: x.get('averageSpendPerCampaign', 0))
        for inf in sorted_inf:
            spend = inf.get('averageSpendPerCampaign', 0)
            if allocated + spend <= budget:
                pred_conv = int(spend / cac) if cac > 0 else 0
                rec = {'influencer_name': inf.get('influencerName', 'Unknown'), 'allocated_budget': spend, 'predicted_conversions': pred_conv, 'effective_cac': cac, 'tier': name, 'market': market}
                recs.append(rec)
                tier_breakdown[name].append(rec)
                allocated += spend
                if allocated >= budget * 0.98: break
    return recs, allocated, tier_breakdown

def create_excel_report(recs, tier_breakdown, market, month, year, target_budget, actual_spend, remaining_budget, total_allocated, booked_influencers):
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
        pd.DataFrame({'Metric': ['Target Budget', 'Actual Spend', 'Remaining Budget', 'Recommended Allocation'], 'Amount': [format_currency(target_budget, market), format_currency(actual_spend, market), format_currency(remaining_budget, market), format_currency(total_allocated, market)]}).to_excel(writer, sheet_name='Budget Summary', index=False)
        if recs: pd.DataFrame(recs).to_excel(writer, sheet_name='All Recommendations', index=False)
        if booked_influencers: pd.DataFrame([{'Influencer Name': inf.get('name', 'Unknown'), 'Spent Budget': format_currency(inf.get('budget_local', 0), market)} for inf in booked_influencers]).to_excel(writer, sheet_name='Booked Influencers', index=False)
    buffer.seek(0)
    return buffer

def create_llm_prompt_with_code_blocks(market, month, year, target_budget, actual_spend, remaining_budget,
                                     booked_influencers, recommendations, total_allocated, tier_breakdown):
    """Generates the enhanced, multi-table AI prompt."""
    total_recs = len(recommendations)
    total_conv = sum(rec['predicted_conversions'] for rec in recommendations)
    avg_cac = total_allocated / total_conv if total_conv > 0 else 0
    gold_recs = tier_breakdown.get('Gold', [])
    silver_recs = tier_breakdown.get('Silver', [])
    bronze_recs = tier_breakdown.get('Bronze', [])
    gold_budget = sum(r['allocated_budget'] for r in gold_recs)
    gold_conv = sum(r['predicted_conversions'] for r in gold_recs)
    silver_budget = sum(r['allocated_budget'] for r in silver_recs)
    silver_conv = sum(r['predicted_conversions'] for r in silver_recs)
    bronze_budget = sum(r['allocated_budget'] for r in bronze_recs)
    bronze_conv = sum(r['predicted_conversions'] for r in bronze_recs)
    rec_table_rows = []
    for rec in recommendations[:15]:
        row = (f"{rec['influencer_name']:<25} | {rec['tier']:<8} | "
               f"{format_currency(rec['allocated_budget'], market):>12} | "
               f"{rec['predicted_conversions']:<5} | "
               f"{format_currency(rec['effective_cac'], market):>12}")
        rec_table_rows.append(row)
    rec_table_str = "\n".join(rec_table_rows)
    booked_table_rows = []
    if booked_influencers:
        for inf in booked_influencers:
            row = (f"{inf.get('name', 'Unknown'):<25} | "
                   f"{format_currency(inf.get('budget_local', 0), market):>15} | Booked")
            booked_table_rows.append(row)
    booked_table_str = "\n".join(booked_table_rows) if booked_table_rows else "No influencers were previously booked for this period."

    prompt = f"""
You are a strategic marketing analyst bot. Generate a comprehensive multi-tier influencer marketing plan using code block table formatting for Slack.

**ANALYSIS FOR: {market.upper()} - {month.capitalize()} {year}**

First, provide the **Budget Overview** in a code block:
