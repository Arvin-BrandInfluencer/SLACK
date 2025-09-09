# ================================================
# FILE: main.py (FINAL VERSION - ADVANCED WEEKLY ROUTING)
# ================================================
import os
import sys
import json
import re
import collections
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from loguru import logger
import google.generativeai as genai

# Import the refactored CORE LOGIC functions from the modules
from month import run_monthly_review, handle_thread_messages as month_thread_handler
from influencer import run_influencer_analysis, handle_thread_messages as influencer_thread_handler
from trend import run_influencer_trend, handle_thread_messages as trend_thread_handler
from plan import run_strategic_plan, handle_thread_replies as plan_thread_handler
from weekly import run_weekly_review_by_range, run_weekly_review_by_number, handle_thread_messages as weekly_thread_handler

# --- Loguru Configuration ---
logger.remove()
logger.add(sys.stderr, format="<yellow>{time:YYYY-MM-DD HH:mm:ss}</yellow> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>", colorize=True)

# --- Environment & App Initialization ---
load_dotenv()
try:
    SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
    SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]
    GOOGLE_API_KEY = os.environ["GOOGLE_API_KEY"]
    app = App(token=SLACK_BOT_TOKEN)
    genai.configure(api_key=GOOGLE_API_KEY)
    gemini_model = genai.GenerativeModel('gemini-1.5-flash-latest')
    logger.success("Clients initialized.")
except KeyError as e:
    logger.critical(f"FATAL: Missing environment variable: {e}.")
    sys.exit(1)

# --- THREAD CONTEXT STORE ---
MAX_CONTEXTS = 20
thread_context_store = collections.OrderedDict()

# --- NATURAL LANGUAGE ROUTERS ---
def route_natural_language_query(query: str):
    prompt = f"""
    You are an expert routing assistant. Map a user query to a tool and extract parameters.

    **RULES:**
    1.  Default `year` to `2025` if not specified.
    2.  Normalize market names: "UK" should be "UK" (uppercase). All other countries (e.g., "france", "sweden") should be Sentence Case (e.g., "France", "Sweden").
    3.  If a query contains "week" or "wk" followed by a number (e.g., "week 36", "wk 5 performance"), you MUST prioritize the `weekly-review-by-number` tool.
    4.  If a query contains a specific date range (e.g., "from June 1 to June 15", "on Sep 15th"), you MUST prioritize the `weekly-review-by-range` tool.
    5.  For any tool requiring a `market`, if the user does NOT provide one, you MUST use the `clarify-market` tool. Do NOT return a null market.
    6.  ALWAYS generate `month_abbr` (3-letter) and `month_full` for monthly tools.
    7.  ALWAYS generate `start_date` and `end_date` in YYYY-MM-DD format for date range tools. If it's a single day, start and end dates are the same.

    **TOOLS:**
    - `monthly-review`: For a whole month. Needs `market`, `month_abbr`, `month_full`, `year`.
    - `weekly-review-by-range`: For a specific date range. Needs `market`, `start_date`, `end_date`, `year`.
    - `weekly-review-by-number`: For a specific week number. Needs `market`, `week_number`, `year`.
    - `analyse-influencer`: For a specific influencer. Needs `influencer_name`.
    - `influencer-trend`: For general leaderboards.
    - `plan`: For future budget allocation. Needs `market`, `month_abbr`, `month_full`, `year`.
    - `clarify-market`: Use if a market is required but missing. Needs `original_query`.

    **RESPONSE FORMAT:** JSON ONLY: `{{"tool_name": "...", "parameters": {{...}}}}`
    **USER QUERY:** "{query}"
    """
    try:
        response = gemini_model.generate_content(prompt)
        cleaned_text = response.text.strip().replace("```json", "").replace("```", "").strip()
        logger.info(f"LLM Router Response for query '{query}': {cleaned_text}")
        return json.loads(cleaned_text)
    except Exception as e:
        logger.error(f"Error parsing LLM response for routing: {e}")
        return {"tool_name": "error", "parameters": {"reason": "Could not understand the request."}}

def determine_thread_intent(user_message: str, context: dict):
    context_type = context.get('type', 'general discussion')
    context_params = context.get('params', {})
    prompt = f"""
    You are an intent detection expert for a Slack bot.
    The current context is `{context_type}`. The user's message is: "{user_message}"
    Your task is to determine if this is a `follow_up` or a `new_command`.

    **RULES:**
    1.  A `follow_up` asks a question answerable with the current context's data.
    2.  It is a `new_command` if the user asks for a different tool or introduces a new set of core parameters like a different month, a new market, a specific date range, or a specific week number.
        - Example `new_command`: Context is November, user asks "now show me June".
        - Example `new_command`: User asks "how about week 36?" during a monthly review.
    3.  If in doubt, default to `new_command`.

    Respond with JSON ONLY: `{{"intent": "follow-up"}}` or `{{"intent": "new_command"}}`
    """
    try:
        response = gemini_model.generate_content(prompt)
        cleaned_text = response.text.strip().replace("```json", "").replace("```", "").strip()
        logger.info(f"Thread Intent Detection: {cleaned_text}")
        return json.loads(cleaned_text).get("intent", "follow-up")
    except Exception as e:
        logger.error(f"Error determining thread intent: {e}")
        return "follow-up"

# --- PARAMETER PROCESSING & NORMALIZATION ---
def normalize_market_name(market_name: str) -> str:
    if not market_name or not isinstance(market_name, str):
        return market_name
    
    market_lower = market_name.strip().lower()
    
    market_map = {
        "uk": "UK",
        "united kingdom": "UK",
        "gb": "UK",
        "great britain": "UK",
        "france": "France",
        "fr": "France",
        "sweden": "Sweden",
        "se": "Sweden",
        "norway": "Norway",
        "no": "Norway",
        "denmark": "Denmark",
        "dk": "Denmark",
        "nordics": "Nordics",
    }
    
    # Return mapped value, or capitalize the original if not found as a fallback.
    return market_map.get(market_lower, market_name.strip().capitalize())

def process_routing_params(params: dict) -> dict:
    if not isinstance(params, dict):
        params = {}
    
    # Normalize market name
    if 'market' in params and params.get('market'):
        params['market'] = normalize_market_name(params['market'])
        logger.info(f"Normalized market name to: {params['market']}")
        
    # Apply default year
    if 'year' not in params or not params.get('year'):
        params['year'] = 2025
        logger.info("Applied default year: 2025")
        
    return params

# --- PRIMARY ENTRY POINT: @mention ---
@app.event("app_mention")
def handle_app_mention(event, say, client):
    user_query = re.sub(r'<@.*?>', '', event['text']).strip()
    thread_ts = event.get('ts')
    if not user_query:
        say(text="Hello! I'm Nova, how can I help?", thread_ts=thread_ts); return

    thinking_message = say(f"Of course! Let me look into: \"_{user_query}_\"...", thread_ts=thread_ts)
    routing_decision = route_natural_language_query(user_query)

    tool_name = routing_decision.get("tool_name")
    params = process_routing_params(routing_decision.get("parameters", {}))

    if tool_name == "clarify-market":
        client.chat_update(channel=event['channel'], ts=thinking_message['ts'], text=f"I can help with that! Which market are you interested in for the query: \"_{params.get('original_query')}_\"?")
        return
    
    if tool_name in ["monthly-review", "weekly-review-by-range", "weekly-review-by-number", "plan"] and not params.get("market"):
        client.chat_update(channel=event['channel'], ts=thinking_message['ts'], text=f"It looks like a market is missing for that request. Which market should I analyze?")
        return
        
    if tool_name and tool_name != "error":
        client.chat_update(channel=event['channel'], ts=thinking_message['ts'], text=f"Understood! Preparing a `*{tool_name}*` analysis for you...")
    else:
        reason = params.get('reason', "I couldn't understand that.")
        client.chat_update(channel=event['channel'], ts=thinking_message['ts'], text=f"My apologies, {reason} Could you please rephrase?")
        return
    
    main_handler_map = {
        "monthly-review": run_monthly_review, 
        "weekly-review-by-range": run_weekly_review_by_range,
        "weekly-review-by-number": run_weekly_review_by_number,
        "analyse-influencer": run_influencer_analysis, 
        "influencer-trend": run_influencer_trend,
        "plan": run_strategic_plan
    }
    if handler := main_handler_map.get(tool_name):
        if tool_name == 'plan':
            handler(client, say, event, thread_ts, params, thread_context_store)
        else:
            handler(say, thread_ts, params, thread_context_store, user_query=user_query)

    while len(thread_context_store) > MAX_CONTEXTS:
        thread_context_store.popitem(last=False)

# --- THREAD MESSAGE ROUTING ---
@app.event("message")
def route_thread_messages(event, say, client):
    thread_ts = event.get("thread_ts")
    if not thread_ts or event.get("bot_id"): return
    
    if thread_ts in thread_context_store:
        thread_context_store.move_to_end(thread_ts)
        context = thread_context_store[thread_ts]
        user_message = event.get("text", "").strip()
        
        intent = determine_thread_intent(user_message, context)

        if intent == "new_command":
            logger.info(f"Thread message '{user_message}' identified as a new command. Pivoting...")
            routing_decision = route_natural_language_query(user_message)
            new_tool = routing_decision.get("tool_name")
            params = process_routing_params(routing_decision.get("parameters", {}))
            
            if new_tool == "clarify-market":
                say(f"I can do that! Which market are you interested in for: \"_{params.get('original_query')}_\"?", thread_ts=thread_ts)
                return
            if new_tool in ["monthly-review", "weekly-review-by-range", "weekly-review-by-number", "plan"] and not params.get("market"):
                say(f"It looks like a market is missing for that request. Which market should I analyze?", thread_ts=thread_ts)
                return

            if new_tool and new_tool != "error":
                say(f"Pivoting to a new analysis: *{new_tool}*...", thread_ts=thread_ts)
                main_handler_map = {
                    "monthly-review": run_monthly_review, 
                    "weekly-review-by-range": run_weekly_review_by_range,
                    "weekly-review-by-number": run_weekly_review_by_number,
                    "analyse-influencer": run_influencer_analysis, 
                    "influencer-trend": run_influencer_trend,
                    "plan": run_strategic_plan
                }
                if handler := main_handler_map.get(new_tool):
                    if new_tool == 'plan':
                        handler(client, say, event, thread_ts, params, thread_context_store)
                    else:
                        handler(say, thread_ts, params, thread_context_store, user_query=user_message)
            else:
                say(f"Sorry, I couldn't understand that as a new command.", thread_ts=thread_ts)
            return

        logger.info(f"Thread message '{user_message}' identified as a follow-up.")
        follow_up_handler_map = {
            "monthly_review": month_thread_handler, 
            "weekly_review_by_range": weekly_thread_handler,
            "weekly_review_by_number": weekly_thread_handler,
            "influencer_analysis": influencer_thread_handler, 
            "strategic_plan": plan_thread_handler,
            "influencer_trend": trend_thread_handler
        }
        if handler := follow_up_handler_map.get(context.get("type")):
            handler(event, say, client, context)

# --- SLASH COMMANDS ---
@app.command("/monthly-review")
def route_monthly_review(ack, say, command):
    ack()
    text = command.get('text', '').strip()
    initial_response = say(f"Running command `/monthly-review {text}`...")
    routing_decision = route_natural_language_query(f"monthly review for {text.replace('-', ' ')}")
    tool_name = routing_decision.get("tool_name")
    params = process_routing_params(routing_decision.get("parameters", {}))
    if tool_name == "monthly-review":
        run_monthly_review(say, initial_response['ts'], params, thread_context_store)
    else:
        say("Invalid format.", thread_ts=initial_response['ts'])

@app.command("/weekly-review")
def route_weekly_review(ack, say, command):
    ack()
    text = command.get('text', '').strip()
    initial_response = say(f"Running command `/weekly-review {text}`...")
    routing_decision = route_natural_language_query(f"weekly review for {text}")
    tool_name = routing_decision.get("tool_name")
    params = process_routing_params(routing_decision.get("parameters", {}))
    
    if tool_name == "weekly-review-by-range" and params.get("market"):
        run_weekly_review_by_range(say, initial_response['ts'], params, thread_context_store)
    elif tool_name == "weekly-review-by-number" and params.get("market"):
        run_weekly_review_by_number(say, initial_response['ts'], params, thread_context_store)
    else:
        say("Invalid format. Use `/weekly-review UK from 2025-06-01 to 2025-06-07` or `/weekly-review UK week 36`", thread_ts=initial_response['ts'])

@app.command("/analyse-influencer")
def route_analyse_influencer(ack, say, command):
    ack()
    text = command.get('text', '').strip()
    initial_response = say(f"Running command `/analyse-influencer {text}`...")
    routing_decision = route_natural_language_query(f"analyse influencer {text.replace('-', ' ')}")
    tool_name = routing_decision.get("tool_name")
    params = process_routing_params(routing_decision.get("parameters", {}))
    if tool_name == "analyse-influencer":
        run_influencer_analysis(say, initial_response['ts'], params, thread_context_store)
    else:
        say("Invalid format.", thread_ts=initial_response['ts'])

@app.command("/influencer-trend")
def route_influencer_trend(ack, say, command):
    ack()
    text = command.get('text', '').strip()
    initial_response = say(f"Running command `/influencer-trend {text}`...")
    routing_decision = route_natural_language_query(f"influencer trends for {text.replace('-', ' ')}")
    tool_name = routing_decision.get("tool_name")
    params = process_routing_params(routing_decision.get("parameters", {}))
    if tool_name == "influencer-trend":
        run_influencer_trend(say, initial_response['ts'], params, thread_context_store)
    else:
        say("Invalid format.", thread_ts=initial_response['ts'])

@app.command("/plan")
def route_plan(ack, say, command, client):
    ack()
    text = command.get('text', '').strip()
    initial_response = say(f"Running command `/plan {text}`...")
    routing_decision = route_natural_language_query(f"plan for {text.replace('-', ' ')}")
    tool_name = routing_decision.get("tool_name")
    params = process_routing_params(routing_decision.get("parameters", {}))
    if tool_name == "plan":
         mock_event = {'channel': command.get('channel_id')}
         run_strategic_plan(client, say, mock_event, initial_response['ts'], params, thread_context_store)
    else:
        say("Invalid format. Use `/plan Market-Month-Year`", thread_ts=initial_response['ts'])

@app.command("/bot-status")
def handle_bot_status(ack, say):
    ack()
    say("Bot Status: All systems operational!")


# --- MAIN APPLICATION STARTUP ---
if __name__ == "__main__":
    logger.info("Starting Unified Slack Bot...")
    try:
        handler = SocketModeHandler(app, SLACK_APP_TOKEN)
        logger.success("Bot is running!")
        handler.start()
    except Exception as e:
        logger.critical(f"Failed to start the bot: {e}")
        sys.exit(1)
