"""
AgentFlow Dashboard — Phase 4
===========================
Streamlit dashboard with navigation and dark/light mode.
"""

import os
import json
import requests
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import psycopg2
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

API_BASE   = os.getenv("API_BASE_URL", "http://localhost:8001")
PAGE_TITLE = "AgentFlow — Growth Engine"

# ─── Page Config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title=PAGE_TITLE,
    page_icon="🛡",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─── Session State ──────────────────────────────────────────────────────────────
if "theme" not in st.session_state:
    st.session_state.theme = "light"

if "page" not in st.session_state:
    st.session_state.page = "dashboard"

def toggle_theme():
    st.session_state.theme = "dark" if st.session_state.theme == "light" else "light"

def set_page(page_name):
    st.session_state.page = page_name

# ─── Theme CSS ──────────────────────────────────────────────────────────────
def get_theme_css():
    if st.session_state.theme == "dark":
        return """
        html, body, [class*="css"] {
            background-color: #0d1117 !important;
            color: #e6edf3 !important;
        }
        section[data-testid="stSidebar"] {
            background-color: #161b22 !important;
            border-right: 1px solid #30363d !important;
        }
        .metric-card {
            background: #161b22;
            border: 1px solid #30363d;
        }
        .metric-label { color: #8b949e; }
        .metric-value { color: #e6edf3; }
        .section-card {
            background: #161b22;
            border: 1px solid #30363d;
        }
        .section-header {
            border-bottom: 1px solid #30363d;
            background: rgba(22, 27, 34, 0.5);
            color: #e6edf3;
        }
        """
    else:
        return """
        html, body, [class*="css"] {
            background-color: #f8f9ff !important;
            color: #0b1c30 !important;
        }
        section[data-testid="stSidebar"] {
            background-color: #ffffff !important;
            border-right: 1px solid #c7c4d7 !important;
        }
        .metric-card {
            background: #ffffff;
            border: 1px solid #c7c4d7;
        }
        .metric-label { color: #464554; }
        .metric-value { color: #0b1c30; }
        .section-card {
            background: #ffffff;
            border: 1px solid #c7c4d7;
        }
        .section-header {
            border-bottom: 1px solid #c7c4d7;
            background: rgba(229, 238, 255, 0.3);
            color: #0b1c30;
        }
        """

st.markdown(f"""
<style>
{get_theme_css()}
.metric-card {{
    border-radius: 12px;
    padding: 20px;
}}
.metric-label {{
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    margin-bottom: 8px;
}}
.metric-value {{
    font-size: 32px;
    font-weight: 700;
}}
.section-card {{
    border-radius: 12px;
    padding: 20px;
    margin-bottom: 20px;
}}
.section-header {{
    font-size: 18px;
    font-weight: 600;
    margin-bottom: 16px;
}}
</style>
""", unsafe_allow_html=True)

# ─── Sidebar ──────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("### 🚀 AgentFlow")
    st.markdown("---")
    
    # Theme Toggle
    theme_icon = "🌙" if st.session_state.theme == "light" else "☀️"
    if st.button(f"{theme_icon} Toggle Theme", key="theme_toggle"):
        toggle_theme()
        st.rerun()
    
    st.markdown("---")
    st.markdown("### Navigation")
    
    if st.button("📊 Dashboard", key="nav_dashboard", use_container_width=True):
        set_page("dashboard")
        st.rerun()
    
    if st.button("🔍 Leads", key="nav_leads", use_container_width=True):
        set_page("leads")
        st.rerun()
    
    if st.button("🛡 Security Logs", key="nav_security", use_container_width=True):
        set_page("security")
        st.rerun()
    
    if st.button("⚙️ Settings", key="nav_settings", use_container_width=True):
        set_page("settings")
        st.rerun()

# ─── Database Helpers ──────────────────────────────────────────────────────────
import psycopg2

def get_db_connection():
    # Cloud-ready: honours DATABASE_URL (Neon/Supabase) or POSTGRES_* vars.
    from db_config import get_conn
    return get_conn()

@st.cache_data(ttl=30)
def fetch_leads(limit=20):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT l.full_name, l.job_title, l.company, l.industry,
                   o.subject_line, o.status, o.security_score, o.rejection_reason,
                   o.security_log
            FROM leads l
            JOIN outreach o ON l.id = o.lead_id
            ORDER BY o.created_at DESC
            LIMIT %s
        """, (limit,))
        columns = ["full_name", "job_title", "company", "industry",
                   "subject_line", "status", "security_score",
                   "rejection_reason", "security_log"]
        leads = [dict(zip(columns, row)) for row in cur.fetchall()]
        cur.close()
        conn.close()
        return leads
    except Exception as e:
        st.error(f"Database error: {e}")
        return []

@st.cache_data(ttl=30)
def fetch_runs(limit=10):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT id, status, leads_processed, trigger_source, started_at, completed_at
            FROM pipeline_runs
            ORDER BY started_at DESC
            LIMIT %s
        """, (limit,))
        columns = ["id", "status", "leads_processed", "trigger_source", "started_at", "completed_at"]
        runs = [dict(zip(columns, row)) for row in cur.fetchall()]
        cur.close()
        conn.close()
        return runs
    except Exception as e:
        st.error(f"Database error: {e}")
        return []

# ─── Load Data ────────────────────────────────────────────────────────────────
leads = fetch_leads(limit=20)
runs = fetch_runs(limit=48)

total_leads = len(leads)
approved = [l for l in leads if l.get("status") == "approved"]
avg_score = round(sum([l.get("security_score", 0) for l in leads]) / len(leads)) if leads else 0

# ─── Page Routing ──────────────────────────────────────────────────────────────
if st.session_state.page == "dashboard":
    st.markdown("## 📊 Dashboard")
    
    # Metrics
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">Total Leads</div>
            <div class="metric-value">{total_leads}</div>
        </div>""", unsafe_allow_html=True)
    
    with c2:
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">Approved</div>
            <div class="metric-value">{len(approved)}</div>
        </div>""", unsafe_allow_html=True)
    
    with c3:
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">Avg Score</div>
            <div class="metric-value">{avg_score}%</div>
        </div>""", unsafe_allow_html=True)
    
    with c4:
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">Pipeline Runs</div>
            <div class="metric-value">{len(runs)}</div>
        </div>""", unsafe_allow_html=True)
    
    # Recent Leads
    st.markdown('<div class="section-card">', unsafe_allow_html=True)
    st.markdown('<div class="section-header">Recent Leads</div>', unsafe_allow_html=True)
    
    if leads:
        for lead in leads[:5]:
            st.markdown(f"**{lead.get('company', 'Unknown')}** - {lead.get('full_name', 'N/A')}")
            st.markdown(f"Score: {lead.get('security_score', 0)}/100 | Status: {lead.get('status', 'pending')}")
            st.markdown("---")
    else:
        st.info("No leads found. Run `python crew.py` first.")
    
    st.markdown('</div>', unsafe_allow_html=True)

elif st.session_state.page == "leads":
    st.markdown("## 🔍 All Leads")
    
    if leads:
        for lead in leads:
            with st.expander(f"{lead.get('company', 'Unknown')} - {lead.get('full_name', 'N/A')}"):
                st.markdown(f"**Title:** {lead.get('job_title', 'N/A')}")
                st.markdown(f"**Subject:** {lead.get('subject_line', 'N/A')}")
                st.markdown(f"**Security Score:** {lead.get('security_score', 0)}/100")
                st.markdown(f"**Status:** {lead.get('status', 'pending')}")
    else:
        st.info("No leads found. Run `python crew.py` first.")

elif st.session_state.page == "security":
    st.markdown("## 🛡 Security Logs")
    
    if leads:
        for lead in leads:
            log = lead.get("security_log", {})
            if isinstance(log, str):
                try:
                    log = json.loads(log)
                except:
                    log = {}

            breakdown = log.get("breakdown", {})
            with st.expander(f"{lead.get('company', 'Unknown')} - Score: {lead.get('security_score', 0)}/100"):
                st.markdown(f"**PII Check:** {breakdown.get('pii', {}).get('score', 0)}/40")
                st.markdown(f"**Injection Check:** {breakdown.get('injection', {}).get('score', 0)}/30")
                st.markdown(f"**Hallucination Check:** {breakdown.get('hallucination', {}).get('score', 0)}/20")
                st.markdown(f"**Tone Check:** {breakdown.get('tone', {}).get('score', 0)}/10")
    else:
        st.info("No security logs found.")

elif st.session_state.page == "settings":
    st.markdown("## ⚙️ Settings")
    
    st.markdown("### 🎨 Appearance")
    st.markdown("Use the theme toggle in the sidebar to switch between light and dark mode.")
    
    st.markdown("---")
    st.markdown("### 🎯 Pipeline Targeting")
    st.markdown("Edit your `.env` file to change:")
    st.code("""
TARGET_INDUSTRY=SaaS
TARGET_JOB_TITLE=Head of Growth
TARGET_LOCATION=United States
    """, language="bash")
    
    st.markdown("---")
    st.markdown("### 🚀 Run Pipeline")
    if st.button("Run Pipeline Now", type="primary"):
        st.info("Pipeline started! Check the Dashboard page for results.")

# ─── Refresh Button ─────────────────────────────────────────────────────────────
st.markdown("---")
if st.button("🔄 Refresh Data"):
    st.cache_data.clear()
    st.rerun()
