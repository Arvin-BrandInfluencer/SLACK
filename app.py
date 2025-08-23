import streamlit as st
import requests
import json
from google import genai
import os
from datetime import datetime
from dotenv import load_dotenv
import pandas as pd

# Load environment variables from .env file
load_dotenv()

# Configure Streamlit page
st.set_page_config(
    page_title="Influencer Analytics Chatbot",
    page_icon="📊",
    layout="wide"
)

# Initialize Gemini client
@st.cache_resource
def init_gemini_client():
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        st.error("❌ GOOGLE_API_KEY not found in .env file. Please add it to your .env file.")
        st.info("Create a .env file with: GOOGLE_API_KEY=your_api_key_here")
        st.stop()
    
    try:
        client = genai.Client(api_key=api_key)
        return client
    except Exception as e:
        st.error(f"❌ Error initializing Gemini client: {str(e)}")
        st.stop()

def query_influencer_api(payload):
    """Query the influencer analytics API"""
    api_url = os.getenv("INFLUENCER_API_URL", "http://127.0.0.1:5001/query")
    
    try:
        response = requests.post(
            api_url,
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

def analyze_question_complexity(user_question, client):
    """Analyze if question requires single or multiple API calls"""
    prompt = """
Analyze this user question to determine if it requires single or multiple API calls:

USER QUERY: "{question}"

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
   - Questions about optimization based on performance vs targets
   - Questions requiring data from dashboard + monthly + summary views

INDICATORS OF MULTI-STEP QUERIES:
- "budget planning", "remaining budget", "optimize", "recommend"
- "based on target", "depending on spend", "how to allocate"
- "compare performance and suggest", "analyze and recommend"
- Questions that need: targets AND spending AND influencer selection

RESPONSE FORMAT:
{{
  "complexity": "single" or "multi-step",
  "reasoning": "Brief explanation why",
  "requires_scratch_pad": true or false
}}

Return ONLY valid JSON.
""".format(question=user_question)

    try:
        response = client.models.generate_content(
            model="gemini-2.0-flash-exp",
            contents=prompt
        )
        
        json_str = response.text.strip()
        if json_str.startswith("```"):
            json_str = json_str.split("```")[1]
            if json_str.startswith("json"):
                json_str = json_str[4:]
        
        return json.loads(json_str)
        
    except Exception as e:
        st.error(f"Error analyzing question complexity: {str(e)}")
        return {"complexity": "single", "reasoning": "Error in analysis", "requires_scratch_pad": False}

def create_scratch_pad_analysis(user_question, client):
    """Create detailed scratch pad analysis for complex queries"""
    prompt = """
Create a detailed scratch pad analysis for this complex query:

USER QUERY: "{question}"

SCRATCH PAD ANALYSIS STRUCTURE:
1. QUESTION BREAKDOWN
   - What is the user really asking for?
   - What are the key entities (market, time period, metrics)?
   - What decision or analysis do they need?

2. DATA REQUIREMENTS ANALYSIS
   - What data sources are needed?
   - What specific information from each source?
   - How will the data be combined/analyzed?

3. STEP-BY-STEP EXECUTION PLAN
   Step 1: [First API call - what and why]
   Step 2: [Second API call - what and why]  
   Step 3: [Analysis/calculation needed]
   Step 4: [Final recommendations/insights]

4. EXPECTED API CALLS
   List each API call with:
   - Purpose
   - Source and view
   - Key filters
   - Expected data format

5. ANALYSIS STRATEGY
   - How to combine the data
   - What calculations are needed
   - What insights to derive
   - What recommendations to make

Make this analysis detailed but concise. Focus on the logical flow and data dependencies.
""".format(question=user_question)

    try:
        response = client.models.generate_content(
            model="gemini-2.0-flash-exp",
            contents=prompt
        )
        return response.text
        
    except Exception as e:
        return f"Error creating scratch pad analysis: {str(e)}"

def extract_entities_and_generate_query(user_question, client):
    """
    Extracts entities from user query and generates a single, compliant API query.
    Uses highly detailed and strict API documentation in the prompt.
    """
    
    prompt = """
You are a meticulous API integration assistant. Your ONLY job is to convert a user's question into a valid JSON payload for the Brand Influence Query API. You must follow the API documentation perfectly.

USER QUERY: "{question}"

--- API DOCUMENTATION ---

**Endpoint:** `http://127.0.0.1:5001/query` (POST request)

**1. Source: `dashboard`**
   - **Purpose:** High-level, monthly Target vs. Actual performance metrics.
   - **Payload:** `{{"source": "dashboard", "filters": {{"market": "UK", "year": "2025"}}}}`
   - **Filter `market`:** "UK", "France", "Sweden", "Norway", "Denmark", "Nordics", "All"
   - **Filter `year`:** "2025", "2024", "All"
   - **NOTE:** `dashboard` source does NOT support `view`, `sort`, or `limit` parameters.

**2. Source: `influencer_analytics`**
   - **Purpose:** Detailed influencer-centric analytics.
   - **`view` parameter is REQUIRED.**
   - **Common Filters:** `market`, `year` (same values as dashboard).

   **2.1. View: `summary`**
      - **Purpose:** Unique influencers with lifetime performance stats. Useful for finding top/worst performers.
      - **Payload:** `{{"source": "influencer_analytics", "view": "summary", "filters": {{...}}, "sort": {{...}}, "limit": <number>}}`
      - **Sortable fields (`sort.by`):** `campaign_count`, `total_conversions`, `total_views`, `total_clicks`, `total_spend_eur`, `effective_cac_eur`, `avg_ctr`, `avg_cvr`.
      - **Sort order (`sort.order`):** "asc", "desc".
      - **Limit:** Integer to limit number of records (only for summary view).

   **2.2. View: `discovery_tiers`**
      - **Purpose:** Ranks influencers into Gold, Silver, Bronze tiers by `effective_cac_eur`.
      - **Payload:** `{{"source": "influencer_analytics", "view": "discovery_tiers", "filters": {{...}}}}`
      - **NOTE:** This view does not support `sort` or `limit` parameters.

   **2.3. View: `monthly_breakdown`**
      - **Purpose:** Groups campaigns by month with summary and details.
      - **Payload:** `{{"source": "influencer_analytics", "view": "monthly_breakdown", "filters": {{...}}}}`
      - **NOTE:** This view does not support `sort` or `limit` parameters.

--- CRITICAL INSTRUCTIONS ---
1. **Strictly Adhere to the Schema.** Do not invent new keys or parameters.
2. **`influencer_analytics` ALWAYS requires a `view` parameter.**
3. **`dashboard` NEVER has a `view`, `sort`, or `limit` parameter.**
4. **If the year is not specified, default to "2024".**
5. **If the market is not specified, default to "All".**
6. **Use `limit` for "top N", "best N", or "worst N" queries in `summary` view.**
   - For "top" or "best" by cost metrics (e.g., `effective_cac_eur`), use `"order": "asc"`.
   - For performance metrics (e.g., `total_conversions`), use `"order": "desc"`.
7. **Routing Logic:**
   - "target vs actual", "budget performance" -> `dashboard`
   - "top influencers", "best performers", "who has the lowest cac", "show me N influencers" -> `influencer_analytics` + `summary` view
   - "monthly spending", "trends by month" -> `influencer_analytics` + `monthly_breakdown` view
   - "tiers", "gold/silver/bronze", "discovery" -> `influencer_analytics` + `discovery_tiers` view
8. **Final Output:** Return ONLY the raw, valid JSON object. No explanations, no markdown, no commentary. Just the JSON.

--- EXAMPLES ---
- User: "Target vs actual for France in 2025" -> `{{"source": "dashboard", "filters": {{"market": "France", "year": "2025"}}}}`
- User: "Top 5 influencers by spend" -> `{{"source": "influencer_analytics", "view": "summary", "filters": {{"market": "All", "year": "2024"}}, "sort": {{"by": "total_spend_eur", "order": "desc"}}, "limit": 5}}`
- User: "Show me 10 influencers with lowest CAC" -> `{{"source": "influencer_analytics", "view": "summary", "filters": {{"market": "All", "year": "2024"}}, "sort": {{"by": "effective_cac_eur", "order": "asc"}}, "limit": 10}}`

Now, generate the JSON for the user query provided above.
""".format(question=user_question)

    try:
        response = client.models.generate_content(
            model="gemini-2.0-flash-exp",
            contents=prompt
        )
        
        json_str = response.text.strip()
        if json_str.startswith("```"):
            json_str = json_str.split("```")[1]
            if json_str.startswith("json"):
                json_str = json_str[4:]
        
        return json.loads(json_str)
        
    except Exception as e:
        st.error(f"Error generating query: {str(e)}")
        return None

def generate_multi_step_queries(user_question, client):
    """
    Generate multiple API queries for complex questions, now with stricter documentation.
    """
    
    prompt = """
You are a strategic planner that breaks down complex user questions into a sequence of precise API calls. You must follow the API documentation perfectly.

USER QUERY: "{question}"

--- API DOCUMENTATION ---

**Endpoint:** `http://127.0.0.1:5001/query` (POST request)

**1. Source: `dashboard`**
   - **Purpose:** High-level, monthly Target vs. Actual performance metrics.
   - **Payload:** `{{"source": "dashboard", "filters": {{"market": "UK", "year": "2025"}}}}`
   - **Filter `market`:** "UK", "France", "Sweden", "Norway", "Denmark", "Nordics", "All"
   - **Filter `year`:** "2025", "2024", "All"
   - **NOTE:** `dashboard` source does NOT support `view`, `sort`, or `limit` parameters.

**2. Source: `influencer_analytics`**
   - **Purpose:** Detailed influencer-centric analytics.
   - **`view` parameter is REQUIRED.**
   - **Common Filters:** `market`, `year` (same values as dashboard).

   **2.1. View: `summary`**
      - **Purpose:** Unique influencers with lifetime performance stats. Useful for finding top/worst performers.
      - **Payload:** `{{"source": "influencer_analytics", "view": "summary", "filters": {{...}}, "sort": {{...}}, "limit": <number>}}`
      - **Sortable fields:** `campaign_count`, `total_conversions`, `total_views`, `total_clicks`, `total_spend_eur`, `effective_cac_eur`, `avg_ctr`, `avg_cvr`.
      - **Sort order:** "asc", "desc".
      - **Limit:** Integer to limit number of records.

   **2.2. View: `discovery_tiers`**
      - **Purpose:** Ranks influencers into Gold, Silver, Bronze tiers. Useful for finding new talent.
      - **Payload:** `{{"source": "influencer_analytics", "view": "discovery_tiers", "filters": {{...}}}}`
      - **NOTE:** Does not support `sort` or `limit`.

   **2.3. View: `monthly_breakdown`**
      - **Purpose:** Groups campaigns by month. Useful for temporal analysis.
      - **Payload:** `{{"source": "influencer_analytics", "view": "monthly_breakdown", "filters": {{...}}}}`
      - **NOTE:** Does not support `sort` or `limit`.

--- CRITICAL INSTRUCTIONS ---
1. **Break down the user query** into a logical sequence of API calls.
2. **Generate a JSON object** containing a `queries` list and a `final_analysis_needed` string.
3. **Each query in the list must be perfectly formed** according to the documentation above.
4. **If the year is not specified, default to "2024".**
5. **If the market is not specified, default to "All".**
6. **Use `limit` for "top N", "best N", or "worst N" queries in `summary` view.**
   - For cost metrics (e.g., `effective_cac_eur`), use `"order": "asc"`.
   - For performance metrics (e.g., `total_conversions`), use `"order": "desc"`.
7. **`influencer_analytics` ALWAYS requires a `view` parameter.**
8. **`dashboard` NEVER has a `view`, `sort`, or `limit` parameter.**
9. **Final Output:** Return ONLY the valid JSON object. No explanations or commentary.

--- EXAMPLE RESPONSE FORMAT ---
{{
  "queries": [
    {{
      "step": 1,
      "purpose": "Get monthly target vs actual data for the UK to understand budget status.",
      "query": {{"source": "dashboard", "filters": {{"market": "UK", "year": "2024"}}}}
    }},
    {{
      "step": 2,
      "purpose": "Get a list of cost-effective influencers in the UK for potential new campaigns.",
      "query": {{"source": "influencer_analytics", "view": "summary", "filters": {{"market": "UK", "year": "2024"}}, "sort": {{"by": "effective_cac_eur", "order": "asc"}}, "limit": 10}}
    }}
  ],
  "final_analysis_needed": "Calculate the remaining budget based on the latest month's data from step 1. Then, recommend how many new influencers from the top of the list in step 2 can be activated with that remaining budget."
}}

Now, generate the JSON for the user query provided above.
""".format(question=user_question)

    try:
        response = client.models.generate_content(
            model="gemini-2.0-flash-exp",
            contents=prompt
        )
        
        json_str = response.text.strip()
        if json_str.startswith("```"):
            json_str = json_str.split("```")[1]
            if json_str.startswith("json"):
                json_str = json_str[4:]
        
        return json.loads(json_str)
        
    except Exception as e:
        st.error(f"Error generating multi-step queries: {str(e)}")
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
        
        st.write(f"**Step {step_num}:** {purpose}")
        st.code(json.dumps(query, indent=2), language="json")
        
        with st.spinner(f"Executing Step {step_num}..."):
            response = query_influencer_api(query)
            
            if "error" in response:
                st.error(f"❌ Error in Step {step_num}: {response['error']}")
                results[f"step_{step_num}"] = {
                    "purpose": purpose,
                    "query": query,
                    "error": response['error']
                }
                continue 
            else:
                st.success(f"✅ Step {step_num} completed")
                results[f"step_{step_num}"] = {
                    "purpose": purpose,
                    "query": query,
                    "data": response
                }
    
    return results

def compose_multi_step_answer(user_query, all_results, final_analysis_needed, client):
    """
    Compose comprehensive answer, handling failed steps.
    """
    
    data_summary = []
    for step_key, step_data in all_results.items():
        if "error" in step_data:
            summary_item = f"**Data from '{step_data['purpose']}' FAILED to load.**\nError: {step_data['error']}\nQuery attempted: {json.dumps(step_data['query'])}"
            data_summary.append(summary_item)
        else:
            summary_item = f"**Data from '{step_data['purpose']}':**\n{json.dumps(step_data.get('data', 'No data returned'), indent=2)}"
            data_summary.append(summary_item)

    combined_data = "\n\n---\n\n".join(data_summary)
    
    prompt = """
You are an expert influencer marketing strategist analyzing complex multi-step data.

ORIGINAL USER QUERY: "{query}"

MULTI-STEP DATA COLLECTED:
{data}

ANALYSIS REQUIRED: {analysis}

COMPOSE A COMPREHENSIVE STRATEGIC ANSWER:

STRUCTURE YOUR RESPONSE:
1. **Executive Summary** - Direct answer to the user's question.
2. **Data Analysis** - What the available data reveals. **If some data failed to load, acknowledge it and explain how it limits the analysis, then proceed with the data you do have.**
3. **Strategic Recommendations** - Specific actionable steps based on the successful data.
4. **Next Steps** - Concrete actions to take.

REQUIREMENTS:
- Synthesize data from all *successful* sources intelligently.
- **Explicitly state if a conclusion cannot be drawn due to missing data from a failed step.**
- Provide specific, actionable recommendations based on the information you have.
- Do not hallucinate data for the failed steps. Work with what is given.
- Focus on business value and ROI.

Provide the best possible strategic analysis given the available (and potentially incomplete) data.
""".format(query=user_query, data=combined_data, analysis=final_analysis_needed)

    try:
        response = client.models.generate_content(
            model="gemini-2.0-flash-exp",
            contents=prompt
        )
        return response.text
        
    except Exception as e:
        return f"Error composing multi-step answer: {str(e)}"

def generate_curl_command(api_payload):
    """Generate CURL command from API payload following the API documentation strictly"""
    api_url = os.getenv("INFLUENCER_API_URL", "http://127.0.0.1:5001/query")
    
    # Ensure JSON payload is properly escaped and enclosed in single quotes
    json_payload = json.dumps(api_payload, ensure_ascii=False).replace('"', '\\"')
    curl_command = f"""curl -X POST {api_url} \\
  -H "Content-Type: application/json" \\
  -d '{json_payload}'"""
    
    return curl_command

def compose_answer_with_llm(user_query, api_data, client):
    """Compose natural language answer using LLM for single queries"""
    prompt = """
You are an expert influencer marketing analyst. 

USER QUERY: "{query}"

API RESPONSE DATA: {data}

COMPOSE A COMPREHENSIVE ANSWER:

INSTRUCTIONS:
1. Directly answer the user's specific question
2. Present data in a clear, business-focused manner
3. Use appropriate formatting (tables, bullets, structured layout)
4. Include specific numbers and key metrics
5. Highlight important findings and business implications
6. For monthly data, show trends and patterns
7. For influencer summaries, focus on top performers
8. For dashboard data, compare targets vs actuals
9. For tier data, explain tier distribution
10. End with actionable insights

FORMATTING:
- Use **bold** for important metrics and headers
- Use bullet points for key findings
- Include € symbols and proper number formatting
- Create tables for comparative data when appropriate
- Use emojis sparingly for visual appeal

DATA TYPE HANDLING:
- Dashboard data: Focus on target vs actual performance
- Monthly breakdown: Show monthly trends and top campaigns
- Summary data: Rank influencers and highlight best performers  
- Discovery tiers: Explain tier distribution and characteristics

BUSINESS CONTEXT:
- CAC = Customer Acquisition Cost (lower is better)
- CTR = Click Through Rate (higher is better)
- CVR = Conversion Rate (higher is better)
- Effective CAC = Total spend / Total conversions

Present insights naturally without mentioning "based on the data provided".
""".format(query=user_query, data=json.dumps(api_data, indent=2))

    try:
        response = client.models.generate_content(
            model="gemini-2.0-flash-exp",
            contents=prompt
        )
        return response.text
        
    except Exception as e:
        return f"Error composing answer: {str(e)}"

def main():
    st.title("🎯 Influencer Analytics Chatbot")
    st.markdown("Ask questions about your influencer marketing performance in natural language!")
    
    # Show environment status
    col1, col2 = st.columns([3, 1])
    with col1:
        st.markdown("*Powered by Google Gemini AI with Multi-Step Analysis & Scratch Pad*")
    with col2:
        if os.getenv("GOOGLE_API_KEY"):
            st.success("🔑 API Key Loaded")
        else:
            st.error("🔑 No API Key")
    
    # Initialize Gemini client
    client = init_gemini_client()
    
    # Sidebar with example questions and manual query builder
    with st.sidebar:
        st.header("💡 Example Questions")
        
        # Simple queries
        st.subheader("📊 Simple Queries")
        simple_questions = [
            "Monthly spending breakdown for UK",
            "Top 10 influencers by total spend",
            "Target vs actual performance for France",
            "Gold tier influencers analysis"
        ]
        
        for i, question in enumerate(simple_questions):
            if st.button(question, key=f"simple_{i}"):
                st.session_state.user_input = question
        
        # Complex multi-step queries
        st.subheader("🔄 Complex Multi-Step Queries")
        complex_questions = [
            "Based on November spend vs target, help me plan how to add more influencers for remaining budget in UK",
            "Analyze our France performance vs targets and recommend budget reallocation strategy",
            "Compare UK vs Nordics efficiency and suggest optimization plan",
            "Based on current spending trends, what's our projected year-end performance vs targets?"
        ]
        
        for i, question in enumerate(complex_questions):
            if st.button(question, key=f"complex_{i}"):
                st.session_state.user_input = question
        
        st.divider()
        
        # Manual Query Builder
        st.header("🔧 Manual Query Builder")
        
        with st.expander("Build Custom Query"):
            # Source selection
            source = st.selectbox("Source", ["dashboard", "influencer_analytics"])
            
            # Common filters
            st.subheader("Filters")
            market = st.selectbox("Market", ["All", "UK", "France", "Sweden", "Norway", "Denmark", "Nordics"])
            year = st.selectbox("Year", ["All", "2024", "2025"])
            
            # Build base query
            manual_query = {"source": source}
            
            # Add filters
            filters = {}
            if market != "All":
                filters["market"] = market
            if year != "All":
                filters["year"] = year
            
            if filters:
                manual_query["filters"] = filters
            
            # Source-specific options
            if source == "influencer_analytics":
                st.subheader("View (Required for influencer_analytics)")
                view = st.selectbox("View", ["summary", "monthly_breakdown", "discovery_tiers"])
                manual_query["view"] = view
                
                # Sort options for summary view
                if view == "summary":
                    st.subheader("Sort Options")
                    sort_by = st.selectbox("Sort By", [
                        "None", "campaign_count", "total_conversions", "total_views", 
                        "total_clicks", "total_spend_eur", "effective_cac_eur", "avg_ctr", "avg_cvr"
                    ])
                    limit = st.number_input("Limit (optional)", min_value=1, max_value=100, value=10)
                    
                    if sort_by != "None":
                        sort_order = st.selectbox("Sort Order", ["asc", "desc"])
                        manual_query["sort"] = {"by": sort_by, "order": sort_order}
                        manual_query["limit"] = limit
            
            if st.button("Generate Manual Query"):
                curl_cmd = generate_curl_command(manual_query)
                st.code(curl_cmd, language="bash")
                st.json(manual_query)
                
                # Execute the query
                if st.button("Execute Query"):
                    with st.spinner("Executing query..."):
                        result = query_influencer_api(manual_query)
                        st.json(result)
    
    # Initialize chat history
    if "messages" not in st.session_state:
        st.session_state.messages = []
    
    # Display chat history
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            if message["role"] == "assistant":
                st.markdown(message["content"])
                
                # Show expandable sections if available
                if "scratch_pad" in message:
                    with st.expander("🗒️ Scratch Pad Analysis"):
                        st.markdown(message["scratch_pad"])
                
                if "multi_step_details" in message:
                    with st.expander("🔄 Multi-Step Execution Details"):
                        for step_key, step_data in message["multi_step_details"].items():
                            st.write(f"**{step_data['purpose']}**")
                            st.code(json.dumps(step_data['query'], indent=2), language="json")
                            with st.expander(f"Raw Data - {step_key}"):
                                st.json(step_data['data'] if 'data' in step_data else {"error": step_data['error']})
                
                if "curl_command" in message:
                    with st.expander("🔧 CURL Command"):
                        st.code(message["curl_command"], language="bash")
                
                if "raw_data" in message:
                    with st.expander("📊 Raw API Response"):
                        st.json(message["raw_data"])
            else:
                st.markdown(message["content"])
    
    # Chat input
    if prompt := st.chat_input("Ask about your influencer analytics..."):
        # Add user message to chat history
        st.session_state.messages.append({"role": "user", "content": prompt})
        
        # Display user message
        with st.chat_message("user"):
            st.markdown(prompt)
        
        # Generate and display assistant response
        with st.chat_message("assistant"):
            # Analyze question complexity
            with st.spinner("🤔 Analyzing question complexity..."):
                complexity_analysis = analyze_question_complexity(prompt, client)
            
            st.write(f"**Query Type:** {complexity_analysis['complexity'].upper()}")
            st.write(f"**Reasoning:** {complexity_analysis['reasoning']}")
            
            if complexity_analysis["complexity"] == "multi-step":
                # Multi-step query handling
                st.markdown("---")
                st.markdown("### 🗒️ Scratch Pad Analysis")
                
                with st.spinner("Creating scratch pad analysis..."):
                    scratch_pad_analysis = create_scratch_pad_analysis(prompt, client)
                
                st.markdown(scratch_pad_analysis)
                
                st.markdown("---")
                st.markdown("### 🔄 Multi-Step Query Execution")
                
                with st.spinner("Planning multi-step queries..."):
                    query_plan = generate_multi_step_queries(prompt, client)
                
                if query_plan:
                    st.write(f"**Execution Plan:** {len(query_plan['queries'])} steps required")
                    st.write(f"**Final Analysis:** {query_plan['final_analysis_needed']}")
                    
                    # Execute each step
                    all_results = execute_multi_step_queries(query_plan)
                    
                    if all_results:
                        st.markdown("---")
                        st.markdown("### 📊 Comprehensive Analysis")
                        
                        with st.spinner("Composing comprehensive strategic analysis..."):
                            final_answer = compose_multi_step_answer(
                                prompt, 
                                all_results, 
                                query_plan['final_analysis_needed'], 
                                client
                            )
                        
                        st.markdown(final_answer)
                        
                        # Add to chat history
                        st.session_state.messages.append({
                            "role": "assistant",
                            "content": final_answer,
                            "scratch_pad": scratch_pad_analysis,
                            "multi_step_details": all_results,
                            "query_plan": query_plan
                        })
                else:
                    error_msg = "❌ Could not generate multi-step query plan."
                    st.markdown(error_msg)
                    st.session_state.messages.append({"role": "assistant", "content": error_msg})
            
            else:
                # Single query handling
                with st.spinner("🧠 Extracting entities and generating query..."):
                    api_query = extract_entities_and_generate_query(prompt, client)
                
                if api_query:
                    curl_command = generate_curl_command(api_query)
                    
                    st.markdown("🔍 **Generated API Query:**")
                    st.code(json.dumps(api_query, indent=2), language="json")
                    
                    with st.spinner("📡 Fetching data from API..."):
                        api_response = query_influencer_api(api_query)
                    
                    if "error" in api_response:
                        error_msg = f"❌ **API Error:** {api_response['error']}"
                        st.markdown(error_msg)
                        st.session_state.messages.append({"role": "assistant", "content": error_msg})
                    else:
                        with st.spinner("✍️ Composing intelligent response..."):
                            composed_answer = compose_answer_with_llm(prompt, api_response, client)
                        
                        st.markdown(composed_answer)
                        
                        with st.expander("🔧 CURL Command"):
                            st.code(curl_command, language="bash")
                        
                        with st.expander("📊 Raw API Response"):
                            st.json(api_response)
                        
                        st.session_state.messages.append({
                            "role": "assistant",
                            "content": composed_answer,
                            "curl_command": curl_command,
                            "raw_data": api_response
                        })
                else:
                    error_msg = "❌ Sorry, I couldn't understand your question. Please try rephrasing it or use the manual query builder in the sidebar."
                    st.markdown(error_msg)
                    st.session_state.messages.append({"role": "assistant", "content": error_msg})
    
    # Handle example question clicks
    if hasattr(st.session_state, 'user_input'):
        prompt = st.session_state.user_input
        delattr(st.session_state, 'user_input')
        
        st.session_state.messages.append({"role": "user", "content": prompt})
        st.rerun()

    # Sidebar utilities
    with st.sidebar:
        st.divider()
        
        if st.button("🗑️ Clear Chat History"):
            st.session_state.messages = []
            st.rerun()
        
        st.header("🔌 API Status")
        if st.button("Check API Connection"):
            st.write("Testing API with correct format...")
            
            # Test dashboard source
            dashboard_test = {"source": "dashboard", "filters": {"market": "UK", "year": "2024"}}
            
            # Test analytics source
            analytics_test = {"source": "influencer_analytics", "view": "summary", "filters": {"market": "UK", "year": "2024"}}
            
            st.subheader("Dashboard Source Test:")
            st.code(json.dumps(dashboard_test, indent=2), language="json")
            dashboard_response = query_influencer_api(dashboard_test)
            if "error" in dashboard_response:
                st.error(f"❌ Dashboard API Error: {dashboard_response['error']}")
            else:
                st.success("✅ Dashboard API Connected!")
                with st.expander("Sample Response"):
                    st.json(dashboard_response)
            
            st.subheader("Analytics Source Test:")
            st.code(json.dumps(analytics_test, indent=2), language="json")
            analytics_response = query_influencer_api(analytics_test)
            if "error" in analytics_response:
                st.error(f"❌ Analytics API Error: {analytics_response['error']}")
            else:
                st.success("✅ Analytics API Connected!")
                with st.expander("Sample Response"):
                    st.json(analytics_response)

if __name__ == "__main__":
    main()