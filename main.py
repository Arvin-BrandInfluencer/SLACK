import os
import requests
import json
import logging
import sys
import re
import threading
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from langfuse import observe, Langfuse

# --- Configuration and Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Load environment variables from .env file
load_dotenv()

# Environment variables with fallback error handling
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")  # Support both variable names
INFLUENCER_API_URL = os.getenv("INFLUENCER_API_URL", "https://lyra-5a7f.onrender.com/query")
LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY")
LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY")
LANGFUSE_HOST = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")
HEALTH_CHECK_PORT = int(os.getenv("HEALTH_CHECK_PORT", "8080"))

# Custom error message
CUSTOM_ERROR_MESSAGE = "Sorry, I am unable to answer this question .. cc @arvin"

# Validate required environment variables
missing_vars = []
if not SLACK_BOT_TOKEN: missing_vars.append("SLACK_BOT_TOKEN")
if not SLACK_APP_TOKEN: missing_vars.append("SLACK_APP_TOKEN")
if not (GEMINI_API_KEY or GOOGLE_API_KEY): missing_vars.append("GEMINI_API_KEY or GOOGLE_API_KEY")
if not LANGFUSE_PUBLIC_KEY: missing_vars.append("LANGFUSE_PUBLIC_KEY")
if not LANGFUSE_SECRET_KEY: missing_vars.append("LANGFUSE_SECRET_KEY")

if missing_vars:
    logging.error(f"FATAL: Missing required environment variables: {', '.join(missing_vars)}")
    logging.error("Please check your .env file and ensure all required variables are set.")
    sys.exit(1)

# Initialize Slack app
app = App(token=SLACK_BOT_TOKEN)

# Initialize Langfuse
langfuse = Langfuse(
    public_key=LANGFUSE_PUBLIC_KEY,
    secret_key=LANGFUSE_SECRET_KEY,
    host=LANGFUSE_HOST,
)

# --- Health Check Server ---

class HealthCheckHandler(BaseHTTPRequestHandler):
    """Simple health check endpoint handler"""
    
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            
            # Basic check for Langfuse connection (ping the host)
            langfuse_status = "ok"
            try:
                response = requests.get(LANGFUSE_HOST, timeout=5)
                if response.status_code != 200:
                    langfuse_status = "error"
            except Exception:
                langfuse_status = "error"
            
            # Check influencer API connection
            api_status = "ok"
            try:
                test_query = {"source": "dashboard", "filters": {"market": "All", "year": "2024"}}
                response = requests.post(INFLUENCER_API_URL, json=test_query, timeout=5)
                if response.status_code != 200:
                    api_status = "error"
            except Exception:
                api_status = "error"
            
            health_status = {
                "status": "healthy",
                "timestamp": datetime.now().isoformat(),
                "service": "slack-influencer-analytics-bot",
                "checks": {
                    "slack_connection": "ok" if SLACK_BOT_TOKEN and SLACK_APP_TOKEN else "error",
                    "langfuse_connection": langfuse_status,
                    "influencer_api": api_status,
                    "gemini_api": "ok" if GOOGLE_API_KEY else "error"
                }
            }
            
            self.wfile.write(json.dumps(health_status, indent=2).encode())
        else:
            self.send_response(404)
            self.end_headers()
    
    def log_message(self, format, *args):
        # Suppress default HTTP server logs to avoid cluttering
        pass

def start_health_check_server():
    """Start the health check server in a separate thread"""
    try:
        server = HTTPServer(("0.0.0.0", HEALTH_CHECK_PORT), HealthCheckHandler)
        logging.info(f"🏥 Health check server started on port {HEALTH_CHECK_PORT}")
        server.serve_forever()
    except Exception as e:
        logging.error(f"Failed to start health check server: {e}")

# --- In-Memory Conversation History ---
conversation_history = defaultdict(list)
HISTORY_CLEANUP_HOURS = 24
MAX_HISTORY_PER_THREAD = 20

# --- Helper Functions ---

def cleanup_old_conversations():
    """Remove conversations older than HISTORY_CLEANUP_HOURS"""
    cutoff_time = datetime.now() - timedelta(hours=HISTORY_CLEANUP_HOURS)
    threads_to_remove = [
        thread_id for thread_id, messages in conversation_history.items()
        if messages and messages[-1]["timestamp"] < cutoff_time
    ]
    for thread_id in threads_to_remove:
        del conversation_history[thread_id]
        logging.info(f"Cleaned up old conversation for thread: {thread_id}")

def add_to_conversation_history(thread_id, role, content):
    """Add a message to the conversation history for a thread"""
    conversation_history[thread_id].append({
        "role": role, "content": content, "timestamp": datetime.now()
    })
    if len(conversation_history[thread_id]) > MAX_HISTORY_PER_THREAD:
        conversation_history[thread_id] = conversation_history[thread_id][-MAX_HISTORY_PER_THREAD:]
    if len(conversation_history) % 10 == 0:
        cleanup_old_conversations()

def get_conversation_context(thread_id):
    """Get the conversation history for a thread as a formatted string"""
    if thread_id not in conversation_history:
        return ""
    context_messages = [
        f"{'User' if msg['role'] == 'user' else 'Assistant'}: {msg['content']}"
        for msg in conversation_history[thread_id]
    ]
    return "\n".join(context_messages) if context_messages else ""

# --- Influencer Analytics API Functions ---

def query_influencer_api(payload):
    """Query the influencer analytics API"""
    try:
        response = requests.post(
            INFLUENCER_API_URL,
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=30
        )
        
        if response.status_code == 200:
            return response.json()
        else:
            return {"error": f"API Error: {response.status_code} - {response.text}"}
            
    except requests.exceptions.RequestException as e:
        return {"error": f"Connection error: {str(e)}"}

def call_gemini_api(prompt):
    """Call Gemini API with the given prompt"""
    api_key = GOOGLE_API_KEY
    if not api_key:
        return "Please configure your Gemini API key in the environment variables."
    
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-exp:generateContent"
    headers = {'Content-Type': 'application/json', 'X-goog-api-key': api_key}
    data = {"contents": [{"parts": [{"text": prompt}]}]}
    
    try:
        response = requests.post(url, headers=headers, json=data, timeout=30)
        response.raise_for_status()
        result = response.json()
        
        if 'candidates' in result and result.get('candidates'):
            if 'content' in result['candidates'][0] and 'parts' in result['candidates'][0]['content']:
                return result['candidates'][0]['content']['parts'][0]['text']
        
        return CUSTOM_ERROR_MESSAGE
        
    except requests.exceptions.RequestException as e:
        logging.error(f"Gemini API Error: {e}")
        return CUSTOM_ERROR_MESSAGE
    except (KeyError, IndexError) as e:
        logging.error(f"Gemini response parsing error: {e}")
        return CUSTOM_ERROR_MESSAGE
    except Exception as e:
        logging.error(f"Unexpected Gemini error: {e}")
        return CUSTOM_ERROR_MESSAGE

def analyze_question_complexity(user_question):
    """Analyze if question requires single or multiple API calls"""
    prompt = f"""
Analyze this user question to determine if it requires single or multiple API calls:

USER QUERY: "{user_question}"

CLASSIFICATION RULES:
1. SINGLE QUERY - Simple, direct questions about one data source:
   - "Top 10 influencers by spend"
   - "Monthly breakdown for UK" 
   - "Target vs actual for France"
   - "Gold tier influencers"

2. MULTI-STEP QUERY - Complex questions requiring multiple data sources and analysis:
   - Questions about budget planning with remaining spend
   - Comparative analysis across different data sources
   - Questions requiring target data + actual spend + influencer recommendations
   - Questions requiring data from dashboard + monthly + summary views

INDICATORS OF MULTI-STEP QUERIES:
- "budget planning", "remaining budget", "optimize", "recommend"
- "based on target", "depending on spend", "how to allocate"
- "compare performance and suggest", "analyze and recommend"
- Questions that need: targets AND spending AND influencer selection

RESPONSE FORMAT:
{{
  "complexity": "single" or "multi-step",
  "reasoning": "Brief explanation why"
}}

Return ONLY valid JSON.
"""

    try:
        response = call_gemini_api(prompt)
        
        if response == CUSTOM_ERROR_MESSAGE:
            return {"complexity": "single", "reasoning": "Error in analysis"}
        
        json_str = response.strip()
        if json_str.startswith("```"):
            json_str = json_str.split("```")[1]
            if json_str.startswith("json"):
                json_str = json_str[4:]
        
        return json.loads(json_str)
        
    except Exception as e:
        logging.error(f"Error analyzing question complexity: {str(e)}")
        return {"complexity": "single", "reasoning": "Error in analysis"}

def extract_entities_and_generate_query(user_question):
    """
    Extracts entities from user query and generates a single, compliant API query.
    """
    
    prompt = f"""
You are a meticulous API integration assistant. Your ONLY job is to convert a user's question into a valid JSON payload for the Brand Influence Query API.

USER QUERY: "{user_question}"

--- API DOCUMENTATION ---

**Endpoint:** `{INFLUENCER_API_URL}` (POST request)

**1. Source: `dashboard`**
   - **Purpose:** High-level, monthly Target vs. Actual performance metrics.
   - **Payload:** {{"source": "dashboard", "filters": {{"market": "UK", "year": "2025"}}}}
   - **Filter `market`:** "UK", "France", "Sweden", "Norway", "Denmark", "Nordics", "All"
   - **Filter `year`:** "2025", "2024", "All"
   - **NOTE:** `dashboard` source does NOT support `view`, `sort`, or `limit` parameters.

**2. Source: `influencer_analytics`**
   - **Purpose:** Detailed influencer-centric analytics.
   - **`view` parameter is REQUIRED.**
   - **Common Filters:** `market`, `year` (same values as dashboard).

   **2.1. View: `summary`**
      - **Purpose:** Unique influencers with lifetime performance stats. Useful for finding top/worst performers.
      - **Payload:** {{"source": "influencer_analytics", "view": "summary", "filters": {{...}}, "sort": {{...}}, "limit": <number>}}
      - **Sortable fields (`sort.by`):** `campaign_count`, `total_conversions`, `total_views`, `total_clicks`, `total_spend_eur`, `effective_cac_eur`, `avg_ctr`, `avg_cvr`.
      - **Sort order (`sort.order`):** "asc", "desc".
      - **Limit:** Integer to limit number of records (only for summary view).

   **2.2. View: `discovery_tiers`**
      - **Purpose:** Ranks influencers into Gold, Silver, Bronze tiers by `effective_cac_eur`.
      - **Payload:** {{"source": "influencer_analytics", "view": "discovery_tiers", "filters": {{...}}}}
      - **NOTE:** This view does not support `sort` or `limit` parameters.

   **2.3. View: `monthly_breakdown`**
      - **Purpose:** Groups campaigns by month with summary and details.
      - **Payload:** {{"source": "influencer_analytics", "view": "monthly_breakdown", "filters": {{...}}}}
      - **NOTE:** This view does not support `sort` or `limit` parameters.

--- CRITICAL INSTRUCTIONS ---
1. **If the year is not specified, default to "2024".**
2. **If the market is not specified, default to "All".**
3. **Use `limit` for "top N", "best N", or "worst N" queries in `summary` view.**
4. **`influencer_analytics` ALWAYS requires a `view` parameter.**
5. **`dashboard` NEVER has a `view`, `sort`, or `limit` parameter.**

Return ONLY the raw, valid JSON object. No explanations, no markdown, no commentary. Just the JSON.
"""

    try:
        response = call_gemini_api(prompt)
        
        if response == CUSTOM_ERROR_MESSAGE:
            return None
        
        json_str = response.strip()
        if json_str.startswith("```"):
            json_str = json_str.split("```")[1]
            if json_str.startswith("json"):
                json_str = json_str[4:]
        
        return json.loads(json_str)
        
    except Exception as e:
        logging.error(f"Error generating query: {str(e)}")
        return None

def generate_multi_step_queries(user_question):
    """
    Generate multiple API queries for complex questions.
    """
    
    prompt = f"""
You are a strategic planner that breaks down complex user questions into a sequence of precise API calls.

USER QUERY: "{user_question}"

--- API DOCUMENTATION ---

**Endpoint:** `{INFLUENCER_API_URL}` (POST request)

**1. Source: `dashboard`**
   - **Purpose:** High-level, monthly Target vs. Actual performance metrics.
   - **Payload:** {{"source": "dashboard", "filters": {{"market": "UK", "year": "2025"}}}}
   - **Filter `market`:** "UK", "France", "Sweden", "Norway", "Denmark", "Nordics", "All"
   - **Filter `year`:** "2025", "2024", "All"

**2. Source: `influencer_analytics`**
   - **Purpose:** Detailed influencer-centric analytics.
   - **`view` parameter is REQUIRED.**

   **2.1. View: `summary`** - Unique influencers with lifetime performance stats
   **2.2. View: `discovery_tiers`** - Ranks influencers into Gold, Silver, Bronze tiers
   **2.3. View: `monthly_breakdown`** - Groups campaigns by month

--- RESPONSE FORMAT ---
{{
  "queries": [
    {{
      "step": 1,
      "purpose": "Brief purpose description",
      "query": {{"source": "...", "filters": {{...}}}}
    }}
  ],
  "final_analysis_needed": "Description of analysis needed after data collection"
}}

Return ONLY valid JSON.
"""

    try:
        response = call_gemini_api(prompt)
        
        if response == CUSTOM_ERROR_MESSAGE:
            return None
        
        json_str = response.strip()
        if json_str.startswith("```"):
            json_str = json_str.split("```")[1]
            if json_str.startswith("json"):
                json_str = json_str[4:]
        
        return json.loads(json_str)
        
    except Exception as e:
        logging.error(f"Error generating multi-step queries: {str(e)}")
        return None

def execute_multi_step_queries(query_plan):
    """
    Executes multiple queries, continuing even if one step fails.
    """
    results = {}
    
    for query_step in query_plan["queries"]:
        step_num = query_step["step"]
        purpose = query_step["purpose"]
        query = query_step["query"]
        
        logging.info(f"Executing Step {step_num}: {purpose}")
        
        response = query_influencer_api(query)
        
        if "error" in response:
            logging.error(f"Error in Step {step_num}: {response['error']}")
            results[f"step_{step_num}"] = {
                "purpose": purpose,
                "query": query,
                "error": response['error']
            }
            continue 
        else:
            logging.info(f"Step {step_num} completed successfully")
            results[f"step_{step_num}"] = {
                "purpose": purpose,
                "query": query,
                "data": response
            }
    
    return results

def compose_multi_step_answer(user_query, all_results, final_analysis_needed):
    """
    Compose comprehensive answer, handling failed steps.
    """
    
    data_summary = []
    for step_key, step_data in all_results.items():
        if "error" in step_data:
            summary_item = f"**Data from '{step_data['purpose']}' FAILED to load.**\nError: {step_data['error']}"
            data_summary.append(summary_item)
        else:
            summary_item = f"**Data from '{step_data['purpose']}':**\n{json.dumps(step_data.get('data', 'No data returned'), indent=2)}"
            data_summary.append(summary_item)

    combined_data = "\n\n---\n\n".join(data_summary)
    
    prompt = f"""
You are an expert influencer marketing strategist analyzing complex multi-step data.

ORIGINAL USER QUERY: "{user_query}"

MULTI-STEP DATA COLLECTED:
{combined_data}

ANALYSIS REQUIRED: {final_analysis_needed}

COMPOSE A COMPREHENSIVE STRATEGIC ANSWER FOR SLACK:

REQUIREMENTS:
- Provide a concise, actionable response (max 300 words)
- Use clear, business-focused language
- Include specific numbers when available
- If some data failed to load, acknowledge it but work with available data
- Focus on key insights and recommendations
- Use bullet points for clarity
- No markdown formatting (plain text for Slack)

Provide the best possible strategic analysis given the available data.
"""

    try:
        response = call_gemini_api(prompt)
        return response if response != CUSTOM_ERROR_MESSAGE else "Unable to analyze the data properly."
        
    except Exception as e:
        logging.error(f"Error composing multi-step answer: {str(e)}")
        return "Error occurred while analyzing the data."

def compose_answer_with_llm(user_query, api_data):
    """Compose natural language answer using LLM for single queries"""
    prompt = f"""
You are an expert influencer marketing analyst responding in Slack.

USER QUERY: "{user_query}"

API RESPONSE DATA: {json.dumps(api_data, indent=2)}

COMPOSE A CONCISE SLACK RESPONSE:

REQUIREMENTS:
- Keep response under 300 words for Slack readability
- Be direct and business-focused
- Include specific numbers and key metrics
- Use bullet points for clarity when needed
- No markdown formatting (plain text for Slack)
- Focus on actionable insights
- For dashboard data: Compare targets vs actuals
- For influencer data: Highlight top performers and key metrics

Present insights naturally and concisely.
"""

    try:
        response = call_gemini_api(prompt)
        return response if response != CUSTOM_ERROR_MESSAGE else "Unable to analyze the data properly."
        
    except Exception as e:
        logging.error(f"Error composing answer: {str(e)}")
        return "Error occurred while analyzing the data."

def create_response_blocks(answer_text, trace_id):
    """Creates Slack message blocks, including feedback buttons if a trace_id is available"""
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": answer_text}}]
    if trace_id and answer_text != CUSTOM_ERROR_MESSAGE:
        blocks.append({
            "type": "actions",
            "block_id": f"feedback_{trace_id}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "👍 Accurate", "emoji": True},
                    "style": "primary",
                    "value": f"up_{trace_id}",
                    "action_id": "feedback_button_up"
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "👎 Inaccurate", "emoji": True},
                    "style": "danger",
                    "value": f"down_{trace_id}",
                    "action_id": "feedback_button_down"
                }
            ]
        })
    return blocks

@observe(name="slack-influencer-analytics-trace")
def process_user_query(user_id, session_id, user_question):
    """Orchestrates the influencer analytics query process with Langfuse tracing"""
    langfuse.update_current_trace(user_id=user_id, session_id=session_id)
    
    add_to_conversation_history(session_id, "user", user_question)
    
    try:
        # Analyze question complexity
        complexity_analysis = analyze_question_complexity(user_question)
        logging.info(f"Question complexity: {complexity_analysis['complexity']}")
        
        with langfuse.start_as_current_generation(
            name="gemini-2.0-flash-analytics-generation",
            model="gemini-2.0-flash-exp",
            input=user_question
        ) as generation:
            
            if complexity_analysis["complexity"] == "multi-step":
                # Multi-step query handling
                logging.info("Processing multi-step query")
                
                query_plan = generate_multi_step_queries(user_question)
                if not query_plan:
                    answer = "I couldn't understand your complex question. Please try rephrasing it or ask about specific metrics."
                else:
                    # Execute multiple queries
                    all_results = execute_multi_step_queries(query_plan)
                    
                    if all_results:
                        # Compose comprehensive answer
                        answer = compose_multi_step_answer(
                            user_question, 
                            all_results, 
                            query_plan['final_analysis_needed']
                        )
                    else:
                        answer = "Unable to fetch the required data for your complex query."
            
            else:
                # Single query handling
                logging.info("Processing single query")
                
                api_query = extract_entities_and_generate_query(user_question)
                
                if api_query:
                    logging.info(f"Generated API query: {json.dumps(api_query)}")
                    api_response = query_influencer_api(api_query)
                    
                    if "error" in api_response:
                        answer = f"API Error: {api_response['error']}"
                    else:
                        answer = compose_answer_with_llm(user_question, api_response)
                else:
                    answer = "I couldn't understand your question. Please try rephrasing it or ask about specific influencer metrics."
            
            generation.update(output=answer)
    
    except Exception as e:
        logging.error(f"Error processing query: {e}")
        answer = CUSTOM_ERROR_MESSAGE
    
    add_to_conversation_history(session_id, "assistant", answer)
    trace_id = langfuse.get_current_trace_id()
    logging.info(f"Generated trace_id: {trace_id} for user {user_id}")
    return answer, trace_id

# --- Slack Event Handlers ---

@app.event("app_mention")
def handle_app_mention(event, say, logger):
    """Handle when the bot is mentioned in a channel"""
    try:
        user_question = re.sub(r'<@.*?>', '', event['text']).strip()
        user_id = event['user']
        channel_id = event['channel']
        thread_ts = event.get("thread_ts", event["ts"])
        session_id = f"{channel_id}_{thread_ts}"
        
        logger.info(f"Received influencer analytics question from user {user_id} in channel {channel_id} (session: {session_id}): '{user_question}'")
        
        thinking_message = say(text="🎯 Analyzing your influencer data...", thread_ts=thread_ts)
        
        if not user_question:
            answer = "Hello! 🎯 I'm your Influencer Analytics Assistant. Ask me about influencer performance, targets vs actuals, top performers, or budget analysis!"
            app.client.chat_update(
                channel=thinking_message['channel'],
                ts=thinking_message['ts'],
                text=answer
            )
        else:
            answer, trace_id = process_user_query(user_id, session_id, user_question)
            response_blocks = create_response_blocks(answer, trace_id)
            
            app.client.chat_update(
                channel=thinking_message['channel'],
                ts=thinking_message['ts'],
                text=answer,
                blocks=response_blocks
            )
            
    except Exception as e:
        logger.error(f"Error in app_mention: {e}", exc_info=True)
        say(CUSTOM_ERROR_MESSAGE, thread_ts=event.get("thread_ts", event["ts"]))

@app.command("/analytics")
def handle_analytics_command(ack, respond, command, logger):
    """Handle /analytics slash command for influencer analytics"""
    ack()
    try:
        user_question = command['text'].strip()
        user_id = command['user_id']
        channel_id = command['channel_id']
        thread_ts = command['trigger_id']
        session_id = f"{channel_id}_{thread_ts}"
        
        logger.info(f"Received analytics slash command from user {user_id} in channel {channel_id}: '{user_question}'")
        
        if not user_question:
            respond(text="Hello! 🎯 I'm your Influencer Analytics Assistant. Use `/analytics your question` to ask about:\n• Top performing influencers\n• Target vs actual performance\n• Monthly breakdowns\n• Budget analysis\n• Tier classifications")
        else:
            answer, trace_id = process_user_query(user_id, session_id, user_question)
            response_blocks = create_response_blocks(answer, trace_id)
            respond(blocks=response_blocks, text=answer)
            
    except Exception as e:
        logger.error(f"Error in analytics slash command: {e}", exc_info=True)
        respond(text=CUSTOM_ERROR_MESSAGE)

@app.command("/influencer")
def handle_slash_command(ack, respond, command, logger):
    """Handle /influencer slash command - redirect to analytics functionality"""
    ack()
    try:
        user_question = command['text'].strip()
        user_id = command['user_id']
        channel_id = command['channel_id']
        thread_ts = command['trigger_id']
        session_id = f"{channel_id}_{thread_ts}"
        
        logger.info(f"Received influencer slash command from user {user_id} in channel {channel_id}: '{user_question}'")
        
        if not user_question:
            respond(text="Hello! 🎯 I'm your Influencer Analytics Assistant. Use `/influencer your question` to ask about influencer performance, targets, budgets, and more!")
        else:
            answer, trace_id = process_user_query(user_id, session_id, user_question)
            response_blocks = create_response_blocks(answer, trace_id)
            respond(blocks=response_blocks, text=answer)
            
    except Exception as e:
        logger.error(f"Error in slash command: {e}", exc_info=True)
        respond(text=CUSTOM_ERROR_MESSAGE)

@app.action(re.compile("feedback_button_(up|down)"))
def handle_feedback_action(ack, action, body, client, logger):
    """Handles clicks on the thumbs up/down feedback buttons"""
    ack()
    try:
        feedback_value = action['value']
        vote, trace_id = feedback_value.split('_', 1)
        score_value = 1 if vote == "up" else 0
        user_id = body['user']['id']

        logger.info(f"Received feedback for trace {trace_id}: {'UP' if score_value == 1 else 'DOWN'} from user {user_id}")

        langfuse.create_score(
            trace_id=trace_id,
            name="user-feedback",
            value=score_value,
            comment=f"Slack feedback from user {user_id}"
        )
        
        original_blocks = body['message']['blocks']
        for i, block in enumerate(original_blocks):
            if block.get("block_id", "").startswith("feedback_"):
                original_blocks[i] = {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": "✅ Thanks for your feedback!"}]
                }
                break

        client.chat_update(
            channel=body['channel']['id'],
            ts=body['message']['ts'],
            blocks=original_blocks
        )
    except Exception as e:
        logger.error(f"Error handling feedback action: {e}", exc_info=True)

@app.event("app_home_opened")
def update_home_tab(client, event, logger):
    """Update the App Home tab when user opens it"""
    try:
        client.views_publish(
            user_id=event["user"],
            view={
                "type": "home",
                "blocks": [
                    {"type": "section", "text": {"type": "mrkdwn", "text": "*🎯 Welcome to Influencer Analytics Assistant!*"}},
                    {"type": "section", "text": {"type": "mrkdwn", "text": "I'm your AI expert for influencer performance analysis and market insights."}},
                    {"type": "divider"},
                    {"type": "section", "text": {"type": "mrkdwn", "text": "*How to use me:*\n• Mention me in any channel: `@YourBotName your question`\n• Send me a direct message\n• Use the slash command: `/influencer your question` or `/analytics your question`"}},
                    {"type": "section", "text": {"type": "mrkdwn", "text": "*What you can ask:*\n• Top performing influencers by spend, conversions, or CAC\n• Target vs actual performance analysis\n• Monthly spending breakdowns\n• Tier classifications (Gold, Silver, Bronze)\n• Budget planning and optimization\n• Market comparisons (UK, France, Nordics, etc.)"}},
                    {"type": "section", "text": {"type": "mrkdwn", "text": "*Example questions:*\n• \"Top 10 influencers by total spend\"\n• \"Target vs actual for France in 2024\"\n• \"Monthly breakdown for UK\"\n• \"Gold tier influencers analysis\"\n• \"Based on current spend vs target, recommend budget allocation\""}},
                    {"type": "divider"},
                    {"type": "section", "text": {"type": "mrkdwn", "text": "*New!* I support feedback. Use the 👍 / 👎 buttons on my responses to help me improve!"}},
                    {"type": "section", "text": {"type": "mrkdwn", "text": "_Ready to help you make data-driven influencer marketing decisions!_ 🚀"}}
                ]
            }
        )
    except Exception as e:
        logger.error(f"Error publishing home tab: {e}")

@app.event("message")
def handle_direct_message(event, say, logger):
    """Handle direct messages to the bot"""
    try:
        # Only respond to direct messages (not channel messages)
        if event.get('channel_type') != 'im':
            return
        
        # Ignore bot messages
        if event.get('subtype') == 'bot_message':
            return
            
        user_question = event['text'].strip()
        user_id = event['user']
        channel_id = event['channel']
        thread_ts = event.get("thread_ts", event["ts"])
        session_id = f"{channel_id}_{thread_ts}"
        
        logger.info(f"Received DM from user {user_id}: '{user_question}'")
        
        thinking_message = say(text="🎯 Analyzing your influencer data...")
        
        if not user_question:
            answer = "Hello! 🎯 I'm your Influencer Analytics Assistant. Ask me about influencer performance, targets vs actuals, top performers, or budget analysis!"
            app.client.chat_update(
                channel=thinking_message['channel'],
                ts=thinking_message['ts'],
                text=answer
            )
        else:
            answer, trace_id = process_user_query(user_id, session_id, user_question)
            response_blocks = create_response_blocks(answer, trace_id)
            
            app.client.chat_update(
                channel=thinking_message['channel'],
                ts=thinking_message['ts'],
                text=answer,
                blocks=response_blocks
            )
            
    except Exception as e:
        logger.error(f"Error in direct message: {e}", exc_info=True)
        say(CUSTOM_ERROR_MESSAGE)

# --- Quick Actions and Shortcuts ---

@app.shortcut("quick_analytics")
def handle_quick_analytics_shortcut(ack, shortcut, client, logger):
    """Handle quick analytics shortcut from Slack"""
    ack()
    try:
        client.views_open(
            trigger_id=shortcut["trigger_id"],
            view={
                "type": "modal",
                "callback_id": "analytics_modal",
                "title": {"type": "plain_text", "text": "Quick Analytics"},
                "submit": {"type": "plain_text", "text": "Get Insights"},
                "blocks": [
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": "Select a quick analytics query:"}
                    },
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "Top 10 by Spend"},
                                "value": "top_10_spend",
                                "action_id": "quick_query"
                            },
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "Target vs Actual UK"},
                                "value": "target_actual_uk",
                                "action_id": "quick_query"
                            },
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "Monthly Breakdown"},
                                "value": "monthly_breakdown",
                                "action_id": "quick_query"
                            }
                        ]
                    },
                    {
                        "type": "input",
                        "block_id": "custom_question",
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "question_input",
                            "placeholder": {"type": "plain_text", "text": "Or ask your own question..."}
                        },
                        "label": {"type": "plain_text", "text": "Custom Question"},
                        "optional": True
                    }
                ]
            }
        )
    except Exception as e:
        logger.error(f"Error opening quick analytics modal: {e}")

@app.action("quick_query")
def handle_quick_query_action(ack, action, body, client, logger):
    """Handle quick query button clicks"""
    ack()
    try:
        query_mapping = {
            "top_10_spend": "Top 10 influencers by total spend",
            "target_actual_uk": "Target vs actual performance for UK in 2024",
            "monthly_breakdown": "Monthly spending breakdown for all markets"
        }
        
        user_question = query_mapping.get(action['value'], "Analytics query")
        user_id = body['user']['id']
        channel_id = body['user']['id']  # DM channel
        session_id = f"modal_{user_id}_{datetime.now().timestamp()}"
        
        # Process the query
        answer, trace_id = process_user_query(user_id, session_id, user_question)
        
        # Send result as DM
        client.chat_postMessage(
            channel=user_id,
            text=f"*Question:* {user_question}\n\n{answer}",
            blocks=create_response_blocks(f"*Question:* {user_question}\n\n{answer}", trace_id)
        )
        
        # Close the modal
        client.views_update(
            view_id=body['view']['id'],
            view={
                "type": "modal",
                "title": {"type": "plain_text", "text": "Analytics Complete"},
                "blocks": [
                    {"type": "section", "text": {"type": "mrkdwn", "text": "✅ Your analytics result has been sent to you as a direct message!"}}
                ]
            }
        )
        
    except Exception as e:
        logger.error(f"Error handling quick query action: {e}")

# --- Error Handling ---

@app.error
def custom_error_handler(error, body, logger):
    """Custom error handler"""
    logger.exception(f"Error: {error}")
    return f"Sorry, something went wrong: {error}"

# --- Main App Execution ---

if __name__ == "__main__":
    logging.info("🎯 Starting Influencer Analytics Slack Bot...")
    
    # Test API connection on startup
    try:
        test_query = {"source": "dashboard", "filters": {"market": "All", "year": "2024"}}
        test_response = query_influencer_api(test_query)
        if "error" in test_response:
            logging.warning(f"⚠️ Influencer API test failed: {test_response['error']}")
        else:
            logging.info("✅ Influencer API connection test successful")
    except Exception as e:
        logging.error(f"Failed to test influencer API: {e}")
    
    try:
        # Start health check server in background thread
        health_thread = threading.Thread(target=start_health_check_server, daemon=True)
        health_thread.start()
        
        handler = SocketModeHandler(app, SLACK_APP_TOKEN)
        logging.info(f"🚀 Influencer Analytics Bot is running! Health check available at http://localhost:{HEALTH_CHECK_PORT}/health")
        logging.info("Available commands:")
        logging.info("  • @mention the bot with your question")
        logging.info("  • /influencer your question")
        logging.info("  • /analytics your question")
        logging.info("  • Direct message the bot")
        handler.start()
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot is shutting down.")
    except Exception as e:
        logging.error(f"Failed to start the bot: {e}")
    finally:
        logging.info("Flushing remaining Langfuse traces...")
        langfuse.flush()
        sys.exit(1)