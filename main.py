# ================================================
# FILE: main.py (FIXED - MARKET CASE NORMALIZATION)
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
    # CORRECTED PROMPT: Changed "Title Case" to "UPPER CASE" for market normalization.
    prompt = f"""
    You are an expert routing assistant. Map a user query to a tool and extract parameters.
    Default `year` to `2025`. Normalize `market` to UPPER CASE (e.g., "UK", "FRANCE"). ALWAYS generate `month_abbr` (3-letter) and `month_full`.

    **TOOLS:**
    - `monthly-review`: For past performance. Needs `market`, `month_abbr`, `month_full`, `year`.
    - `analyse-influencer`: For a specific influencer. Needs `influencer_name`.
    - `influencer-trend`: For leaderboards and comparisons.
    - `plan`: For future budget allocation. Needs `market`, `month_abbr`, `month_full`, `year`.

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
    The current conversation context is a `{context_type}` with these parameters: `{json.dumps(context_params)}`.
    The user just sent this message: "{user_message}"

    Your task is to determine if this is a `follow_up` to the current context or a `new_command`.

    **RULES:**
    1.  A `follow_up` is a question that can be answered using the data from the CURRENT context. It includes simple questions ("who was best?"), clarifications ("why?"), or modifications to the existing context ("what about in France?").
    2.  A `new_command` explicitly asks for a DIFFERENT tool or a completely different set of core parameters (e.g., asking for a "plan" during a "monthly-review", or asking for a "June review" when the context is "November").
    3.  A question that tries to COMPARE the current context with a past, unstated context (e.g., "how many of these influencers were also used in june?") is a `follow_up`. The tool's handler is responsible for gracefully explaining its memory limitations.

    **Analysis:**
    - User message: "{user_message}"
    - Current context: `{context_type}` for `{json.dumps(context_params)}`
    - Does the message ask for a fundamentally different analysis type (e.g., `plan` vs `review`)? If so, `new_command`.
    - Does the message ask for a completely different time period that isn't a simple comparison (e.g., `do a full review of june`)? If so, `new_command`.
    - Otherwise, it's a `follow_up`.

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

    if tool_name and tool_name != "error":
        client.chat_update(channel=event['channel'], ts=thinking_message['ts'], text=f"Understood! Preparing a `*{tool_name}*` analysis for you...")
    else:
        reason = params.get('reason', "I couldn't understand that.")
        client.chat_update(channel=event['channel'], ts=thinking_message['ts'], text=f"My apologies, {reason} Could you please rephrase?")
        return

    main_handler_map = {"monthly-review": run_monthly_review, "analyse-influencer": run_influencer_analysis, "influencer-trend": run_influencer_trend, "plan": run_strategic_plan}
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

            if new_tool and new_tool != "error":
                say(f"Pivoting to a new analysis: *{new_tool}*...", thread_ts=thread_ts)
                main_handler_map = {"monthly-review": run_monthly_review, "analyse-influencer": run_influencer_analysis, "influencer-trend": run_influencer_trend, "plan": run_strategic_plan}
                if handler := main_handler_map.get(new_tool):
                    if new_tool == 'plan':
                        handler(client, say, event, thread_ts, params, thread_context_store)
                    else:
                        handler(say, thread_ts, params, thread_context_store, user_query=user_message)
            else:
                say(f"Sorry, I couldn't understand that as a new command.", thread_ts=thread_ts)
            return

        logger.info(f"Thread message '{user_message}' identified as a follow-up.")
        follow_up_handler_map = {"monthly_review": month_thread_handler, "influencer_analysis": influencer_thread_handler, "strategic_plan": plan_thread_handler, "influencer_trend": trend_thread_handler}
        if handler := follow_up_handler_map.get(context.get("type")):
            handler(event, say, client, context)

# --- SLASH COMMANDS ---
# ... (No changes needed in the slash command section) ...
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
