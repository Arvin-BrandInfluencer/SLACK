# ================================================
# FILE: main.py (Unified Context Architecture)
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
logger.add(
    sys.stderr,
    format="<yellow>{time:YYYY-MM-DD HH:mm:ss}</yellow> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>",
    colorize=True
)

# --- Environment & App Initialization ---
load_dotenv()
try:
    SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
    SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]
    GOOGLE_API_KEY = os.environ["GOOGLE_API_KEY"]
    
    app = App(token=SLACK_BOT_TOKEN)
    logger.success("Main Slack App initialized successfully.")
    
    genai.configure(api_key=GOOGLE_API_KEY)
    gemini_model = genai.GenerativeModel('gemini-1.5-flash-latest')
    logger.success("Gemini client initialized for router.")
    
except KeyError as e:
    logger.critical(f"FATAL: Missing environment variable: {e}. Please check your .env file.")
    sys.exit(1)

# --- ⭐️ UNIFIED THREAD CONTEXT STORE (with pruning) ⭐️ ---
MAX_CONTEXTS = 20
thread_context_store = collections.OrderedDict()

# --- 🧠 ENHANCED NATURAL LANGUAGE ROUTER (LLM Function) ---
def route_natural_language_query(query: str):
    """
    Uses Gemini to classify user intent, extract entities, AND NORMALIZE them for routing.
    This is the single point of intelligence for handling messy user input.
    """
    prompt = f"""
    You are an expert routing and normalization assistant for a marketing analytics Slack bot.
    Your task is to analyze the user's query, map it to a tool, and extract AND NORMALIZE all parameters into the exact format required.

    **PARAMETER NORMALIZATION RULES (STRICT):**

    1.  **`market`**:
        - User might type "uk", "United Kingdom", "fr", "France", "FR".
        - You MUST normalize this to one of the following exact, case-sensitive strings:
          `"UK"`, `"France"`, `"Sweden"`, `"Norway"`, `"Denmark"`, `"Nordics"`

    2.  **`month`**:
        - User might type "january", "Jan", "jan.", "janu", or misspell it like "janury".
        - If a month is present, you MUST generate TWO formats in the output:
          - `month_abbr`: The 3-letter abbreviation with a capital letter (e.g., "Jan", "Feb").
          - `month_full`: The full month name with a capital letter (e.g., "January", "February").

    3. **`tier`**:
        - User might type "Gold tier", "golden", "gld".
        - You MUST normalize this to one of the following exact, lowercase strings:
          `"gold"`, `"silver"`, `"bronze"`

    **AVAILABLE TOOLS:**

    1.  **`monthly-review`**:
        - **Description**: Generates a performance review for a specific market, month, and year.
        - **Parameters**: `market`, `month_abbr`, `month_full`, `year`.
        - **Example**: "show me review for uk in decembr 2025" -> `{{"tool_name": "monthly-review", "parameters": {{"market": "UK", "month_abbr": "Dec", "month_full": "December", "year": 2025}}}}`

    2.  **`analyse-influencer`**:
        - **Description**: Provides a detailed analysis of a single influencer. Month and year are optional.
        - **Parameters**: `influencer_name`, `month_abbr` (optional), `month_full` (optional), `year` (optional).
        - **Example**: "analyse influencer stylebyanna for janury 2025" -> `{{"tool_name": "analyse-influencer", "parameters": {{"influencer_name": "stylebyanna", "month_abbr": "Jan", "month_full": "January", "year": 2025}}}}`

    3.  **`influencer-trend`**:
        - **Description**: Shows trend reports and leaderboards. All parameters are optional filters.
        - **Parameters**: `market` (optional), `month_abbr` (optional), `month_full` (optional), `year` (optional), `tier` (optional).
        - **Example**: "top golden influencers in fr for 2025" -> `{{"tool_name": "influencer-trend", "parameters": {{"market": "France", "tier": "gold", "year": 2025}}}}`

    4.  **`plan`**:
        - **Description**: Creates a strategic budget allocation plan for a future month.
        - **Parameters**: `market`, `month_abbr`, `month_full`, `year`.
        - **Example**: "plan the budget for UK in novemb 2025" -> `{{"tool_name": "plan", "parameters": {{"market": "UK", "month_abbr": "Nov", "month_full": "November", "year": 2025}}}}`

    **RESPONSE FORMAT:**
    - If you can confidently identify the tool and NORMALIZE all required parameters, respond with ONLY a valid JSON object.
    - If the query is ambiguous or missing information, respond with `{{"tool_name": "error", "parameters": {{"reason": "Ambiguous or missing information"}}}}`.
    - Do not add any commentary. Only the JSON.

    ---
    **USER QUERY:** "{query}"
    ---
    Analyze and normalize the query, then provide the JSON output now.
    """
    try:
        response = gemini_model.generate_content(prompt)
        cleaned_text = response.text.strip().replace("```json", "").replace("```", "").strip()
        logger.info(f"LLM Router Response: {cleaned_text}")
        return json.loads(cleaned_text)
    except Exception as e:
        logger.error(f"Error parsing LLM response for routing: {e}")
        return {"tool_name": "error", "parameters": {"reason": "Could not understand the request."}}

# --- ✅ PRIMARY ENTRY POINT: @mention ---
@app.event("app_mention")
def handle_app_mention(event, say, client):
    """Handles direct mentions to the bot, creating a thread for the conversation."""
    user_query = re.sub(r'<@.*?>', '', event['text']).strip()
    channel_id = event['channel']
    thread_ts = event.get('ts')

    if not user_query:
        say(text="Hello! How can I help? Mention me with a question.", thread_ts=thread_ts)
        return

    thinking_message = say(f"🤔 Thinking about your request: \"_{user_query}_\"...", thread_ts=thread_ts)
    
    routing_decision = route_natural_language_query(user_query)
    tool_name = routing_decision.get("tool_name")
    params = routing_decision.get("parameters", {})

    if tool_name and tool_name != "error":
        client.chat_update(
            channel=channel_id,
            ts=thinking_message['ts'],
            text=f"✅ Understood! Routing to `*{tool_name}*` analysis. Fetching data now..."
        )
    else:
        client.chat_update(
            channel=channel_id,
            ts=thinking_message['ts'],
            text=f"😕 Sorry, I couldn't quite understand that. Please be specific, for example: `show me the monthly review for UK in December 2025`"
        )
        return

    # Call the appropriate handler which will populate the context store
    if tool_name == "monthly-review":
        run_monthly_review(say, thread_ts, params, thread_context_store)
    elif tool_name == "analyse-influencer":
        run_influencer_analysis(say, thread_ts, params, thread_context_store)
    elif tool_name == "influencer-trend":
        run_influencer_trend(say, thread_ts, params, thread_context_store)
    elif tool_name == "plan":
        run_strategic_plan(client, say, event, thread_ts, params, thread_context_store)

    # Prune the context store after the request is handled
    while len(thread_context_store) > MAX_CONTEXTS:
        oldest_thread, _ = thread_context_store.popitem(last=False)
        logger.info(f"Context store full ({len(thread_context_store)} > {MAX_CONTEXTS}). Pruned oldest context for thread: {oldest_thread}")


# --- THREAD MESSAGE ROUTING ---
@app.event("message")
def route_thread_messages(event, say):
    thread_ts = event.get("thread_ts")
    if not thread_ts or event.get("bot_id"):
        return

    if thread_ts in thread_context_store:
        # Refresh the position of the accessed thread to keep it from being pruned
        thread_context_store.move_to_end(thread_ts)
        
        context = thread_context_store[thread_ts]
        context_type = context.get("type")
        logger.info(f"Routing thread message in {thread_ts} to handler of type: '{context_type}'")

        if context_type == "monthly_review":
            month_thread_handler(event, say, context)
        elif context_type == "influencer_analysis":
            influencer_thread_handler(event, say, context)
        elif context_type == "strategic_plan":
            plan_thread_handler(event, say, context)
        elif context_type == "influencer_trend":
            trend_thread_handler(event, say, context)

# --- 🛠️ LEGACY SLASH COMMANDS (Refactored to call core logic) ---

@app.command("/monthly-review")
def route_monthly_review(ack, say, command):
    ack()
    text = command.get('text', '').strip()
    initial_response = say(f"Running command `/monthly-review {text}`...")
    routing_decision = route_natural_language_query(f"monthly review for {text.replace('-', ' ')}")
    params = routing_decision.get("parameters", {})
    if routing_decision.get("tool_name") == "monthly-review":
        run_monthly_review(say, initial_response['ts'], params, thread_context_store)
    else:
        say("Invalid format. Use `/monthly-review Market-Month-Year`", thread_ts=initial_response['ts'])


@app.command("/analyse-influencer")
def route_analyse_influencer(ack, say, command):
    ack()
    text = command.get('text', '').strip()
    initial_response = say(f"Running command `/analyse-influencer {text}`...")
    routing_decision = route_natural_language_query(f"analyse influencer {text.replace('-', ' ')}")
    params = routing_decision.get("parameters", {})
    if routing_decision.get("tool_name") == "analyse-influencer":
        run_influencer_analysis(say, initial_response['ts'], params, thread_context_store)
    else:
        say("Invalid format. Use `/analyse-influencer name - [year] - [month]`", thread_ts=initial_response['ts'])


@app.command("/influencer-trend")
def route_influencer_trend(ack, say, command):
    ack()
    text = command.get('text', '').strip()
    initial_response = say(f"Running command `/influencer-trend {text}`...")
    routing_decision = route_natural_language_query(f"influencer trends for {text.replace('-', ' ')}")
    params = routing_decision.get("parameters", {})
    if routing_decision.get("tool_name") == "influencer-trend":
        run_influencer_trend(say, initial_response['ts'], params, thread_context_store)
    else:
        say("Invalid format. Use `/influencer-trend [Market]-[Year]-[Month]-[Tier]`", thread_ts=initial_response['ts'])

@app.command("/plan")
def route_plan(ack, say, command, client):
    ack()
    text = command.get('text', '').strip()
    initial_response = say(f"Running command `/plan {text}`...")
    routing_decision = route_natural_language_query(f"plan for {text.replace('-', ' ')}")
    params = routing_decision.get("parameters", {})
    if routing_decision.get("tool_name") == "plan":
         run_strategic_plan(client, say, command, initial_response['ts'], params, thread_context_store)
    else:
        say("Invalid format. Use `/plan Market-Month-Year`", thread_ts=initial_response['ts'])

@app.command("/bot-status")
def handle_bot_status(ack, say):
    ack()
    say("🤖 **Bot Status:** All systems operational!\n\n**Primary Usage:**\n• Just mention me (`@botname`) and ask your question in natural language!")

# --- MAIN APPLICATION STARTUP ---
if __name__ == "__main__":
    logger.info("🎯 Starting Unified Slack Bot with @mention Listener...")
    try:
        handler = SocketModeHandler(app, SLACK_APP_TOKEN)
        logger.success("🚀 Unified Bot is running and connected to Slack!")
        handler.start()
    except Exception as e:
        logger.critical(f"Failed to start the unified bot: {e}")
        sys.exit(1)
