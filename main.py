# ================================================
# FILE: main.py (FINAL VERSION - ROBUST ROUTING & VALIDATION)
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
from weekly import run_weekly_review, handle_thread_messages as weekly_thread_handler

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
    # --- MODIFIED PROMPT: STRONGER RULES FOR DATES AND MARKETS ---
    prompt = f"""
    You are an expert routing assistant. Map a user query to a tool and extract parameters.

    **RULES:**
    1.  Default `year` to `2025` if not specified.
    2.  Normalize `market` to UPPER CASE (e.g., "UK", "FRANCE").
    3.  For any tool requiring a `market` (`monthly-review`, `weekly-review`, `plan`), if the user does NOT provide one, you MUST use the `clarify-market` tool. Do NOT return a null market.
    4.  If the query contains a specific date range (e.g., "from June 1 to June 15", "between sep 1 and 30"), you MUST prioritize the `weekly-review` tool.
    5.  ALWAYS generate `month_abbr` (3-letter) and `month_full` for monthly tools.
    6.  ALWAYS generate `start_date` and `end_date` in YYYY-MM-DD format for weekly tools.

    **TOOLS:**
    - `monthly-review`: For past performance of a whole month. Needs `market`, `month_abbr`, `month_full`, `year`.
    - `weekly-review`: For performance in a specific date range. Needs `market`, `start_date`, `end_date`, `year`.
    - `analyse-influencer`: For a specific influencer. Needs `influencer_name`.
    - `influencer-trend`: For general leaderboards and comparisons.
    - `plan`: For future budget allocation. Needs `market`, `month_abbr`, `month_full`, `year`.
    - `clarify-market`: Use this if a market is required but missing. Needs `original_query`.

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
    # --- MODIFIED PROMPT: STRONGER RULES FOR PIVOTING ---
    prompt = f"""
    You are an intent detection expert for a Slack bot.
    The current conversation context is a `{context_type}` with parameters: `{json.dumps(context_params)}`.
    The user's message is: "{user_message}"

    Your task is to determine if this is a `follow_up` or a `new_command`.

    **RULES:**
    1.  A `follow_up` asks a question that can be answered using data from the CURRENT context. Examples: "who was best?", "why?", "show me the details".
    2.  It is a `new_command` if the user explicitly asks for a DIFFERENT tool (e.g., "make a plan" during a "review").
    3.  It is a `new_command` if the user introduces a completely new set of core parameters, such as a different month, a new market, or a specific date range.
        - Example `new_command`: Context is November, user asks "now show me June".
        - Example `new_command`: Context is a trend report, user asks "give me a weekly analysis for Sep 1 to 10". This introduces a date range, making it a new command.
    4.  If in doubt, default to `new_command` to allow for re-routing.

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

def _apply_default_year(params: dict):
    if not isinstance(params, dict): params = {}
    if 'year' not in params or not params['year']:
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
    params = routing_decision.get("parameters", {})
    params = _apply_default_year(params)

    # --- NEW: VALIDATION AND CLARIFICATION LOGIC ---
    if tool_name == "clarify-market":
        client.chat_update(channel=event['channel'], ts=thinking_message['ts'], text=f"I can help with that! Which market are you interested in for the query: \"_{params.get('original_query')}_\"?")
        return
    
    if tool_name in ["monthly-review", "weekly-review", "plan"] and not params.get("market"):
        client.chat_update(channel=event['channel'], ts=thinking_message['ts'], text=f"It looks like a market is missing for that request. Which market should I analyze?")
        return
        
    if tool_name and tool_name != "error":
        client.chat_update(channel=event['channel'], ts=thinking_message['ts'], text=f"Understood! Preparing a `*{tool_name}*` analysis for you...")
    else:
        reason = params.get('reason', "I couldn't understand that.")
        client.chat_update(channel=event['channel'], ts=thinking_message['ts'], text=f"My apologies, {reason} Could you please rephrase?")
        return
    
    main_handler_map = {
        "monthly-review": run_monthly_review, "weekly-review": run_weekly_review,
        "analyse-influencer": run_influencer_analysis, "influencer-trend": run_influencer_trend,
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
            params = routing_decision.get("parameters", {})
            params = _apply_default_year(params)
            
            # --- NEW: VALIDATION AND CLARIFICATION LOGIC IN THREADS ---
            if new_tool == "clarify-market":
                say(f"I can do that! Which market are you interested in for: \"_{params.get('original_query')}_\"?", thread_ts=thread_ts)
                return
            
            if new_tool in ["monthly-review", "weekly-review", "plan"] and not params.get("market"):
                say(f"It looks like a market is missing for that request. Which market should I analyze?", thread_ts=thread_ts)
                return

            if new_tool and new_tool != "error":
                say(f"Pivoting to a new analysis: *{new_tool}*...", thread_ts=thread_ts)
                main_handler_map = {
                    "monthly-review": run_monthly_review, "weekly-review": run_weekly_review,
                    "analyse-influencer": run_influencer_analysis, "influencer-trend": run_influencer_trend,
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
            "monthly_review": month_thread_handler, "weekly_review": weekly_thread_handler,
            "influencer_analysis": influencer_thread_handler, "strategic_plan": plan_thread_handler,
            "influencer_trend": trend_thread_handler
        }
        if handler := follow_up_handler_map.get(context.get("type")):
            handler(event, say, client, context)

# --- SLASH COMMANDS ---
# ... (Slash command section is unchanged but included for completeness) ...
@app.command("/monthly-review")
def route_monthly_review(ack, say, command):
    ack()
    text = command.get('text', '').strip()
    initial_response = say(f"Running command `/monthly-review {text}`...")
    routing_decision = route_natural_language_query(f"monthly review for {text.replace('-', ' ')}")
    tool_name = routing_decision.get("tool_name")
    params = routing_decision.get("parameters", {})
    params = _apply_default_year(params)
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
    params = routing_decision.get("parameters", {})
    params = _apply_default_year(params)
    if tool_name == "weekly-review" and params.get("market"):
        run_weekly_review(say, initial_response['ts'], params, thread_context_store)
    else:
        say("Invalid format. Use `/weekly-review UK from 2025-06-01 to 2025-06-07`", thread_ts=initial_response['ts'])

@app.command("/analyse-influencer")
def route_analyse_influencer(ack, say, command):
    ack()
    text = command.get('text', '').strip()
    initial_response = say(f"Running command `/analyse-influencer {text}`...")
    routing_decision = route_natural_language_query(f"analyse influencer {text.replace('-', ' ')}")
    tool_name = routing_decision.get("tool_name")
    params = routing_decision.get("parameters", {})
    params = _apply_default_year(params)
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
    params = routing_decision.get("parameters", {})
    params = _apply_default_year(params)
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
    params = routing_decision.get("parameters", {})
    params = _apply_default_year(params)
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
