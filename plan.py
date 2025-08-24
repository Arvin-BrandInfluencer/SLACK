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
BASE_API_URL = os.getenv("BASE_API_URL", "https://lyra-final.onrender.com")
TARGET_API_URL = f"{BASE_API_URL}/api/dashboard/targets"
ACTUALS_API_URL = f"{BASE_API_URL}/api/monthly_breakdown"
DISCOVERY_API_URL = f"{BASE_API_URL}/api/discovery"

# --- Market-Specific Currency Configuration (Same as month.py) ---
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

# --- Currency Mapping ---
MARKET_CURRENCY_MAP = {
    'sweden': 'SEK',
    'norway': 'NOK',
    'denmark': 'DKK',
    'uk': 'GBP',
    'france': 'EUR'
}

# --- In-Memory Storage for Thread Context ---
THREAD_CONTEXT = {}

def store_thread_context(thread_ts, market, month, year, currency, target_budget, actual_spend,
                        remaining_budget, total_allocated, recommendations, tier_breakdown, booked_influencers):
    """Store context data for a thread to enable follow-up questions"""
    THREAD_CONTEXT[thread_ts] = {
        'market': market,
        'month': month,
        'year': year,
        'currency': currency,
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

def get_currency_for_market(market):
    """Get the currency for a given market"""
    return MARKET_CURRENCY_MAP.get(market.lower(), 'EUR')  # Default to EUR if market not found

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
    
    chunks = []
    current_chunk = ""
    in_code_block = False

    lines = message.split('\n')

    for line in lines:
        # Check if we're entering or exiting a code block
        if line.strip().startswith('```'):
            in_code_block = not in_code_block
        
        # If adding this line would exceed the limit
        if len(current_chunk) + len(line) + 1 > max_length:
            # If we're in a code block, close it properly
            if in_code_block and current_chunk.strip():
                current_chunk += "\n```"
                in_code_block = False
            
            if current_chunk.strip():
                chunks.append(current_chunk.strip())
            
            # Start new chunk, reopening code block if needed
            if in_code_block:
                current_chunk = "```\n" + line + "\n"
            else:
                current_chunk = line + "\n"
        else:
            current_chunk += line + "\n"

    # Add the final chunk
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
    """
    Fetch unbooked influencers for a specific tier
    
    Args:
        market: Target market
        year: Target year
        month_full: Full month name (e.g., "January", "February")
        tier: Tier name (gold, silver, bronze)
        booked_influencer_names: Set of already booked influencer names

    Returns:
        List of unbooked influencers for the tier
    """
    logger.info(f"Fetching {tier.capitalize()}-tier influencers for {market} {month_full} {year}")

    # Updated payload structure to match your curl command
    discovery_payload = {
        "source": "influencer_analytics",
        "view": "discovery_tiers",
        "filters": {
            "market": market,
            "year": year,
            "month": month_full,  # Use full month name like "January"
            "tier": tier
        }
    }

    discovery_data = query_api(DISCOVERY_API_URL, discovery_payload, f"Discovery-{tier.capitalize()}")

    if "error" in discovery_data:
        logger.error(f"Error fetching {tier} tier: {discovery_data['error']}")
        return []

    all_influencers_in_tier = discovery_data.get("influencers", [])
    unbooked_influencers = [inf for inf in all_influencers_in_tier 
                           if inf['influencerName'] not in booked_influencer_names]

    logger.info(f"Found {len(unbooked_influencers)} unbooked {tier.capitalize()}-tier influencers")
    return unbooked_influencers

def allocate_budget_cascading_tiers(gold_influencers, silver_influencers, bronze_influencers,
                                  remaining_budget, effective_cac=50, market='FRANCE'):
    """
    Allocate budget across tiers in cascade: Gold -> Silver -> Bronze
    
    Args:
        gold_influencers: List of available Gold-tier influencers
        silver_influencers: List of available Silver-tier influencers
        bronze_influencers: List of available Bronze-tier influencers
        remaining_budget: Total budget available to allocate
        effective_cac: Cost per acquisition (default: 50)
        market: Market name for currency formatting

    Returns:
        Tuple: (all_recommendations, total_allocated_budget, tier_breakdown)
    """
    all_recommendations = []
    allocated_budget = 0
    tier_breakdown = {'Gold': [], 'Silver': [], 'Bronze': []}

    # Define tier priority
    tiers_data = [
        ('Gold', gold_influencers),
        ('Silver', silver_influencers),
        ('Bronze', bronze_influencers)
    ]

    for tier_name, influencers_list in tiers_data:
        if allocated_budget >= remaining_budget * 0.98:  # Stop if 98% budget utilized
            break
            
        logger.info(f"Allocating budget for {tier_name} tier - Budget left: {format_currency(remaining_budget - allocated_budget, market)}")
        
        # Sort influencers by average spend (ascending) to maximize utilization
        sorted_influencers = sorted(influencers_list, 
                                  key=lambda x: x.get('averageSpendPerCampaign', 0))
        
        tier_allocated = 0
        tier_recommendations = []
        
        for influencer in sorted_influencers:
            avg_spend = influencer.get('averageSpendPerCampaign', 0)
            
            # Check if we can afford this influencer
            if allocated_budget + avg_spend <= remaining_budget:
                predicted_conversions = int(avg_spend / effective_cac) if effective_cac > 0 else 0
                conversion_rate = (predicted_conversions / avg_spend * 100) if avg_spend > 0 else 0
                
                recommendation = {
                    'influencer_name': influencer.get('influencerName', 'Unknown'),
                    'allocated_budget': avg_spend,
                    'predicted_conversions': predicted_conversions,
                    'effective_cac': effective_cac,
                    'conversion_rate_percent': round(conversion_rate, 2),
                    'tier': tier_name,
                    'market': market
                }
                
                tier_recommendations.append(recommendation)
                all_recommendations.append(recommendation)
                allocated_budget += avg_spend
                tier_allocated += avg_spend
                
                # Stop if we've allocated most of the remaining budget
                if allocated_budget >= remaining_budget * 0.98:
                    break
        
        tier_breakdown[tier_name] = tier_recommendations
        logger.info(f"{tier_name} tier allocation complete: {len(tier_recommendations)} influencers, {format_currency(tier_allocated, market)}")

    logger.success(f"Total budget allocated: {format_currency(allocated_budget, market)} across {len(all_recommendations)} influencers")
    return all_recommendations, allocated_budget, tier_breakdown

def create_excel_report(recommendations, tier_breakdown, market, month, year, target_budget,
                       actual_spend, remaining_budget, total_allocated, booked_influencers):
    """
    Create an Excel report with multiple sheets including tier breakdown
    """
    excel_buffer = BytesIO()
    
    with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
        # Sheet 1: Budget Summary
        budget_summary = pd.DataFrame({
            'Metric': ['Target Budget', 'Actual Spend', 'Remaining Budget', 'Recommended Allocation', 'Budget Utilization %'],
            'Amount': [
                f"{format_currency(target_budget, market)}",
                f"{format_currency(actual_spend, market)}",
                f"{format_currency(remaining_budget, market)}",
                f"{format_currency(total_allocated, market)}",
                f"{(total_allocated/remaining_budget)*100:.1f}%" if remaining_budget > 0 else "0%"
            ]
        })
        budget_summary.to_excel(writer, sheet_name='Budget Summary', index=False)
        
        # Sheet 2: All Influencer Recommendations
        if recommendations:
            recommendations_df = pd.DataFrame(recommendations)
            recommendations_df.columns = [
                'Influencer Name', f'Allocated Budget', 'Predicted Conversions',
                f'Effective CAC', 'Conversion Rate (%)', 'Tier', 'Market'
            ]
            # Format budget column
            recommendations_df[f'Allocated Budget'] = recommendations_df[f'Allocated Budget'].apply(lambda x: f"{format_currency(x, market)}")
            recommendations_df[f'Effective CAC'] = recommendations_df[f'Effective CAC'].apply(lambda x: f"{format_currency(x, market)}")
            # Remove market column from display
            recommendations_df = recommendations_df.drop('Market', axis=1)
            
            recommendations_df.to_excel(writer, sheet_name='All Recommendations', index=False)
        
        # Sheet 3: Tier Breakdown
        tier_summary_data = []
        for tier_name, tier_recs in tier_breakdown.items():
            if tier_recs:
                tier_budget = sum(rec['allocated_budget'] for rec in tier_recs)
                tier_conversions = sum(rec['predicted_conversions'] for rec in tier_recs)
                tier_summary_data.append({
                    'Tier': tier_name,
                    'Influencers Count': len(tier_recs),
                    f'Total Budget': format_currency(tier_budget, market),
                    'Predicted Conversions': tier_conversions,
                    f'Average CAC': format_currency(tier_budget/tier_conversions, market) if tier_conversions > 0 else "N/A"
                })
        
        if tier_summary_data:
            tier_summary_df = pd.DataFrame(tier_summary_data)
            tier_summary_df.to_excel(writer, sheet_name='Tier Breakdown', index=False)
        
        # Sheet 4: Already Booked Influencers
        if booked_influencers:
            booked_df = pd.DataFrame([{
                'Influencer Name': inf.get('name', 'Unknown'),
                f'Spent Budget': format_currency(inf.get('budget_local', 0), market),
                'Status': 'Booked'
            } for inf in booked_influencers])
            booked_df.to_excel(writer, sheet_name='Booked Influencers', index=False)
        
        # Sheet 5: Campaign Summary
        currency_info = get_currency_info(market)
        currency = currency_info['name']
        summary_data = {
            'Campaign Details': [
                'Market', 'Period', 'Currency', 'Generated On', 'Total Influencers Recommended', 
                'Total Predicted Conversions', 'Gold Tier Count', 'Silver Tier Count', 'Bronze Tier Count'
            ],
            'Values': [
                market.upper(),
                f"{month.capitalize()} {year}",
                currency,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                len(recommendations),
                sum(rec['predicted_conversions'] for rec in recommendations),
                len(tier_breakdown.get('Gold', [])),
                len(tier_breakdown.get('Silver', [])),
                len(tier_breakdown.get('Bronze', []))
            ]
        }
        summary_df = pd.DataFrame(summary_data)
        summary_df.to_excel(writer, sheet_name='Campaign Summary', index=False)

    excel_buffer.seek(0)
    return excel_buffer

def create_llm_prompt_with_code_blocks(market, month, year, target_budget, actual_spend, remaining_budget,
                                     booked_influencers, recommendations, total_allocated, tier_breakdown):
    """Updated LLM prompt to generate code block formatted responses with local currency"""
    
    booked_list_str = "\n".join([f"- {inf['name']} (Spent: {format_currency(inf['budget_local'], market)})" 
                                for inf in booked_influencers]) or "None"

    # Create tier-wise breakdown for the prompt
    tier_summary = []
    for tier_name, tier_recs in tier_breakdown.items():
        if tier_recs:
            tier_budget = sum(rec['allocated_budget'] for rec in tier_recs)
            tier_conversions = sum(rec['predicted_conversions'] for rec in tier_recs)
            tier_summary.append(f"{tier_name}: {len(tier_recs)} influencers, {format_currency(tier_budget, market)}, {tier_conversions} conversions")

    if recommendations:
        total_conversions = sum(rec['predicted_conversions'] for rec in recommendations)
        avg_cac = total_allocated / total_conversions if total_conversions > 0 else 0
    else:
        total_conversions = 0
        avg_cac = 0

    currency_info = get_currency_info(market)
    currency = currency_info['name']

    prompt = f"""
You are a strategic marketing analyst bot. Generate a comprehensive multi-tier influencer marketing plan using code block table formatting for Slack.

**CONTEXT:**
- Market: {market}
- Currency: {currency}
- Period: {month.capitalize()} {year}
- Target Budget: {format_currency(target_budget, market)}
- Actual Spend: {format_currency(actual_spend, market)}
- Remaining Budget: {format_currency(remaining_budget, market)}
- Recommended Allocation: {format_currency(total_allocated, market)}
- Total Influencers: {len(recommendations)}
- Total Predicted Conversions: {total_conversions}

**ALREADY BOOKED INFLUENCERS:**
{booked_list_str}

**TIER BREAKDOWN:**
{json.dumps(tier_breakdown, indent=2)}

**FORMATTING REQUIREMENTS:**
Generate a response with the following sections using code blocks:

1. **Budget Overview Section:**
Budget Summary for {market.upper()} - {month.capitalize()} {year}
Target Budget: {format_currency(target_budget, market)}
Actual Spend: {format_currency(actual_spend, market)} ({(actual_spend/target_budget)*100:.1f}%)
Remaining Budget: {format_currency(remaining_budget, market)}
Recommended Allocation: {format_currency(total_allocated, market)} ({(total_allocated/remaining_budget)*100:.1f}%)
Budget Utilization: {(total_allocated/remaining_budget)*100:.1f}%
Currency: {currency}

code
Code
2. **Multi-Tier Strategy Overview:**
Multi-Tier Allocation Strategy
🥇 Gold Tier: [count] influencers | {currency} [budget] | [conversions] conversions
🥈 Silver Tier: [count] influencers | {currency} [budget] | [conversions] conversions
🥉 Bronze Tier: [count] influencers | {currency} [budget] | [conversions] conversions
Total Strategy: {len(recommendations)} influencers | {format_currency(total_allocated, market)} | {total_conversions} conversions
Average CAC: {format_currency(avg_cac, market)}

code
Code
3. **Detailed Influencer Recommendations (Top 15-20):**
Recommended Influencer Portfolio
Influencer Name	Tier	Budget ({currency})	Conv	CAC ({currency})	Performance
[Use actual data from recommendations to create this table with {currency} formatting]					
code
Code
4. **Already Booked Influencers (if any):**
Currently Booked Influencers
Influencer Name	Spent Budget ({currency})	Status
[List booked influencers if any with {currency} formatting]		
code
Code
5. **Strategic Insights:**
Provide 3-4 key insights about:
- Why this tier mix optimizes ROI for {market.upper()} market
- Budget utilization efficiency in {currency}
- Risk diversification across tiers
- Performance predictions for {market.upper()}

**IMPORTANT FORMATTING RULES:**
- Use proper table alignment with | separators
- Use = for major section dividers (80 chars)
- Use - for table headers and subsections
- Include tier emojis (🥇🥈🥉) for visual appeal
- Wrap all tables in code blocks with triple backticks
- Keep text sections outside code blocks
- Make sure tables are properly aligned
- Always use {currency} for currency formatting (not EUR)
- Include currency information in all relevant sections

Generate the complete response now:
"""
    return prompt

# --- 3. SLACK COMMAND HANDLER ---
@app.command("/plan")
def handle_plan_command(ack, say, command):
    ack()
    text = command.get('text', '').strip()
    
    # --- 1. Parse, Validate, and Standardize User Input ---
    parts = text.split('-')
    if len(parts) != 3:
        say("Sorry, I didn't understand that. Please use the format: `/plan Market-Month-Year` (e.g., `/plan UK-December-2025`).")
        return

    try:
        market, raw_month_input, year = parts[0].strip(), parts[1].strip(), int(parts[2].strip())
        month_abbr = USER_INPUT_TO_ABBR_MAP.get(raw_month_input.lower())
        if not month_abbr:
            say(f"Sorry, I don't recognize the month '{raw_month_input}'. Please use a full name or a 3-letter abbreviation.")
            return
        month_full = ABBR_TO_FULL_MONTH_MAP.get(month_abbr)
        
        # Get currency for the market
        currency_info = get_currency_info(market)
        currency = currency_info['name']
        
        say(f"Got it! I'm creating a strategic plan for *{market.upper()}* for *{raw_month_input.capitalize()} {year}* (Currency: *{currency}*). Please wait...")
    except (ValueError, IndexError):
        say("Invalid format. Please use `/plan Market-Month-Year` with a valid year.")
        return

    # --- 2. Fetch Base Data using CORRECT Month Formats ---
    target_filters = {"market": market, "month": month_abbr, "year": year}
    target_data = query_api(TARGET_API_URL, {"filters": target_filters}, "Targets")
    if "error" in target_data:
        say(f"Step 1/4 failed. API Error: `{target_data['error']}`")
        return

    actuals_filters = {"market": market, "month": month_full, "year": year}
    actual_data = query_api(ACTUALS_API_URL, {"filters": actuals_filters}, "Actuals")
    if "error" in actual_data:
        say(f"Step 2/4 failed. API Error: `{actual_data['error']}`")
        return

    # --- 3. Consolidate Base Data and Check Budget (Apply same logic as month.py) ---
    target_budget = target_data.get("kpis", {}).get("total_target_budget", 0)  # Already in local currency
    actual_spend_eur = actual_data.get("metrics", {}).get("budget_spent_eur", 0)  # This is in EUR
    actual_spend = convert_eur_to_local(actual_spend_eur, market)  # Convert to local currency
    booked_influencers = actual_data.get("influencers", [])
    booked_influencer_names = {inf['name'] for inf in booked_influencers}
    remaining_budget = target_budget - actual_spend

    if remaining_budget <= 0:
        # Format budget exhausted message in code block style
        budget_exhausted_msg = f"""**Budget Analysis Complete - {market.upper()} {raw_month_input.capitalize()} {year}**
Budget Status - No Further Allocation Possible
Target Budget: {format_currency(target_budget, market)}
Actual Spend: {format_currency(actual_spend, market)}
Remaining Budget: {format_currency(remaining_budget, market)}
Status: {'OVERSPENT' if remaining_budget < 0 else 'FULLY UTILIZED'}
Currency: {currency}

code
Code
🚫 Analysis Result: The budget for this market and period is {'overspent' if remaining_budget < 0 else 'fully utilized'}. No further influencer allocations are recommended."""
        
        say(budget_exhausted_msg)
        return

    # --- 4. Fetch All Tier Data ---
    say(f"Fetching available influencers across all tiers...")

    # Pass month_full instead of just year, since discovery API needs month
    gold_influencers = fetch_tier_influencers(market, year, month_full, "gold", booked_influencer_names)
    silver_influencers = fetch_tier_influencers(market, year, month_full, "silver", booked_influencer_names)
    bronze_influencers = fetch_tier_influencers(market, year, month_full, "bronze", booked_influencer_names)

    total_available = len(gold_influencers) + len(silver_influencers) + len(bronze_influencers)

    if total_available == 0:
        no_influencers_msg = f"""**Analysis Complete - {market.upper()} {raw_month_input.capitalize()} {year}**
Tier Availability Analysis
🥇 Gold Tier: {len(gold_influencers)} available
🥈 Silver Tier: {len(silver_influencers)} available
🥉 Bronze Tier: {len(bronze_influencers)} available
Total Available: {total_available} influencers

code
Code
✅ Excellent Work! All available Gold, Silver, and Bronze-tier influencers for this market have already been booked.
Budget Status
Remaining Budget: {format_currency(remaining_budget, market)}
Currency: {currency}
Recommendation: Consider expanding to additional markets or upcoming periods

code
"""
        say(no_influencers_msg)
        return
    
    say(f"Found influencers: *Gold: {len(gold_influencers)}*, *Silver: {len(silver_influencers)}*, *Bronze: {len(bronze_influencers)}*")
    say("Optimizing budget allocation across tiers...")
    
    # --- 5. Cascading Budget Allocation ---
    effective_cac = 50  # You can make this configurable based on market/currency
    recommendations, total_allocated, tier_breakdown = allocate_budget_cascading_tiers(
        gold_influencers, silver_influencers, bronze_influencers, remaining_budget, effective_cac, market
    )
    
    if not recommendations:
        say(f"No influencers found that fit within the remaining budget of {format_currency(remaining_budget, market)}.")
        return
    
    # --- 6. Generate Threaded Report and AI Summary ---
    say("Creating threaded report and AI summary...")
    thread_ts = None  # Initialize in case of early errors
    try:
        # Create a parent message to establish the thread, then get its timestamp
        parent_response = say(
            text=f"📊 Here is the multi-tier influencer plan for *{market.upper()}* - *{raw_month_input.capitalize()} {year}* ({currency}). I'll add the detailed report and AI summary to this thread."
        )
        thread_ts = parent_response['ts']

        # --- 6a. Generate and upload Excel report to the thread ---
        excel_buffer = create_excel_report(
            recommendations, tier_breakdown, market, raw_month_input, year,
            target_budget, actual_spend, remaining_budget, total_allocated, booked_influencers
        )
        filename = f"Multi_Tier_Plan_{market}_{raw_month_input}_{year}_{currency}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"

        app.client.files_upload_v2(
            channel=command.get('channel_id'),
            file=excel_buffer.getvalue(),
            filename=filename,
            title=f"Multi-Tier Plan - {market.upper()} {raw_month_input.capitalize()} {year} ({currency})",
            initial_comment="Detailed Excel report attached.",
            thread_ts=thread_ts
        )
        logger.success(f"Successfully uploaded Excel report to thread {thread_ts}")

        # --- 6b. Generate and post AI summary to the thread ---
        prompt = create_llm_prompt_with_code_blocks(
            market, raw_month_input, year, target_budget, actual_spend,
            remaining_budget, booked_influencers, recommendations,
            total_allocated, tier_breakdown
        )
        response = gemini_model.generate_content(prompt)
        ai_recommendation = response.text

        message_chunks = split_message_for_slack(ai_recommendation)

        store_thread_context(
            thread_ts, market, raw_month_input, year, currency, target_budget,
            actual_spend, remaining_budget, total_allocated, recommendations,
            tier_breakdown, booked_influencers
        )

        for i, chunk in enumerate(message_chunks):
            if i == 0:
                say(
                    text=f"📈 **Multi-Tier Strategic Plan - {market.upper()} {raw_month_input.capitalize()} {year} ({currency})**\n\n{chunk}",
                    thread_ts=thread_ts
                )
            else:
                say(text=chunk, thread_ts=thread_ts)

        say(
            text="💬 **Have questions about this plan?** Reply in this thread and mention me for detailed answers about the data, recommendations, or strategy!",
            thread_ts=thread_ts
        )

    except Exception as e:
        logger.error(f"Error creating report or summary: {e}")
        error_message = f"I encountered an error while generating the report and summary: `{str(e)}`"
        if thread_ts:
            say(text=error_message, thread_ts=thread_ts)
        else:
            say(text=error_message)
        return

    logger.success(f"Successfully completed multi-tier plan generation for {market}-{raw_month_input}-{year} in {currency}.")

# --- 4. THREAD REPLY HANDLER ---

@app.event("message")
def handle_thread_replies(event, say):
    """Handle thread replies and questions about the plan data"""
    # Only handle thread replies that mention the bot
    if (event.get('thread_ts') and 
        event.get('text') and 
        ('<@' in event.get('text', '') or 'bot' in event.get('text', '').lower())):
        
        thread_ts = event['thread_ts']
        user_message = event['text']
        user_id = event.get('user')
        
        # Get context for this thread
        context = get_thread_context(thread_ts)
        if not context:
            say(
                text="I don't have context for this thread. Please run a new `/plan` command to generate fresh data.",
                thread_ts=thread_ts
            )
            return
        
        logger.info(f"Handling thread question: {user_message}")
        
        try:
            # Create context-aware prompt for the question
            context_prompt = f"""
            You are a strategic marketing analyst bot answering a follow-up question about a previously generated influencer marketing plan.
            
            **PLAN CONTEXT:**
            - Market: {context['market'].upper()}
            - Period: {context['month']} {context['year']}
            - Currency: {context['currency']}
            - Target Budget: {format_currency(context['target_budget'], context['market'])}
            - Actual Spend: {format_currency(context['actual_spend'], context['market'])}
            - Remaining Budget: {format_currency(context['remaining_budget'], context['market'])}
            - Recommended Allocation: {format_currency(context['total_allocated'], context['market'])}
            - Total Recommended Influencers: {len(context['recommendations'])}
            - Booked Influencers: {len(context['booked_influencers'])}
            
            **TIER BREAKDOWN:**
            - Gold Tier: {len(context['tier_breakdown']['Gold'])} influencers
            - Silver Tier: {len(context['tier_breakdown']['Silver'])} influencers  
            - Bronze Tier: {len(context['tier_breakdown']['Bronze'])} influencers
            
            **DETAILED RECOMMENDATIONS DATA:**
            {json.dumps(context['recommendations'][:10], indent=2)}
            
            **BOOKED INFLUENCERS:**
            {json.dumps([{'name': inf['name'], 'budget': inf.get('budget_local', 0)} for inf in context['booked_influencers']], indent=2)}
            
            **USER QUESTION:** {user_message}
            
            **INSTRUCTIONS:**
            - Answer the user's question based on the plan context above
            - Use specific data from the recommendations and context
            - Keep responses concise but informative (max 500 words)
            - Use the correct currency ({context['currency']}) in all monetary references
            - If they ask about specific influencers, tiers, or budgets, provide exact data
            - If they ask for modifications or "what-if" scenarios, explain based on available data
            - Use code blocks for any data tables if relevant
            - Be conversational and helpful
            
            Answer the question now:
            """
            
            # Generate response using Gemini
            response = gemini_model.generate_content(context_prompt)
            ai_response = response.text
            
            # Post response in thread
            say(
                text=f"<@{user_id}> {ai_response}",
                thread_ts=thread_ts
            )
            
            logger.success(f"Successfully answered thread question for {context['market']}-{context['month']}-{context['year']}")
            
        except Exception as e:
            logger.error(f"Error handling thread question: {e}")
            say(
                text=f"<@{user_id}> I encountered an error processing your question: `{str(e)}`. Please try rephrasing your question.",
                thread_ts=thread_ts
            )

# --- 5. APP EXECUTION ---
if __name__ == "__main__":
    logger.info("🎯 Starting Enhanced Multi-Tier Strategic Planner Slack Bot...")
    try:
        handler = SocketModeHandler(app, SLACK_APP_TOKEN)
        logger.success("🚀 Bot is running and connected to Slack!")
        handler.start()
    except Exception as e:
        logger.critical(f"Failed to start the bot: {e}")
        sys.exit(1)
