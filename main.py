# ================================================
# FILE: main.py (Unified Context Architecture)
# ================================================
import os
import sys
import json
import re
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from loguru import logger
import google.generativeai as genai

# Import the refactored CORE LOGIC functions from the modules
from month import run_monthly_review, handle_thread_messages as month_thread_handler
from influencer import run_influencer_analysis, handle_thread_messages as influencer_thread_handler
from trend import run_influencer_trend # Note: trend.py may not have a thread handler
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

# --- ⭐️ UNIFIED THREAD CONTEXT STORE ⭐️ ---
# This single dictionary manages the context for ALL threaded conversations.
thread_context_store = {}

# --- 🧠 NATURAL LANGUAGE ROUTER (LLM Function) ---
def route_natural_language_query(query: str):
    """Uses Gemini to classify user intent and extract entities for routing."""
    prompt = f"""
    You are an intelligent routing assistant for a marketing analytics Slack bot.
    Your task is to analyze the user's query and map it to one of the available tools.
    You must extract the required parameters for that tool and respond in a specific JSON format.

    **AVAILABLE TOOLS:**

    1.  **`monthly-review`**:
        - **Description**: Generates a performance review for a specific market, month, and year.
        - **Parameters**: `market` (string), `month` (string), `year` (integer).
        - **Example Queries**: "show me the monthly review for UK in December 2025", "how did France do in january 2025"

    2.  **`analyse-influencer`**:
        - **Description**: Provides a detailed analysis of a single influencer. Month and year are optional.
        - **Parameters**: `influencer_name` (string), `month` (string, optional), `year` (integer, optional).
        - **Example Queries**: "analyse influencer stylebyanna for jan 2025", "performance of home_on_the_commons"

    3.  **`influencer-trend`**:
        - **Description**: Shows trend reports and leaderboards. All parameters are optional filters.
        - **Parameters**: `market` (string, optional), `month` (string, optional), `year` (integer, optional), `tier` (string, optional: "gold", "silver", "bronze").
        - **Example Queries**: "show me influencer trends for the UK", "top trends in france for 2025"

    4.  **`plan`**:
        - **Description**: Creates a strategic budget allocation plan for a future month.
        - **Parameters**: `market` (string), `month` (string), `year` (integer).
        - **Example Queries**: "plan the budget for UK in November 2025", "create a plan for france december 2025"

    **RESPONSE FORMAT:**
    - If you can confidently identify the tool and all required parameters, respond with ONLY a valid JSON object.
    - If the query is ambiguous or missing information, respond with `tool_name: "error"`.

    **JSON Structure:**
    `{{"tool_name": "...", "parameters": {{...}}}}`

    ---
    **USER QUERY:** "{query}"
    ---
    Analyze the query and provide the JSON output now.
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
def handle_app_mention(event, say):
    """Handles direct mentions to the bot, creating a thread for the conversation."""
    user_query = re.sub(r'<@.*?>', '', event['text']).strip()
    channel_id = event['channel']
    thread_ts = event.get('ts') # Use the timestamp of the mention to start the thread

    if not user_query:
        say(text="Hello! How can I help? Mention me with a question.", thread_ts=thread_ts)
        return

    thinking_message = say(f"🤔 Thinking about your request: \"_{user_query}_\"...", thread_ts=thread_ts)
    
    routing_decision = route_natural_language_query(user_query)
    tool_name = routing_decision.get("tool_name")
    params = routing_decision.get("parameters", {})

    app.client.chat_update(
        channel=channel_id,
        ts=thinking_message['ts'],
        text=f"✅ Understood! Routing to `*{tool_name}*` analysis. Fetching data now..."
    )

    # Route to the appropriate handler function
    if tool_name == "monthly-review":
        run_monthly_review(say, thread_ts, params, thread_context_store)
    elif tool_name == "analyse-influencer":
        run_influencer_analysis(say, thread_ts, params, thread_context_store)
    elif tool_name == "influencer-trend":
        run_influencer_trend(say, thread_ts, params, thread_context_store)
    elif tool_name == "plan":
        run_strategic_plan(say, thread_ts, params, thread_context_store)
    else:
        app.client.chat_update(
            channel=channel_id,
            ts=thinking_message['ts'],
            text=f"😕 Sorry, I couldn't quite understand that. Please be specific, for example: `show me the monthly review for UK in December 2025`"
        )

# --- THREAD MESSAGE ROUTING ---
@app.event("message")
def route_thread_messages(event, say):
    """
    Routes thread messages to the correct handler based on the context
    stored in our unified `thread_context_store`.
    """
    thread_ts = event.get("thread_ts")
    if not thread_ts or event.get("bot_id"):
        return

    if thread_ts in thread_context_store:
        context = thread_context_store[thread_ts]
        context_type = context.get("type")
        logger.info(f"Routing thread message in {thread_ts} to handler of type: '{context_type}'")

        if context_type == "monthly_review":
            month_thread_handler(event, say, context)
        elif context_type == "influencer_analysis":
            influencer_thread_handler(event, say, context)
        elif context_type == "strategic_plan":
            plan_thread_handler(event, say, context)
        # Add other handlers here if they support threading

# --- 🛠️ LEGACY SLASH COMMANDS (Refactored to call core logic) ---

@app.command("/monthly-review")
def route_monthly_review(ack, say, command):
    ack()
    text = command.get('text', '').strip()
    parts = text.split('-')
    if len(parts) != 3:
        say("Invalid format. Use `/monthly-review Market-Month-Year`")
        return
    params = {'market': parts[0].strip(), 'month': parts[1].strip(), 'year': parts[2].strip()}
    initial_response = say(f"Running command `/monthly-review` for *{params['market']}*...")
    run_monthly_review(say, initial_response['ts'], params, thread_context_store)

@app.command("/analyse-influencer")
def route_analyse_influencer(ack, say, command):
    ack()
    text = command.get('text', '').strip()
    parts = [p.strip() for p in text.split('-') if p.strip()]
    if not parts:
        say("Invalid format. Use `/analyse-influencer name - [year] - [month]`")
        return
    params = {"influencer_name": parts[0]}
    if len(parts) > 1: params['year'] = parts[1]
    if len(parts) > 2: params['month'] = parts[2]
    initial_response = say(f"Running command `/analyse-influencer` for *{params['influencer_name']}*...")
    run_influencer_analysis(say, initial_response['ts'], params, thread_context_store)

@app.command("/influencer-trend")
def route_influencer_trend(ack, say, command):
    ack()
    text = command.get('text', '').strip()
    parts = [p.strip() for p in text.split('-') if p.strip()]
    params = {}
    if len(parts) > 0: params['market'] = parts[0]
    if len(parts) > 1: params['year'] = parts[1]
    if len(parts) > 2: params['month'] = parts[2]
    if len(parts) > 3: params['tier'] = parts[3]
    initial_response = say(f"Running command `/influencer-trend`...")
    run_influencer_trend(say, initial_response['ts'], params, thread_context_store)

@app.command("/plan")
def route_plan(ack, say, command):
    ack()
    text = command.get('text', '').strip()
    parts = text.split('-')
    if len(parts) != 3:
        say("Invalid format. Use `/plan Market-Month-Year`")
        return
    params = {'market': parts[0].strip(), 'month': parts[1].strip(), 'year': parts[2].strip()}
    initial_response = say(f"Running command `/plan` for *{params['market']}*...")
    run_strategic_plan(say, initial_response['ts'], params, thread_context_store)


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
