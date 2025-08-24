import os
import sys
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from loguru import logger

# Import command handlers from respective modules
from month import handle_monthly_review_command, handle_thread_messages as month_thread_handler
from influencer import handle_analyse_influencer_command, handle_thread_messages as influencer_thread_handler
from trend import handle_influencer_trend_command
# --- MODIFIED LINE ---
# The function for handling thread messages in your new plan module is called 'handle_thread_replies'.
# We update the import to match it, while keeping the alias 'plan_thread_handler' for consistency.
from plan import handle_plan_command, handle_thread_replies as plan_thread_handler

# --- Loguru Configuration ---
logger.remove()
logger.add(
    sys.stderr,
    format="<yellow>{time:YYYY-MM-DD HH:mm:ss}</yellow> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>",
    colorize=True
)

# --- Environment & Slack App Initialization ---
load_dotenv()

try:
    SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
    SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]
    
    # Initialize the main Slack app
    app = App(token=SLACK_BOT_TOKEN)
    logger.success("Main Slack App initialized successfully.")
    
except KeyError as e:
    logger.critical(f"FATAL: Missing environment variable: {e}. Please check your .env file.")
    sys.exit(1)

# --- COMMAND ROUTING ---

@app.command("/monthly-review")
def route_monthly_review(ack, say, command):
    """Route monthly review command to month.py handler"""
    logger.info("Routing /monthly-review command to month.py")
    return handle_monthly_review_command(ack, say, command)

@app.command("/analyse-influencer")  
def route_analyse_influencer(ack, say, command):
    """Route influencer analysis command to influencer.py handler"""
    logger.info("Routing /analyse-influencer command to influencer.py")
    return handle_analyse_influencer_command(ack, say, command)

@app.command("/influencer-trend")
def route_influencer_trend(ack, say, command):
    """Route influencer trend command to trend.py handler"""
    logger.info("Routing /influencer-trend command to trend.py")
    return handle_influencer_trend_command(ack, say, command)

@app.command("/plan")
def route_plan(ack, say, command):
    """Route plan command to plan.py handler"""
    logger.info("Routing /plan command to plan.py")
    return handle_plan_command(ack, say, command)

# --- THREAD MESSAGE ROUTING ---
# We need to route thread messages to the appropriate handler based on context

@app.event("message")
def route_thread_messages(event, say):
    """
    Route thread messages to appropriate handlers.
    Each module should handle their own thread context.
    """
    # Skip if not a thread message or if it's a bot message
    if not event.get("thread_ts") or event.get("bot_id"):
        return
    
    logger.info(f"Routing thread message in thread {event['thread_ts']}")
    
    # Try each handler - they will check if they have context for this thread
    try:
        # Try month handler first
        month_thread_handler(event, say)
    except Exception as e:
        logger.debug(f"Month handler skipped thread {event['thread_ts']}: {e}")
    
    try:
        # Try influencer handler
        influencer_thread_handler(event, say)
    except Exception as e:
        logger.debug(f"Influencer handler skipped thread {event['thread_ts']}: {e}")
    
    # Try plan handler
    try:
        plan_thread_handler(event, say)
    except Exception as e:
        logger.debug(f"Plan handler skipped thread {event['thread_ts']}: {e}")
    
    # Note: trend.py doesn't seem to have thread handling, so we skip it

# --- HEALTH CHECK ENDPOINT (Optional) ---
@app.command("/bot-status")
def handle_bot_status(ack, say):
    """Simple health check command"""
    ack()
    say("🤖 **Bot Status:** All systems operational!\n\n**Available Commands:**\n• `/monthly-review Market-Month-Year`\n• `/analyse-influencer name - [year] - [month]`\n• `/influencer-trend Market-Year-Month-Tier`\n• `/plan Your planning query`")

# --- ERROR HANDLING ---
@app.error
def custom_error_handler(error, body, logger):
    logger.error(f"Error: {error}")
    logger.error(f"Request body: {body}")

# --- MAIN APPLICATION STARTUP ---
if __name__ == "__main__":
    logger.info("🎯 Starting Unified Slack Bot Router...")
    logger.info("📋 Available Commands:")
    logger.info("   • /monthly-review -> month.py")
    logger.info("   • /analyse-influencer -> influencer.py") 
    logger.info("   • /influencer-trend -> trend.py")
    logger.info("   • /plan -> plan.py")
    
    try:
        handler = SocketModeHandler(app, SLACK_APP_TOKEN)
        logger.success("🚀 Unified Bot is running and connected to Slack!")
        logger.info("All commands are being routed to their respective handlers.")
        handler.start()
        
    except Exception as e:
        logger.critical(f"Failed to start the unified bot: {e}")
        sys.exit(1)
