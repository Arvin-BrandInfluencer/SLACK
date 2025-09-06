# ======================================================
# FILE: plan.py (FINAL - BUGS FIXED)
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

# --- 1. CONFIGURATION & INITIALIZATION ---
logger.remove(); logger.add(sys.stderr, format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function} - {message}", colorize=True)
load_dotenv()
try:
    GOOGLE_API_KEY = os.environ["GOOGLE_API_KEY"]; genai.configure(api_key=GOOGLE_API_KEY)
    gemini_model = genai.GenerativeModel('gemini-1.5-flash-latest'); logger.success("Gemini client initialized for plan.py.")
except KeyError as e:
    logger.critical(f"FATAL: Missing GOOGLE_API_KEY. Please check .env file."); sys.exit(1)

# --- CONSTANTS AND HELPERS ---
BASE_API_URL = os.getenv("BASE_API_URL", "http://127.0.0.1:10000"); UNIFIED_API_URL = f"{BASE_API_URL}/api/influencer/query"
MARKET_CURRENCY_CONFIG = { 'SWEDEN': {'rate': 11.30, 'symbol': 'SEK', 'name': 'SEK'}, 'NORWAY': {'rate': 11.50, 'symbol': 'NOK', 'name': 'NOK'}, 'DENMARK': {'rate': 7.46, 'symbol': 'DKK', 'name': 'DKK'}, 'UK': {'rate': 0.85, 'symbol': '£', 'name': 'GBP'}, 'FRANCE': {'rate': 1.0, 'symbol': '€', 'name': 'EUR'}, }

def get_currency_info(market): return MARKET_CURRENCY_CONFIG.get(str(market).upper(), {'rate': 1.0, 'symbol': '€', 'name': 'EUR'})

def convert_eur_to_local(amount_eur, market):
    try: safe_amount = float(amount_eur if amount_eur is not None else 0.0)
    except (ValueError, TypeError): safe_amount = 0.0
    return safe_amount * get_currency_info(market)['rate']

def format_currency(amount, market):
    try: safe_amount = float(amount if amount is not None else 0.0)
    except (ValueError, TypeError): safe_amount = 0.0
    currency_info = get_currency_info(market)
    if currency_info['name'] in ['SEK', 'NOK', 'DKK']: return f"{safe_amount:,.0f} {currency_info['symbol']}"
    else: return f"{currency_info['symbol']}{safe_amount:,.2f}"

def split_message_for_slack(message: str, max_length: int = 2800) -> list:
    if not message: return []
    if len(message) <= max_length: return [message]
    chunks, current_chunk = [], ""
    for line in message.split('\n'):
        if len(current_chunk) + len(line) + 1 > max_length:
            if current_chunk.strip(): chunks.append(current_chunk)
            current_chunk = line + "\n"
        else: current_chunk += line + "\n"
    if current_chunk.strip(): chunks.append(current_chunk)
    return chunks

def query_api(url: str, payload: dict, endpoint_name: str) -> dict:
    logger.info(f"Querying {endpoint_name} API at {url} with payload: {json.dumps(payload)}")
    try:
        response = requests.post(url, json=payload, timeout=60); response.raise_for_status(); return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"{endpoint_name} API Connection Error: {e}"); return {"error": f"Could not connect to the {endpoint_name} API."}

def fetch_tier_influencers(market, year, tier, booked_influencer_names):
    payload = {"source": "influencer_analytics", "view": "discovery_tiers", "filters": {"market": market, "year": year, 'tier': tier}}
    data = query_api(UNIFIED_API_URL, payload, f"Discovery-{tier.capitalize()}")
    if "error" in data: logger.error(f"Error fetching {tier} tier: {data['error']}"); return []
    unbooked = [inf for inf in data.get("items", []) if inf.get('influencer_name') not in booked_influencer_names]
    logger.info(f"Found {len(unbooked)} unbooked {tier.capitalize()}-tier influencers"); return unbooked

def allocate_budget_cascading_tiers(gold, silver, bronze, budget, cac=50, market='France'):
    recs, allocated = [], 0.0; tier_breakdown = {'Gold': [], 'Silver': [], 'Bronze': []}
    for name, influencers in [('Gold', gold), ('Silver', silver), ('Bronze', bronze)]:
        if allocated >= budget * 0.98: break
        for inf in influencers:
            count = inf.get('campaign_count', 1) or 1
            spend = float(inf.get('total_spend_eur', 0.0) or 0.0)
            inf['averageSpendPerCampaign'] = spend / count if count > 0 else 0.0
        for inf in sorted(influencers, key=lambda x: x.get('averageSpendPerCampaign', 0) or 0):
            spend_eur = inf.get('averageSpendPerCampaign', 0) or 0
            if spend_eur <= 0: continue
            spend_local = convert_eur_to_local(spend_eur, market)
            if allocated + spend_local <= budget:
                pred_conv = int(spend_local / cac) if cac > 0 else 0
                rec = {'influencer_name': inf.get('influencer_name', 'Unknown'), 'allocated_budget': spend_local, 'predicted_conversions': pred_conv, 'effective_cac': float(cac), 'tier': name, 'market': market}
                recs.append(rec); tier_breakdown[name].append(rec); allocated += spend_local
                if allocated >= budget * 0.98: break
    return recs, allocated, tier_breakdown

def create_excel_report(recs, market, month, year, target_budget, actual_spend, remaining_budget, total_allocated, booked_influencers):
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
        budget_data = {'Metric': ['Target Budget', 'Actual Spend', 'Remaining Budget', 'Recommended Allocation'], 'Amount': [format_currency(target_budget, market), format_currency(actual_spend, market), format_currency(remaining_budget, market), format_currency(total_allocated, market)]}
        pd.DataFrame(budget_data).to_excel(writer, sheet_name='Budget Summary', index=False)
        if recs: pd.DataFrame(recs).to_excel(writer, sheet_name='All Recommendations', index=False)
        if booked_influencers:
            booked_data = [{'Influencer Name': inf.get('influencer_name', 'Unknown'), 'Spent Budget': format_currency(inf.get('budget_local', 0), market)} for inf in booked_influencers]
            pd.DataFrame(booked_data).to_excel(writer, sheet_name='Booked Influencers', index=False)
    buffer.seek(0); return buffer

def create_llm_prompt(market, month, year, target_budget, actual_spend, remaining_budget, recommendations, total_allocated, tier_breakdown):
    safe_total_allocated = float(total_allocated or 0.0)
    total_conv = sum(rec.get('predicted_conversions', 0) for rec in recommendations)
    avg_cac = safe_total_allocated / total_conv if total_conv > 0 else 0.0
    gold_recs, silver_recs, bronze_recs = tier_breakdown.get('Gold', []), tier_breakdown.get('Silver', []), tier_breakdown.get('Bronze', [])
    gold_budget, silver_budget, bronze_budget = sum(r['allocated_budget'] for r in gold_recs), sum(r['allocated_budget'] for r in silver_recs), sum(r['allocated_budget'] for r in bronze_recs)
    gold_conv, silver_conv, bronze_conv = sum(r['predicted_conversions'] for r in gold_recs), sum(r['predicted_conversions'] for r in silver_recs), sum(r['predicted_conversions'] for r in bronze_recs)
    rec_table_str = "\n".join([f"{(rec.get('influencer_name') or 'Unknown')[:25]:<25} | {rec.get('tier', 'N/A'):<8} | {format_currency(rec.get('allocated_budget', 0), market):>12} | {rec.get('predicted_conversions', 0):<5} | {format_currency(rec.get('effective_cac', 0), market):>12}" for rec in recommendations[:15]])
    
    pre_formatted_report = f"""Here is the strategic plan for **{market.upper()} - {month.capitalize()} {year}**.

**Budget Overview**
Target Budget: {format_currency(target_budget, market)}
Actual Spend So Far: {format_currency(actual_spend, market)}
Remaining Budget: {format_currency(remaining_budget, market)}
Recommended Allocation: {format_currency(total_allocated, market)} ({(safe_total_allocated / remaining_budget * 100) if remaining_budget > 0 else 0:.1f}% of remaining)

**Multi-Tier Strategy**
Tier | Influencers | Budget | Est. Conversions
Gold | {len(gold_recs):>11} | {format_currency(gold_budget, market):>14} | {gold_conv:>16}
Silver | {len(silver_recs):>11} | {format_currency(silver_budget, market):>14} | {silver_conv:>16}
Bronze | {len(bronze_recs):>11} | {format_currency(bronze_budget, market):>14} | {bronze_conv:>16}
TOTAL | {len(recommendations):>11} | {format_currency(total_allocated, market):>14} | {total_conv:>16}
Projected Avg CAC: {format_currency(avg_cac, market)}

**Top 15 Influencer Recommendations**
Influencer Name | Tier | Budget | Conv. | Est. CAC
{rec_table_str}
"""
    prompt = f"""You are Nova, a marketing analyst. Below is a pre-formatted marketing plan. Your ONLY task is to add a short "Strategic Insights" section at the end. Base your insights ONLY on the data presented in the plan.Your insights should be direct and actionable; do not state "based on the data" or similar introductory phrases.

{pre_formatted_report}

**Strategic Insights**
*   [Provide a bullet point analyzing the ROI optimization of the tier mix]
*   [Provide a bullet point commenting on the budget utilization]
*   [Provide a bullet point about risk diversification]
"""
    return prompt, pre_formatted_report

# --- CORE LOGIC FUNCTION ---
def run_strategic_plan(client, say, event, thread_ts, params, thread_context_store):
    try:
        market, month_abbr, month_full, year = params['market'], params['month_abbr'], params['month_full'], params['year']
    except KeyError as e:
        say(f"A required parameter was missing: {e}", thread_ts=thread_ts); return

    say(f"📊 Creating a strategic plan for *{market.upper()}* for *{month_full} {year}*...", thread_ts=thread_ts)

    target_payload = {"source": "dashboard", "filters": {"market": market, "year": year}}
    target_data = query_api(UNIFIED_API_URL, target_payload, "Dashboard (Targets)")
    if "error" in target_data: say(f"API Error: `{target_data['error']}`", thread_ts=thread_ts); return
        
    actuals_payload = {"source": "influencer_analytics", "view": "monthly_breakdown", "filters": {"market": market, "month": month_full, "year": year}}
    actual_data_response = query_api(UNIFIED_API_URL, actuals_payload, "Influencer Analytics (Monthly)")
    if "error" in actual_data_response: say(f"API Error: `{actual_data_response['error']}`", thread_ts=thread_ts); return

    target_budget = next((float(m.get("target_budget_clean", 0.0)) for m in target_data.get("monthly_detail", []) if m.get("month") == month_abbr), 0.0)
    summary = (actual_data_response.get("monthly_data") or [{}])[0].get("summary", {})
    actual_spend = convert_eur_to_local(float(summary.get("total_spend_eur", 0.0)), market)
    booked_influencers = (actual_data_response.get("monthly_data") or [{}])[0].get("details", [])
    booked_names = {inf.get('influencer_name') for inf in booked_influencers if inf.get('influencer_name')}
    remaining_budget = target_budget - actual_spend
    
    if remaining_budget <= 0:
        say(f"The budget for this period has already been fully utilized or overspent.", thread_ts=thread_ts); return
    
    gold, silver, bronze = fetch_tier_influencers(market, year, "gold", booked_names), fetch_tier_influencers(market, year, "silver", booked_names), fetch_tier_influencers(market, year, "bronze", booked_names)
    if not any([gold, silver, bronze]):
        say(f"Excellent! All available high-performing influencers seem to be booked for this period.", thread_ts=thread_ts); return

    recs, total_allocated, tier_breakdown = allocate_budget_cascading_tiers(gold, silver, bronze, remaining_budget, 50, market)
    if not recs:
        say(f"No available influencers could be booked with the remaining budget of {format_currency(remaining_budget, market)}.", thread_ts=thread_ts); return

    try:
        channel_id = event.get('channel') or event.get('channel_id')
        excel_buffer = create_excel_report(recs, market, month_full, year, target_budget, actual_spend, remaining_budget, total_allocated, booked_influencers)
        client.files_upload_v2(channel=channel_id, file=excel_buffer.getvalue(), filename=f"Strategic_Plan_{market}_{month_full}_{year}.xlsx", title=f"Strategic Plan Details", initial_comment="For your convenience, here is the detailed plan in an Excel file:", thread_ts=thread_ts)
        
        prompt, report_text = create_llm_prompt(market, month_full, year, target_budget, actual_spend, remaining_budget, recs, total_allocated, tier_breakdown)
        response = gemini_model.generate_content(prompt)
        
        for chunk in split_message_for_slack(report_text): say(text=chunk, thread_ts=thread_ts)
        say(text=response.text, thread_ts=thread_ts)

        thread_context_store[thread_ts] = {'type': 'strategic_plan', 'params': params, 'raw_target_data': target_data, 'raw_actual_data': actual_data_response, 'plan_recommendations': recs, 'bot_response': report_text + "\n" + response.text}
        say(text="💬 This plan is ready for review. Feel free to ask any follow-up questions right here in this thread!", thread_ts=thread_ts)
    except Exception as e:
        logger.error(f"Error during report generation for plan: {e}", exc_info=True); say(f"I'm sorry, an error occurred: `{str(e)}`", thread_ts=thread_ts)

# --- THREAD FOLLOW-UP HANDLER ---
def handle_thread_replies(event, say, client, context):
    user_message = event.get("text", "").strip()
    thread_ts, user_id = event["thread_ts"], event.get('user')
    logger.info(f"Handling follow-up for strategic_plan in thread {thread_ts}")
    try:
        context_prompt = f"""
        You are a helpful marketing analyst assistant.
        **Current Context:** A Strategic Plan for **{context['params']['market']}** for **{context['params']['month_full']} {context['params']['year']}**.
        **Available Data:** You have the full JSON data used to create this plan: {json.dumps({'targets': context.get('raw_target_data', {}), 'actuals': context.get('raw_actual_data', {}), 'recommendations': context.get('plan_recommendations', [])})}
        
        **User's Follow-up:** "{user_message}"
        
        **Instructions:**
        1.  Answer the user's question **ONLY** using the data provided for the current plan's context ({context['params']['month_full']} {context['params']['year']}).
        2.  **CRITICAL:** If the user asks to compare this plan to a different time period (e.g., "how many of these influencers were used in June?"), you MUST state that you do not have the data for the other period in your current context.
            - Correct response example: "That's a great question. I can't directly compare, as my current context is only the November plan. I don't have the June data loaded right now. To answer, I'd need to run a new review for June."
        3. Present your answer naturally, without phrases like "based on the provided data".
        """
        response = gemini_model.generate_content(context_prompt)
        for chunk in split_message_for_slack(response.text):
            say(text=f"<@{user_id}> {chunk}", thread_ts=thread_ts)
    except Exception as e:
        logger.error(f"Error handling thread question in plan.py: {e}"); say(text=f"<@{user_id}> I encountered an error: `{str(e)}`.", thread_ts=thread_ts)
