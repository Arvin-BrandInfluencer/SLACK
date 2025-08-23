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

# --- API Endpoint Configuration ---
BASE_API_URL = os.getenv("BASE_API_URL", "http://127.0.0.1:10000")
TARGET_API_URL = f"{BASE_API_URL}/api/dashboard/targets"
ACTUALS_API_URL = f"{BASE_API_URL}/api/monthly_breakdown"
DISCOVERY_API_URL = f"{BASE_API_URL}/api/discovery"

# --- Market-Specific Currency Configuration (adopted from month.py) ---
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
    # Nordic currencies are typically formatted with the symbol after the number
    if currency_info['name'] in ['SEK', 'NOK', 'DKK']:
        return f"{amount:,.0f} {symbol}"
    else:
        return f"{symbol}{amount:,.2f}"

# --- In-Memory Storage for Thread Context ---
THREAD_CONTEXT = {}

def store_thread_context(thread_ts, market, month, year, target_budget, actual_spend, 
                        remaining_budget, total_allocated, recommendations, tier_breakdown, booked_influencers):
    """Store context data for a thread to enable follow-up questions"""
    currency_info = get_currency_info(market)
    THREAD_CONTEXT[thread_ts] = {
        'market': market,
        'month': month,
        'year': year,
        'currency_name': currency_info['name'],
        'target_budget': target_budget,
        'actual_spend': actual_spend,
        'remaining_budget': remaining_budget,
        'total_allocated': total_allocated,
        'recommendations': recommendations,
        'tier_breakdown': tier_breakdown,
        'booked_influencers': booked_influencers,
        'timestamp': datetime.now().isoformat()
    }
    logger.info(f"Stored context for thread {thread_ts}: {market} {month} {year}")

def get_thread_context(thread_ts):
    """Retrieve context data for a thread"""
    return THREAD_CONTEXT.get(thread_ts)

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
    logger.info(f"Querying {endpoint_name} API at {url} with payload: {json.dumps(payload)}")
    try:
        response = requests.post(url, json=payload, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"{endpoint_name} API Connection Error: {e}")
        return {"error": f"Could not connect to the {endpoint_name} API. Please check if the service is running."}

def fetch_tier_influencers(market, year, month_full, tier, booked_influencer_names):
    """Fetch unbooked influencers for a specific tier."""
    logger.info(f"Fetching {tier.capitalize()}-tier influencers for {market} {month_full} {year}")
    
    discovery_payload = {
        "source": "influencer_analytics",
        "view": "discovery_tiers",
        "filters": {"market": market, "year": year, "month": month_full, "tier": tier}
    }
    
    discovery_data = query_api(DISCOVERY_API_URL, discovery_payload, f"Discovery-{tier.capitalize()}")
    
    if "error" in discovery_data:
        logger.error(f"Error fetching {tier} tier: {discovery_data['error']}")
        return []
    
    all_influencers_in_tier = discovery_data.get("influencers", [])
    unbooked_influencers = [inf for inf in all_influencers_in_tier if inf['influencerName'] not in booked_influencer_names]
    
    logger.info(f"Found {len(unbooked_influencers)} unbooked {tier.capitalize()}-tier influencers")
    return unbooked_ influencers

def allocate_budget_cascading_tiers(gold_influencers, silver_influencers, bronze_influencers, 
                                   remaining_budget, market, effective_cac=50):
    """Allocate budget across tiers in cascade: Gold -> Silver -> Bronze."""
    all_recommendations, allocated_budget = [], 0
    tier_breakdown = {'Gold': [], 'Silver': [], 'Bronze': []}
    tiers_data = [('Gold', gold_influencers), ('Silver', silver_influencers), ('Bronze', bronze_influencers)]
    
    for tier_name, influencers_list in tiers_data:
        if allocated_budget >= remaining_budget * 0.98: break # Stop if 98% budget utilized
            
        logger.info(f"Allocating for {tier_name} tier - Budget left: {format_currency(remaining_budget - allocated_budget, market)}")
        sorted_influencers = sorted(influencers_list, key=lambda x: x.get('averageSpendPerCampaign', 0))
        
        tier_allocated, tier_recommendations = 0, []
        for influencer in sorted_influencers:
            avg_spend = influencer.get('averageSpendPerCampaign', 0)
            if allocated_budget + avg_spend <= remaining_budget:
                predicted_conversions = int(avg_spend / effective_cac) if effective_cac > 0 else 0
                recommendation = {
                    'influencer_name': influencer.get('influencerName', 'Unknown'),
                    'allocated_budget': avg_spend,
                    'predicted_conversions': predicted_conversions,
                    'effective_cac': effective_cac,
                    'tier': tier_name
                }
                tier_recommendations.append(recommendation)
                all_recommendations.append(recommendation)
                allocated_budget += avg_spend
                tier_allocated += avg_spend
                if allocated_budget >= remaining_budget * 0.98: break
        
        tier_breakdown[tier_name] = tier_recommendations
        logger.info(f"{tier_name} tier allocation complete: {len(tier_recommendations)} influencers, {format_currency(tier_allocated, market)}")
    
    logger.success(f"Total budget allocated: {format_currency(allocated_budget, market)} across {len(all_recommendations)} influencers")
    return all_recommendations, allocated_budget, tier_breakdown

def create_excel_report(recommendations, tier_breakdown, market, month, year, target_budget, 
                       actual_spend, remaining_budget, total_allocated, booked_influencers):
    """Create an Excel report with multiple sheets including tier breakdown."""
    excel_buffer = BytesIO()
    currency_name = get_currency_info(market)['name']
    
    with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
        budget_summary = pd.DataFrame({
            'Metric': ['Target Budget', 'Actual Spend', 'Remaining Budget', 'Recommended Allocation', 'Budget Utilization %'],
            'Amount': [
                format_currency(target_budget, market), format_currency(actual_spend, market),
                format_currency(remaining_budget, market), format_currency(total_allocated, market),
                f"{(total_allocated/remaining_budget)*100:.1f}%" if remaining_budget > 0 else "0%"
            ]})
        budget_summary.to_excel(writer, sheet_name='Budget Summary', index=False)
        
        if recommendations:
            df = pd.DataFrame(recommendations)
            df.columns = ['Influencer Name', f'Allocated Budget ({currency_name})', 'Predicted Conversions', f'Effective CAC ({currency_name})', 'Tier']
            df[f'Allocated Budget ({currency_name})'] = df[f'Allocated Budget ({currency_name})'].apply(lambda x: format_currency(x, market))
            df[f'Effective CAC ({currency_name})'] = df[f'Effective CAC ({currency_name})'].apply(lambda x: format_currency(x, market))
            df.to_excel(writer, sheet_name='All Recommendations', index=False)
        
        tier_summary_data = []
        for tier, recs in tier_breakdown.items():
            if recs:
                budget = sum(r['allocated_budget'] for r in recs)
                conversions = sum(r['predicted_conversions'] for r in recs)
                tier_summary_data.append({
                    'Tier': tier, 'Influencers Count': len(recs), f'Total Budget ({currency_name})': format_currency(budget, market),
                    'Predicted Conversions': conversions, f'Average CAC ({currency_name})': format_currency(budget/conversions, market) if conversions > 0 else "N/A"
                })
        if tier_summary_data: pd.DataFrame(tier_summary_data).to_excel(writer, sheet_name='Tier Breakdown', index=False)
        
        if booked_influencers:
            booked_df = pd.DataFrame([{'Influencer Name': i.get('name', 'Unknown'), f'Spent Budget ({currency_name})': format_currency(i.get('budget_local', 0), market), 'Status': 'Booked'} for i in booked_influencers])
            booked_df.to_excel(writer, sheet_name='Booked Influencers', index=False)
    
    excel_buffer.seek(0)
    return excel_buffer

def create_llm_prompt_with_code_blocks(market, month, year, target_budget, actual_spend, remaining_budget, 
                                      booked_influencers, recommendations, total_allocated, tier_breakdown):
    """Updated LLM prompt to generate code block formatted responses with local currency."""
    currency_info = get_currency_info(market)
    currency_name = currency_info['name']
    
    booked_list_str = "\n".join([f"- {inf['name']} (Spent: {format_currency(inf['budget_local'], market)})" for inf in booked_influencers]) or "None"
    
    total_conversions = sum(rec['predicted_conversions'] for rec in recommendations) if recommendations else 0
    avg_cac = total_allocated / total_conversions if total_conversions > 0 else 0

    prompt = f"""
    You are a strategic marketing analyst bot. Generate a comprehensive multi-tier influencer marketing plan using code block table formatting for Slack.

    **CONTEXT:**
    - Market: {market.upper()}
    - Currency: {currency_name}
    - Period: {month.capitalize()} {year}
    - Target Budget: {format_currency(target_budget, market)}
    - Actual Spend: {format_currency(actual_spend, market)}
    - Remaining Budget: {format_currency(remaining_budget, market)}
    
    **DATA FOR PLAN:**
    - Recommended Allocation: {format_currency(total_allocated, market)}
    - Total Influencers: {len(recommendations)}
    - Total Predicted Conversions: {total_conversions}
    - Already Booked Influencers Data: {json.dumps(booked_influencers)}
    - Tier Breakdown Data: {json.dumps(tier_breakdown, indent=2)}

    **FORMATTING REQUIREMENTS:**
    Generate a response with the following sections using code blocks. Ensure all monetary values are formatted correctly for the {market.upper()} market.

    1. **Budget Overview Section:**
    ```
    Budget Summary for {market.upper()} - {month.capitalize()} {year}
    ================================================================================
    Target Budget:           {format_currency(target_budget, market)}
    Actual Spend:            {format_currency(actual_spend, market)}  ({(actual_spend/target_budget)*100:.1f}% of Target)
    Remaining Budget:        {format_currency(remaining_budget, market)}
    Recommended Allocation:  {format_currency(total_allocated, market)}  ({(total_allocated/remaining_budget)*100:.1f}% of Remaining)
    Currency:                {currency_name}
    ```

    2. **Multi-Tier Strategy Overview:**
    ```
    Multi-Tier Allocation Strategy
    ================================================================================
    [Use Tier Breakdown Data to create a summary table with counts, total budget, and total conversions for Gold, Silver, and Bronze tiers.]
    --------------------------------------------------------------------------------
    Total Strategy:  {len(recommendations)} influencers | {format_currency(total_allocated, market)} | {total_conversions} conversions
    Average CAC:     {format_currency(avg_cac, market)}
    ```

    3. **Detailed Influencer Recommendations (Top 15):**
    ```
    Recommended Influencer Portfolio
    ================================================================================
    Influencer Name     | Tier   | Budget ({currency_name}) | Conv | CAC ({currency_name})
    -------------------|--------|------------|------|-----------
    [Use Tier Breakdown Data to create a detailed table of recommended influencers.]
    ```

    4. **Currently Booked Influencers (if any):**
    ```
    Currently Booked Influencers
    ================================================================================
    Influencer Name     | Spent Budget ({currency_name}) | Status
    -------------------|------------------|--------
    [List booked influencers from the data provided.]
    ```

    5. **Strategic Insights:** (3-4 key bullet points about the plan for {market.upper()})

    Generate the complete, user-facing response now.
    """
    return prompt

# --- 3. SLACK COMMAND HANDLER ---

@app.command("/plan")
def handle_plan_command(ack, say, command):
    ack()
    text = command.get('text', '').strip()
    channel_id = command.get('channel_id')
    
    # --- 1. Parse & Validate Input ---
    parts = text.split('-')
    if len(parts) != 3:
        say("Format: `/plan Market-Month-Year` (e.g., `/plan UK-December-2025`)")
        return

    try:
        market, raw_month_input, year = parts[0].strip(), parts[1].strip(), int(parts[2].strip())
        month_abbr = USER_INPUT_TO_ABBR_MAP.get(raw_month_input.lower())
        if not month_abbr:
            say(f"Invalid month '{raw_month_input}'. Use full name or 3-letter abbreviation.")
            return
        month_full = ABBR_TO_FULL_MONTH_MAP.get(month_abbr)
        currency_name = get_currency_info(market)['name']
        say(f"Creating a strategic plan for *{market.upper()}* for *{raw_month_input.capitalize()} {year}* (Currency: *{currency_name}*)...")
    except (ValueError, IndexError):
        say("Invalid format. Use `/plan Market-Month-Year` with a valid year.")
        return

    # --- 2. Fetch Base Data ---
    # V V V V V V V V V V V V V V V V V V V V V V V V V V V V V V V V V V V V V
    # CORRECTED: Use 'month_abbr' for the Targets API as it requires the 
    # three-letter abbreviation (e.g., 'Dec').
    target_data = query_api(TARGET_API_URL, {"filters": {"market": market, "month": month_abbr, "year": year}}, "Targets")
    # ^ ^ ^ ^ ^ ^ ^ ^ ^ ^ ^ ^ ^ ^ ^ ^ ^ ^ ^ ^ ^ ^ ^ ^ ^ ^ ^ ^ ^ ^ ^ ^ ^ ^ ^ ^ ^
    if "error" in target_data: return say(f"API Error: `{target_data['error']}`")
    
    # Other APIs might require the full month name, so we use 'month_full' here.
    actual_data = query_api(ACTUALS_API_URL, {"filters": {"market": market, "month": month_full, "year": year}}, "Actuals")
    if "error" in actual_data: return say(f"API Error: `{actual_data['error']}`")

    # --- 3. Consolidate Data & Calculate Local Currency Spend ---
    target_budget = target_data.get("kpis", {}).get("total_target_budget", 0)
    
    actual_spend_eur = actual_data.get("metrics", {}).get("budget_spent_eur", 0)
    actual_spend = convert_eur_to_local(actual_spend_eur, market)
    
    booked_influencers = actual_data.get("influencers", [])
    booked_influencer_names = {inf['name'] for inf in booked_influencers}
    remaining_budget = target_budget - actual_spend

    if remaining_budget <= 0:
        status = 'OVERSPENT' if remaining_budget < 0 else 'FULLY UTILIZED'
        say(f"**Budget Analysis: No Allocation Possible for {market.upper()} {raw_month_input.capitalize()} {year}**\n"
            f"```\nTarget: {format_currency(target_budget, market)}\n"
            f"Spent:  {format_currency(actual_spend, market)}\n"
            f"Status: {status}\n```\n"
            f"🚫 The budget is fully utilized or overspent. No further allocations are recommended.")
        return

    # --- 4. Fetch Available Influencers ---
    say(f"Fetching available influencers...")
    gold_influencers = fetch_tier_influencers(market, year, month_full, "gold", booked_influencer_names)
    silver_influencers = fetch_tier_influencers(market, year, month_full, "silver", booked_influencer_names)
    bronze_influencers = fetch_tier_influencers(market, year, month_full, "bronze", booked_influencer_names)
    
    total_available = len(gold_influencers) + len(silver_influencers) + len(bronze_influencers)
    if total_available == 0:
        say(f"✅ **Excellent Work!** All available Gold, Silver, and Bronze influencers for {market.upper()} have already been booked. Remaining budget is {format_currency(remaining_budget, market)}.")
        return
    
    say(f"Found influencers: *Gold: {len(gold_influencers)}*, *Silver: {len(silver_influencers)}*, *Bronze: {len(bronze_influencers)}*. Optimizing budget...")
    
    # --- 5. Allocate Budget & Generate Plan ---
    effective_cac = 50 
    recommendations, total_allocated, tier_breakdown = allocate_budget_cascading_tiers(
        gold_influencers, silver_influencers, bronze_influencers, remaining_budget, market, effective_cac
    )
    
    if not recommendations:
        say(f"No influencers found that fit within the remaining budget of {format_currency(remaining_budget, market)}.")
        return
    
    # --- 6. Generate & Upload Excel Report ---
    say("Creating detailed Excel report...")
    try:
        excel_buffer = create_excel_report(recommendations, tier_breakdown, market, raw_month_input, year, target_budget, actual_spend, remaining_budget, total_allocated, booked_influencers)
        filename = f"Influencer_Plan_{market}_{raw_month_input}_{year}_{datetime.now().strftime('%Y%m%d')}.xlsx"
        
        excel_response = app.client.files_upload_v2(
            channel=channel_id,
            file=excel_buffer.getvalue(),
            filename=filename,
            title=f"Influencer Plan - {market.upper()} {raw_month_input.capitalize()} {year}",
            initial_comment=f"📊 Here's your influencer marketing plan for {market.upper()}!"
        )
        
        thread_ts = None
        try:
            shares = excel_response.get('file', {}).get('shares', {})
            if shares:
                channel_shares = shares.get('public', {}).get(channel_id) or shares.get('private', {}).get(channel_id)
                if channel_shares and isinstance(channel_shares, list) and channel_shares:
                    thread_ts = channel_shares[0].get('ts')
            if thread_ts:
                logger.success(f"Successfully uploaded Excel report '{filename}' and found thread_ts: {thread_ts}")
            else:
                logger.warning(f"Could not determine thread_ts from file upload response for '{filename}'. AI summary will not be threaded.")
        except Exception as e:
            logger.error(f"Error parsing file upload response for thread_ts: {e}. AI summary will not be threaded.")
            thread_ts = None

    except Exception as e:
        logger.error(f"Error creating/uploading Excel report: {e}")
        say(f"Failed to create the Excel report. Error: `{str(e)}`")
        return

    # --- 7. Generate & Post AI Summary ---
    say("Generating strategic AI summary...", thread_ts=thread_ts)
    try:
        prompt = create_llm_prompt_with_code_blocks(market, raw_month_input, year, target_budget, actual_spend, remaining_budget, booked_influencers, recommendations, total_allocated, tier_breakdown)
        response = gemini_model.generate_content(prompt)
        
        if thread_ts:
            store_thread_context(thread_ts, market, raw_month_input, year, target_budget, actual_spend, remaining_budget, total_allocated, recommendations, tier_breakdown, booked_influencers)
        
        for chunk in split_message_for_slack(response.text):
            say(text=chunk, thread_ts=thread_ts)
        
        if thread_ts:
            say(text="💬 **Have questions?** Reply in this thread and mention me!", thread_ts=thread_ts)
            
    except Exception as e:
        logger.error(f"Error calling Gemini API: {e}")
        say(f"Excel report uploaded, but AI summary failed. Error: `{str(e)}`", thread_ts=thread_ts)
    
    logger.success(f"Completed plan for {market}-{raw_month_input}-{year}.")

# --- 4. THREAD REPLY HANDLER ---

@app.event("message")
def handle_thread_replies(event, say):
    """Handle thread replies and questions about the plan data."""
    if not event.get('thread_ts') or not event.get('text') or '<@' not in event.get('text', ''):
        return

    thread_ts, user_message, channel, user_id = event['thread_ts'], event['text'], event['channel'], event.get('user')
    context = get_thread_context(thread_ts)
    if not context: return

    logger.info(f"Handling thread question: {user_message}")
    try:
        context_prompt = f"""
        You are a marketing analyst bot answering a follow-up question about a plan.
        
        **PLAN CONTEXT:**
        - Market: {context['market'].upper()}
        - Period: {context['month']} {context['year']}
        - Currency: {context['currency_name']}
        - Target Budget: {format_currency(context['target_budget'], context['market'])}
        - Actual Spend: {format_currency(context['actual_spend'], context['market'])}
        - Recommended Allocation: {format_currency(context['total_allocated'], context['market'])}
        - Detailed Recommendations Data: {json.dumps(context['recommendations'][:10], indent=2)}
        - Booked Influencers Data: {json.dumps(context['booked_influencers'], indent=2)}
        
        **USER QUESTION:** "{user_message}"
        
        **INSTRUCTIONS:**
        - Answer the user's question using only the provided plan context.
        - Be concise, data-driven, and use the correct currency ({context['currency_name']}).
        - If the question cannot be answered with the data, state what's missing.
        
        Answer the question now:
        """
        response = gemini_model.generate_content(context_prompt)
        say(channel=channel, thread_ts=thread_ts, text=f"<@{user_id}> {response.text}")
        logger.success(f"Successfully answered thread question.")
    except Exception as e:
        logger.error(f"Error handling thread question: {e}")
        say(channel=channel, thread_ts=thread_ts, text=f"<@{user_id}> I encountered an error: `{str(e)}`.")

# --- 5. APP EXECUTION ---
if __name__ == "__main__":
    logger.info("🎯 Starting Multi-Tier Strategic Planner Slack Bot...")
    try:
        handler = SocketModeHandler(app, SLACK_APP_TOKEN)
        logger.success("🚀 Bot is running and connected to Slack!")
        handler.start()
    except Exception as e:
        logger.critical(f"Failed to start the bot: {e}")
        sys.exit(1)