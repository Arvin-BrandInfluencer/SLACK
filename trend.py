import os
import json
import requests
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
import google.generativeai as genai

# Load environment
load_dotenv()

# Initialize
app = App(token=os.environ["SLACK_BOT_TOKEN"])
genai.configure(api_key=os.environ["GOOGLE_API_KEY"])
model = genai.GenerativeModel('gemini-1.5-flash-latest')

BASE_API_URL = os.getenv("BASE_API_URL", "ttps://lyra-final.onrender.com")

# Currency mapping
CURRENCY_MAP = {
    'SWEDEN': 'SEK',
    'NORWAY': 'NOK', 
    'DENMARK': 'DKK',
    'UK': 'GBP',
    'FRANCE': 'EUR'
}

def get_currency_symbol(market):
    return CURRENCY_MAP.get(market.upper(), 'EUR')

@app.command("/influencer-trend")
def handle_influencer_trend_command(ack, say, command):
    ack()
    
    text = command.get('text', '').strip()
    if not text:
        say("Usage: `/influencer-trend Market-Year-Month-Tier` (only Market required)")
        return
    
    # Parse input
    parts = text.split('-')
    filters = {}
    filters['market'] = parts[0].strip()
    
    if len(parts) > 1 and parts[1].strip():
        filters['year'] = int(parts[1].strip())
    if len(parts) > 2 and parts[2].strip():
        filters['month'] = parts[2].strip().capitalize()
    if len(parts) > 3 and parts[3].strip():
        filters['tier'] = parts[3].strip().lower()
    
    # API call
    url = f"{BASE_API_URL}/api/influencer/query"
    payload = {
        "source": "influencer_analytics",
        "view": "discovery_tiers", 
        "filters": filters
    }
    
    print(f"Making API call to {url}")
    print(f"Payload: {json.dumps(payload, indent=2)}")
    
    try:
        response = requests.post(url, json=payload, timeout=30)
        print(f"API response status: {response.status_code}")
        print(f"API response text: {response.text[:500]}...")
        
        data = response.json()
        
        # The API returns data grouped by tiers: {"bronze": [...], "silver": [...], "gold": [...]}
        all_influencers = []
        for tier, tier_data in data.items():
            if isinstance(tier_data, list):
                all_influencers.extend(tier_data)
        
        say(f"🔍 Found {len(all_influencers)} influencers for: {filters}")
        
        if not all_influencers:
            say("❌ No data found")
            return
        
        # Create initial thread message
        initial_response = say(f"📊 **INFLUENCER PERFORMANCE ANALYSIS** - {filters.get('market', '').upper()} {filters.get('year', '')} {filters.get('month', '')}")
        thread_ts = initial_response['ts']
        
        # Get currency for this market
        currency = get_currency_symbol(filters.get('market', 'FRANCE'))
        
        influencers = all_influencers
        
        # Analysis and Excel-like tables
        # Best by Conversions (Top 25)
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
        
        # Best by CAC (only those with conversions > 0 and CAC > 0)
        with_conversions = [x for x in all_influencers if x.get('total_conversions', 0) > 0 and x.get('effective_cac_eur', 0) > 0]
        by_cac = sorted(with_conversions, key=lambda x: x.get('effective_cac_eur', float('inf')))[:15]
        cac_table = "```\nBEST 15 INFLUENCERS BY CAC (Lowest CAC, Non-Zero Only)\n"
        cac_table += f"Rank | Name                    | CAC ({currency})  | Conversions | CTR      | CVR\n"
        cac_table += "-" * 75 + "\n"
        for i, inf in enumerate(by_cac, 1):
            name = inf.get('influencer_name', 'N/A')[:20]
            cac = inf.get('effective_cac_eur', 0)
            conv = inf.get('total_conversions', 0)
            ctr = inf.get('avg_ctr', 0) * 100
            cvr = inf.get('avg_cvr', 0) * 100
            cac_table += f"{i:2d}   | {name:<20} | {cac:6.2f}   | {conv:8.0f}    | {ctr:6.3f}%  | {cvr:6.3f}%\n"
        cac_table += "```"
        say(text=cac_table, thread_ts=thread_ts)
        
        # Best by CTR
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
        say(text=ctr_table, thread_ts=thread_ts)
        
        # Best by Video Views
        by_views = sorted(all_influencers, key=lambda x: x.get('total_views', 0), reverse=True)[:15]
        views_table = "```\nBEST 15 INFLUENCERS BY VIDEO VIEWS\n"
        views_table += f"Rank | Name                    | Views      | CTR      | Conversions | Spend ({currency})\n"
        views_table += "-" * 85 + "\n"
        for i, inf in enumerate(by_views, 1):
            name = inf.get('influencer_name', 'N/A')[:20]
            views = inf.get('total_views', 0)
            ctr = inf.get('avg_ctr', 0) * 100
            conv = inf.get('total_conversions', 0)
            spend = inf.get('total_spend_eur', 0)
            views_table += f"{i:2d}   | {name:<20} | {views:8.0f}   | {ctr:6.3f}%  | {conv:8.0f}    | {spend:10.2f}\n"
        views_table += "```"
        say(text=views_table, thread_ts=thread_ts)
        
        # Group by Assets
        asset_groups = {}
        for inf in all_influencers:
            assets = inf.get('assets', [])
            asset_key = '+'.join(sorted(assets)) if assets else 'Unknown'
            if asset_key not in asset_groups:
                asset_groups[asset_key] = []
            asset_groups[asset_key].append(inf)
        
        asset_table = "```\nPERFORMANCE BY ASSET TYPE\n"
        asset_table += f"Asset Type     | Count | Avg Conv | Avg CAC ({currency}) | Total Spend ({currency})\n"
        asset_table += "-" * 75 + "\n"
        for asset_type, influencers in asset_groups.items():
            count = len(influencers)
            avg_conv = sum(inf.get('total_conversions', 0) for inf in influencers) / count
            total_spend = sum(inf.get('total_spend_eur', 0) for inf in influencers)
            with_conv = [inf for inf in influencers if inf.get('total_conversions', 0) > 0]
            avg_cac = sum(inf.get('effective_cac_eur', 0) for inf in with_conv) / len(with_conv) if with_conv else 0
            asset_table += f"{asset_type:<13} | {count:4d}  | {avg_conv:6.1f}   | {avg_cac:10.2f}    | {total_spend:12.2f}\n"
        asset_table += "```"
        say(text=asset_table, thread_ts=thread_ts)
        
        # WORST PERFORMERS - Zero Conversions = Zero CAC = Budget Wasted
        zero_conv = [x for x in all_influencers if x.get('total_conversions', 0) == 0]
        worst_by_spend = sorted(zero_conv, key=lambda x: x.get('total_spend_eur', 0), reverse=True)[:15]
        worst_table = "```\nWORST 15 PERFORMERS - ZERO CAC/CONVERSIONS (Pure Budget Waste)\n"
        worst_table += f"Rank | Name                    | Spend ({currency}) | Views      | Clicks   | CTR\n"
        worst_table += "-" * 80 + "\n"
        for i, inf in enumerate(worst_by_spend, 1):
            name = inf.get('influencer_name', 'N/A')[:20]
            spend = inf.get('total_spend_eur', 0)
            views = inf.get('total_views', 0)
            clicks = inf.get('total_clicks', 0)
            ctr = inf.get('avg_ctr', 0) * 100
            worst_table += f"{i:2d}   | {name:<20} | {spend:9.2f}   | {views:8.0f}   | {clicks:6.0f}   | {ctr:6.3f}%\n"
        worst_table += "```"
        say(text=worst_table, thread_ts=thread_ts)
        
        # AI analysis
        prompt = f"""
        Based on this influencer performance data for {filters.get('market', 'Unknown')}:
        
        Total Influencers: {len(all_influencers)}
        Best Performer by Conversions: {by_conversions[0]['influencer_name']} ({by_conversions[0]['total_conversions']} conversions)
        Best CAC: {by_cac[0]['influencer_name']} ({currency}{by_cac[0]['effective_cac_eur']:.2f}) if by_cac else 'N/A'
        Worst Performers: {len(zero_conv)} influencers with 0 conversions wasted {currency}{sum(inf.get('total_spend_eur', 0) for inf in zero_conv):.2f}
        
        Sample data: {json.dumps(all_influencers[:3], indent=2)}
        
        Give me:
        1. Overall performance summary (2-3 sentences)
        2. Key insight about conversion efficiency 
        3. Red flag about budget waste
        4. One actionable recommendation
        
        Be direct and data-focused.
        """
        
        ai_response = model.generate_content(prompt)
        say(text=f"🧠 **EXECUTIVE SUMMARY:**\n{ai_response.text}", thread_ts=thread_ts)
        
    except Exception as e:
        print(f"Error: {e}")
        say(f"❌ Error: {str(e)}")

if __name__ == "__main__":
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    print("🚀 Influencer bot started!")
    handler.start()
