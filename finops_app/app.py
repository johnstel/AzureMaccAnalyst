"""Azure MACC Analyst — FinOps Executive Dashboard."""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

APP_VERSION = "1.1.3"

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import pandas as pd
import streamlit as st

from src.logging_config import get_logger

logger = get_logger("app")
logger.info("========== Application starting ==========")

from src.analysis import (
    build_recommendation_summary,
    build_savings_analysis,
    commitment_rows,
    recommendation_rows,
    summarize_export,
    summary_to_dict,
)
from src.azure_auth import (
    create_interactive_only_credential,
    create_user_context_credential,
    load_auth_config,
)
from src.azure_clients import (
    is_authorization_exception,
    is_throttling_exception,
    list_advisor_cost_recommendations,
    list_advisor_cost_recommendations_parallel,
    list_reservations,
    list_savings_plans,
    list_subscriptions,
    query_cost_by_service,
    query_cost_by_service_parallel,
)
from src.csv_ri_detection import detect_ri_opportunities
from src.demo_data import generate_demo_data
from src.excel_export import build_excel_workbook
from src.file_loader import convert_excel_to_csv
from src.retail_pricing import batch_lookup_prices


# ── Helper: decode identity from JWT access token ─────────────────────────────
def _extract_identity(token: str) -> dict[str, str]:
    """Decode the JWT payload (without verification) to extract user info."""
    import base64
    import json as _json

    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        payload = _json.loads(base64.urlsafe_b64decode(payload_b64))
        return {
            "name": payload.get("name", ""),
            "email": (
                payload.get("upn", "")
                or payload.get("preferred_username", "")
                or payload.get("email", "")
                or payload.get("unique_name", "")
            ),
            "oid": payload.get("oid", ""),
            "tid": payload.get("tid", ""),
        }
    except Exception:
        return {"name": "", "email": "Unknown"}


def _azure_warning(label: str, ex: Exception, subscription_id: str | None = None) -> str:
    if is_throttling_exception(ex):
        if subscription_id:
            return f"{label} ({subscription_id[:8]}…): Azure is throttling requests. Retried automatically; partial results may be shown."
        return f"{label}: Azure is throttling requests. Retried automatically; partial results may be shown."
    if is_authorization_exception(ex):
        if subscription_id:
            return f"{label} ({subscription_id[:8]}…): Access denied (RBAC/permissions). Continuing with partial results."
        return f"{label}: Access denied (RBAC/permissions). Continuing with partial results."
    if subscription_id:
        return f"{label} ({subscription_id[:8]}…): {ex}"
    return f"{label}: {ex}"


# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Azure MACC Analyst",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Custom CSS ─────────────────────────────────────────────────────────────────
st.markdown(
    """
    <style>
    /* Sidebar branding */
    section[data-testid="stSidebar"] {background-color: #1B3A5C;}
    section[data-testid="stSidebar"] * {color: #FFFFFF !important;}
    section[data-testid="stSidebar"] .stButton > button {
        background-color: #2E75B6; color: white; border: none; border-radius: 6px;
        font-weight: 600; width: 100%;
    }
    section[data-testid="stSidebar"] .stButton > button:hover {background-color: #3A8FD4;}

    /* KPI cards */
    div[data-testid="stMetric"] {
        background-color: #F2F2F2; border-left: 4px solid #2E75B6;
        padding: 12px 16px; border-radius: 6px;
    }
    div[data-testid="stMetric"] label {font-size: 0.8rem; color: #555 !important;}
    div[data-testid="stMetric"] div[data-testid="stMetricValue"] {
        font-size: 1.3rem; font-weight: 700; color: #1B3A5C !important;
    }

    /* Section dividers */
    .section-divider {border: none; border-top: 2px solid #D6E4F0; margin: 1.5rem 0;}

    /* Status pill */
    .status-pill {
        display: inline-block; padding: 3px 12px; border-radius: 12px;
        font-size: 0.75rem; font-weight: 600;
    }
    .pill-green {background: #27AE60; color: white;}
    .pill-red   {background: #E74C3C; color: white;}
    </style>
    """,
    unsafe_allow_html=True,
)


# ── Helper: native file dialog ────────────────────────────────────────────────
def _pick_file() -> str | None:
    """Open a Windows file dialog. Returns path string only — no file read.

    Uses the Win32 ``GetOpenFileNameW`` API via ctypes so the dialog works
    reliably inside a PyInstaller-frozen executable (tkinter's Tcl/Tk runtime
    often fails to load in frozen bundles).  Falls back to tkinter when not
    running on Windows.
    """
    if sys.platform == "win32":
        result = _pick_file_win32()
        if result is not None:
            return result
    # Fallback: tkinter (dev / non-Windows)
    return _pick_file_tk()


def _pick_file_win32() -> str | None:
    """Native Windows file dialog via ctypes — no tkinter dependency."""
    try:
        import ctypes
        from ctypes import wintypes

        OFN_FILEMUSTEXIST = 0x00001000
        OFN_PATHMUSTEXIST = 0x00000800
        OFN_NOCHANGEDIR   = 0x00000008

        class OPENFILENAMEW(ctypes.Structure):
            _fields_ = [
                ("lStructSize",      wintypes.DWORD),
                ("hwndOwner",        wintypes.HWND),
                ("hInstance",        wintypes.HINSTANCE),
                ("lpstrFilter",      wintypes.LPCWSTR),
                ("lpstrCustomFilter", wintypes.LPWSTR),
                ("nMaxCustFilter",   wintypes.DWORD),
                ("nFilterIndex",     wintypes.DWORD),
                ("lpstrFile",        wintypes.LPWSTR),
                ("nMaxFile",         wintypes.DWORD),
                ("lpstrFileTitle",   wintypes.LPWSTR),
                ("nMaxFileTitle",    wintypes.DWORD),
                ("lpstrInitialDir",  wintypes.LPCWSTR),
                ("lpstrTitle",       wintypes.LPCWSTR),
                ("Flags",            wintypes.DWORD),
                ("nFileOffset",      wintypes.WORD),
                ("nFileExtension",   wintypes.WORD),
                ("lpstrDefExt",      wintypes.LPCWSTR),
                ("lCustData",        wintypes.LPARAM),
                ("lpfnHook",         ctypes.c_void_p),
                ("lpTemplateName",   wintypes.LPCWSTR),
                ("pvReserved",       ctypes.c_void_p),
                ("dwReserved",       wintypes.DWORD),
                ("FlagsEx",          wintypes.DWORD),
            ]

        # Filter pairs separated by \0, terminated with \0\0
        filter_str = (
            "Supported files\0*.csv;*.xlsx;*.xls;*.xlsm\0"
            "CSV files\0*.csv\0"
            "Excel files\0*.xlsx;*.xls;*.xlsm\0"
            "All files\0*.*\0\0"
        )

        buf = ctypes.create_unicode_buffer(4096)

        ofn = OPENFILENAMEW()
        ofn.lStructSize    = ctypes.sizeof(OPENFILENAMEW)
        ofn.hwndOwner      = None
        ofn.lpstrFilter    = filter_str
        ofn.lpstrFile      = ctypes.cast(buf, wintypes.LPWSTR)
        ofn.nMaxFile       = 4096
        ofn.lpstrTitle     = "Select Azure Invoice Export"
        ofn.Flags          = OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST | OFN_NOCHANGEDIR

        if ctypes.windll.comdlg32.GetOpenFileNameW(ctypes.byref(ofn)):
            return buf.value or None
        return None
    except Exception:
        return None


def _pick_file_tk() -> str | None:
    """Tkinter file dialog — works in dev mode and non-Windows platforms."""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askopenfilename(
            title="Select Azure Invoice Export",
            filetypes=[
                ("Supported files", "*.csv;*.xlsx;*.xls;*.xlsm"),
                ("CSV files", "*.csv"),
                ("Excel files", "*.xlsx;*.xls;*.xlsm"),
                ("All files", "*.*"),
            ],
        )
        root.destroy()
        return path if path else None
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════════
#  SIDEBAR — Authentication
# ═══════════════════════════════════════════════════════════════════════════════
with st.sidebar:
    # Azure logo — inline SVG (no external dependency)
    st.markdown(
        '<svg xmlns="http://www.w3.org/2000/svg" width="44" height="44" viewBox="0 0 96 96">'
        '<defs><linearGradient id="az" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0" stop-color="#114A8B"/><stop offset="1" stop-color="#0669BC"/>'
        '</linearGradient></defs>'
        '<path fill="url(#az)" d="M33.3 6.5h26.1L32.9 85.3a3.5 3.5 0 0 1-3.3 2.4H9.1a3.5 3.5 0 0 1-3.3-4.6L29.9 8.9a3.5 3.5 0 0 1 3.4-2.4z"/>'
        '<path fill="#0078D4" d="M71.2 60.8H34.9a1.6 1.6 0 0 0-1.1 2.7l26.2 24.4a3.6 3.6 0 0 0 2.4.9h22.5z"/>'
        '<path fill="#1490DF" d="M33.3 6.5A3.4 3.4 0 0 0 30 8.8L6 83.1a3.5 3.5 0 0 0 3.3 4.6h21a3.8 3.8 0 0 0 2.9-2.6l5.1-14.9 18 16.8a3.6 3.6 0 0 0 2.2.7h22.3L66.4 52.8l-21.2.1L59.4 6.5z"/>'
        '<path opacity=".3" fill="#000" d="M59.4 6.5H33.2a3.5 3.5 0 0 0-3.3 2.4l-24 74a3.5 3.5 0 0 0 .7 3.2 3.5 3.5 0 0 1-.7-3.2L30 8.9a3.5 3.5 0 0 1 3.3-2.4z"/>'
        '</svg>',
        unsafe_allow_html=True,
    )
    st.markdown("### Azure MACC Analyst")
    st.caption(f"FinOps Cost & Commitment Analysis  ·  v{APP_VERSION}")
    st.markdown("<hr style='border-color:#2E75B6; margin:8px 0;'>", unsafe_allow_html=True)
    st.markdown(
        '<div style="font-size:0.68rem; color:#A0A0A0; line-height:1.3; margin-top:4px;">'
        '⚠️ <b>Disclaimer:</b> This tool is for estimation and '
        'demonstrative purposes only. Savings projections are '
        'approximations based on publicly available Azure retail '
        'pricing and standard discount assumptions. No guarantees '
        'are made regarding accuracy, completeness, or actual '
        'savings. Always validate with your Microsoft account team '
        'before making commitment decisions.</div>',
        unsafe_allow_html=True,
    )

    signed_in = "credential" in st.session_state
    if signed_in:
        st.markdown('<span class="status-pill pill-green">● Connected</span>', unsafe_allow_html=True)
        # Show signed-in identity
        identity = st.session_state.get("user_identity")
        if identity:
            st.markdown(
                f'<div style="margin:6px 0 4px; font-size:0.82rem; color:#D6E4F0;">'
                f'<b>{identity.get("name", "")}</b><br>'
                f'<span style="font-size:0.75rem; opacity:0.8;">{identity.get("email", "")}</span></div>',
                unsafe_allow_html=True,
            )
    else:
        st.markdown('<span class="status-pill pill-red">● Not connected</span>', unsafe_allow_html=True)

    st.markdown("")

    # Auto sign-in (cached / CLI credentials)
    if st.button("Sign in (auto-detect)", use_container_width=True, disabled=signed_in):
        with st.spinner("Authenticating…"):
            try:
                logger.info("User initiated auto-detect sign-in")
                cfg = load_auth_config()
                cred = create_user_context_credential(cfg)
                token = cred.get_token("https://management.azure.com/.default")
                st.session_state["credential"] = cred
                st.session_state["user_identity"] = _extract_identity(token.token)
                logger.info("Auto-detect sign-in succeeded for %s", st.session_state["user_identity"].get("email", "unknown"))
                st.rerun()
            except Exception as ex:
                logger.exception("Auto-detect sign-in failed")
                st.error(f"Auto sign-in failed: {ex}")

    # Force browser prompt (different user)
    if st.button("Sign in as different user…", use_container_width=True):
        with st.spinner("Opening browser login…"):
            try:
                logger.info("User initiated interactive browser sign-in")
                cfg = load_auth_config()
                cred = create_interactive_only_credential(cfg)
                token = cred.get_token("https://management.azure.com/.default")
                st.session_state["credential"] = cred
                st.session_state["user_identity"] = _extract_identity(token.token)
                logger.info("Interactive sign-in succeeded for %s", st.session_state["user_identity"].get("email", "unknown"))
                st.rerun()
            except Exception as ex:
                logger.exception("Interactive sign-in failed")
                st.error(f"Sign-in failed: {ex}")

    # Sign out
    if signed_in and st.button("Sign out", use_container_width=True):
        logger.info("User signed out")
        for k in list(st.session_state.keys()):
            if k != "csv_path":
                del st.session_state[k]
        st.rerun()

    st.markdown("<hr style='border-color:#2E75B6; margin:12px 0;'>", unsafe_allow_html=True)
    st.caption(
        "Authentication uses Microsoft Entra ID.\n"
        "No passwords are stored by this app.\n"
        "Access governed by your Azure RBAC roles."
    )

    st.markdown("<hr style='border-color:#2E75B6; margin:12px 0;'>", unsafe_allow_html=True)
    demo_mode = st.toggle(
        "Demo Mode",
        value=st.session_state.get("demo_mode", False),
        help="Simulate RI / SP recommendations & commitments from invoice data (no Azure sign-in required).",
    )
    st.session_state["demo_mode"] = demo_mode
    if demo_mode:
        st.markdown('<span class="status-pill pill-green">● Demo data active</span>', unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════════════════
#  HEADER
# ═══════════════════════════════════════════════════════════════════════════════
st.markdown("## 📊 &nbsp; Azure Cost & Commitment Dashboard", unsafe_allow_html=True)
st.markdown(
    f"<span style='color:#888; font-size:0.85rem;'>{datetime.now():%A, %B %d, %Y}</span>",
    unsafe_allow_html=True,
)

# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 1 — Invoice File Selection
# ═══════════════════════════════════════════════════════════════════════════════
st.markdown('<hr class="section-divider">', unsafe_allow_html=True)
st.markdown("### 1 &nbsp;&nbsp; Invoice Export", unsafe_allow_html=True)
st.caption("Select your Azure invoice-detail CSV or Excel file. Excel inputs are converted to CSV automatically before analysis.")

col_path, col_browse, col_analyze = st.columns([5, 1, 1])
with col_path:
    selected_file = st.text_input(
        "Invoice file path",
        value=st.session_state.get("csv_path", ""),
        placeholder=r"C:\Exports\invoice-detail.csv  or  .xlsx",
        label_visibility="collapsed",
    )
with col_browse:
    if st.button("Browse…", use_container_width=True):
        picked = _pick_file()
        if picked:
            st.session_state["csv_path"] = picked
            st.rerun()
with col_analyze:
    _manual_analyze = st.button("Analyse", use_container_width=True)

if selected_file:
    st.session_state["csv_path"] = selected_file

# Auto-analyze when a valid file is provided and hasn't been analyzed yet,
# or when the user explicitly clicks the Analyse button.
_VALID_EXTENSIONS = {".csv", ".xlsx", ".xls", ".xlsm", ".xlsb"}
_file_is_new = (
    selected_file
    and Path(selected_file).suffix.lower() in _VALID_EXTENSIONS
    and Path(selected_file).exists()
    and (
        st.session_state.get("_analyzed_file") != selected_file
        or _manual_analyze
    )
)

if _file_is_new:
        try:
            logger.info("User clicked Analyze for file: %s", selected_file)
            analysis_file = selected_file

            # ── Clear stale results from previous file ─────────────────────
            for _stale_key in (
                "savings_analysis", "csv_recs", "recommendations",
                "commitments", "recommendation_summary", "cost_query_rows",
                "azure_warnings",
            ):
                st.session_state.pop(_stale_key, None)

            if Path(selected_file).suffix.lower() in {".xlsx", ".xls", ".xlsm", ".xlsb"}:
                # ── Excel → intermediate conversion with live progress bar ──
                progress_bar = st.progress(0, text="Converting Excel… preparing")

                def _update_progress(rows_done: int, total_est: int) -> None:
                    frac = min(rows_done / max(total_est, 1), 1.0)
                    progress_bar.progress(
                        frac,
                        text=f"Converting Excel… {rows_done:,} / ~{total_est:,} rows",
                    )

                analysis_file = convert_excel_to_csv(
                    selected_file, progress=_update_progress,
                )
                progress_bar.progress(1.0, text="Conversion complete ✓")
                st.session_state["csv_path"] = analysis_file
                logger.info("Excel input converted. Using %s for analysis: %s",
                            Path(analysis_file).suffix, analysis_file)

            with st.spinner("Analysing invoice data…"):
                summary_obj, pivots = summarize_export(analysis_file)
                st.session_state["summary"] = summary_to_dict(summary_obj)
                st.session_state["pivots"] = pivots
                st.session_state["_analyzed_file"] = selected_file
                st.session_state["csv_path"] = analysis_file
                logger.info("Invoice analysis completed successfully")

            # ── CSV-based RI / SP opportunity detection ────────────────────────
            with st.spinner("Detecting RI / SP opportunities from CSV…"):
                try:
                    csv_recs = detect_ri_opportunities(
                        analysis_file, lookup_prices=True, max_price_workers=4,
                    )
                    if csv_recs:
                        logger.info("CSV RI detection: %d opportunities found", len(csv_recs))
                        # Bridge embedded retail prices into the dict format
                        # expected by build_savings_analysis  (key = rec index)
                        csv_retail: dict[int, dict] = {}
                        for idx, rec in enumerate(csv_recs):
                            rp = (rec.raw or {}).get("retailPrices")
                            if rp:
                                csv_retail[idx] = rp
                        savings_data = build_savings_analysis(
                            analysis_file, csv_recs, retail_prices=csv_retail,
                        )
                        savings_data["source"] = "csv_detection"
                        st.session_state["savings_analysis"] = savings_data
                        st.session_state["csv_recs"] = csv_recs
                        logger.info("CSV-based savings analysis stored")
                    else:
                        logger.info("No RI-eligible SKUs detected from CSV")
                        st.info(
                            "No RI / SP-eligible services detected in this billing data. "
                            "Savings analysis requires usage from services like "
                            "Virtual Machines, SQL Database, Cosmos DB, App Service, etc."
                        )
                except Exception as ex:
                    logger.warning("CSV RI detection failed (non-fatal): %s", ex, exc_info=True)
                    st.warning(f"RI / SP opportunity detection encountered an error: {ex}")

            st.toast("Invoice analysis complete.", icon="✅")
            st.rerun()
        except Exception as ex:
            logger.exception("Invoice analysis failed for %s", selected_file)
            st.error(f"Analysis failed: {ex}")

# ── KPI strip ──────────────────────────────────────────────────────────────────
if "summary" in st.session_state:
    s = st.session_state["summary"]
    ccy = s.get("currency", "USD")

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Total Spend", f"{ccy} {s['total_cost']:,.2f}")
    k2.metric("Line Items", f"{s['record_count']:,}")
    k3.metric("Period Start", str(s.get("period_start") or "—"))
    k4.metric("Period End", str(s.get("period_end") or "—"))

    # ── Tabs with charts + tables ──────────────────────────────────────────────
    st.markdown('<hr class="section-divider">', unsafe_allow_html=True)
    pivots = st.session_state.get("pivots", {})

    tab_svc, tab_sub, tab_chg, tab_day = st.tabs([
        "By Service", "By Subscription", "By Charge Type", "Daily Trend",
    ])

    with tab_svc:
        if "cost_by_service_family" in pivots:
            df = pivots["cost_by_service_family"].to_pandas()
            left, right = st.columns([1, 1])
            with left:
                st.bar_chart(df.set_index("ServiceFamily")["TotalCost"])
            with right:
                st.dataframe(
                    df.style.format({"TotalCost": "${:,.2f}"}),
                    use_container_width=True, hide_index=True,
                )

    with tab_sub:
        if "cost_by_subscription" in pivots:
            df = pivots["cost_by_subscription"].to_pandas()
            left, right = st.columns([1, 1])
            with left:
                st.bar_chart(df.set_index("SubscriptionName")["TotalCost"])
            with right:
                st.dataframe(
                    df.style.format({"TotalCost": "${:,.2f}"}),
                    use_container_width=True, hide_index=True,
                )

    with tab_chg:
        if "cost_by_charge_type" in pivots:
            df = pivots["cost_by_charge_type"].to_pandas()
            st.dataframe(
                df.style.format({"TotalCost": "${:,.2f}"}),
                use_container_width=True, hide_index=True,
            )

    with tab_day:
        if "cost_by_day" in pivots:
            day_df = pivots["cost_by_day"].to_pandas().sort_values("Date")
            st.line_chart(day_df.set_index("Date")["TotalCost"], use_container_width=True)


# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 2 — Azure Enrichment
# ═══════════════════════════════════════════════════════════════════════════════
st.markdown('<hr class="section-divider">', unsafe_allow_html=True)
st.markdown("### 2 &nbsp;&nbsp; Azure Enrichment", unsafe_allow_html=True)
st.caption("Pull live RI / Savings Plan inventory, Advisor cost recommendations, and Cost Management data.")

signed_in = "credential" in st.session_state
is_demo = st.session_state.get("demo_mode", False)

# ── Demo-mode enrichment ───────────────────────────────────────────────────────
if is_demo:
    st.info("**Demo Mode** — Simulated RI/SP data will be generated from your invoice file.")
    if st.button("Generate Demo Data", type="primary"):
        csv_path = st.session_state.get("csv_path", "")
        if not csv_path:
            st.warning("Load an invoice file first (Step 1).")
        else:
            with st.spinner("Generating simulated Azure data…"):
                try:
                    logger.info("Generating demo data from %s", csv_path)
                    demo = generate_demo_data(csv_path)
                    all_recs = demo["recommendations"]
                    commitments_list = demo["commitments"]
                    st.session_state["commitments"] = commitment_rows(commitments_list)
                    st.session_state["recommendations"] = recommendation_rows(all_recs)
                    st.session_state["recommendation_summary"] = build_recommendation_summary(
                        all_recs, commitments_list
                    )
                    st.session_state["cost_query_rows"] = demo["cost_query_rows"]
                    st.session_state["azure_warnings"] = ["Demo mode — all Azure data is simulated."]

                    # Build savings analysis from demo recs
                    from src.analysis import build_savings_analysis as _bsa
                    savings_data = _bsa(
                        csv_path,
                        all_recs,
                        augmented_annual_paygo=demo.get("full_annual_paygo"),
                    )
                    st.session_state["savings_analysis"] = savings_data

                    logger.info("Demo data generation completed")
                    st.toast("Demo data generated.", icon="✅")
                    st.rerun()
                except Exception as ex:
                    logger.exception("Demo data generation failed")
                    st.error(f"Demo generation failed: {ex}")

# ── Live Azure enrichment ──────────────────────────────────────────────────────
if not is_demo and st.button("Fetch Azure Data", type="primary", disabled=not signed_in):
    credential = st.session_state["credential"]
    progress = st.progress(0, text="Discovering subscriptions…")
    try:
        logger.info("Starting Azure data enrichment")
        try:
            subs = list_subscriptions(credential)
        except Exception as ex:
            subs = []
            logger.warning("Failed to list subscriptions: %s", ex)
            warnings = [_azure_warning("Subscriptions", ex)]
        else:
            warnings: list[str] = []

        env_ids = os.getenv("AZURE_SUBSCRIPTION_IDS", "").strip()
        sub_ids = [i.strip() for i in env_ids.split(",") if i.strip()]
        if not sub_ids:
            sub_ids = [s["subscriptionId"] for s in subs if s.get("subscriptionId")]
        logger.info("Operating on %d subscriptions", len(sub_ids))

        if not sub_ids:
            warnings.append("Subscriptions: No accessible subscriptions found. Azure enrichment will continue for tenant-level endpoints only.")

        # Reservations
        progress.progress(20, text="Fetching reservations…")
        try:
            reservations = list_reservations(credential)
        except Exception as ex:
            reservations = []
            logger.warning("Failed to list reservations: %s", ex)
            warnings.append(_azure_warning("Reservations", ex))

        # Savings Plans
        progress.progress(40, text="Fetching savings plans…")
        try:
            savings_plans = list_savings_plans(credential)
        except Exception as ex:
            savings_plans = []
            logger.warning("Failed to list savings plans: %s", ex)
            warnings.append(_azure_warning("Savings Plans", ex))

        commitments = reservations + savings_plans

        # Advisor (fetched in parallel for all subscriptions)
        progress.progress(60, text="Fetching Advisor recommendations…")
        all_recs = list_advisor_cost_recommendations_parallel(credential, sub_ids)

        # Cost Management queries (all subscriptions in parallel)
        progress.progress(80, text="Running Cost Management queries…")
        p_start = st.session_state.get("summary", {}).get("period_start")
        p_end = st.session_state.get("summary", {}).get("period_end")
        cq_rows: list[dict] = []
        if p_start and p_end:
            cq_rows = query_cost_by_service_parallel(credential, sub_ids, p_start, p_end)

        st.session_state["commitments"] = commitment_rows(commitments)
        st.session_state["recommendations"] = recommendation_rows(all_recs)
        st.session_state["recommendation_summary"] = build_recommendation_summary(all_recs, commitments)
        st.session_state["cost_query_rows"] = cq_rows
        st.session_state["azure_warnings"] = warnings

        # ── Build RI / SP savings analysis ─────────────────────────────────────
        csv_path = st.session_state.get("csv_path", "")
        if csv_path and all_recs:
            try:
                # Look up real Azure retail prices for Advisor recommendations
                progress.progress(90, text="Looking up Azure retail prices…")
                retail_prices = batch_lookup_prices(all_recs, max_workers=3)
                if retail_prices:
                    logger.info("Retail prices found for %d / %d recommendations",
                                len(retail_prices), len(all_recs))
                else:
                    logger.info("No retail prices retrieved — using estimated discount rates")

                savings_data = build_savings_analysis(
                    csv_path, all_recs, retail_prices=retail_prices,
                )
                savings_data["source"] = "advisor"
                st.session_state["savings_analysis"] = savings_data
            except Exception as ex:
                logger.exception("Savings analysis failed")
                warnings.append(f"Savings analysis: {ex}")
                # Keep existing CSV-detected savings if available
                if not st.session_state.get("savings_analysis"):
                    st.session_state["savings_analysis"] = None
        else:
            # No Advisor recs — keep CSV-detected savings if already present
            if not st.session_state.get("savings_analysis"):
                st.session_state["savings_analysis"] = None

        progress.progress(100, text="Done.")
        logger.info(
            "Azure enrichment complete: %d commitments, %d recs, %d cost rows, %d warnings",
            len(commitments), len(all_recs), len(cq_rows), len(warnings),
        )
        st.toast("Azure enrichment complete.", icon="✅")
        st.rerun()
    except Exception as ex:
        logger.exception("Azure enrichment failed")
        st.error(f"Enrichment failed: {ex}")

if not signed_in and not is_demo:
    st.info("Sign in from the sidebar to enable Azure data enrichment, or enable **Demo Mode** to simulate data.")

# ── Enrichment KPIs ────────────────────────────────────────────────────────────
if "recommendation_summary" in st.session_state:
    rs = st.session_state["recommendation_summary"]
    ccy = st.session_state.get("summary", {}).get("currency", "USD")

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Active Commitments", rs.get("active_commitments", "—"))
    m2.metric("Expiring ≤ 90 Days", rs.get("commitments_expiring_within_90_days", "—"))
    m3.metric("Advisor Recommendations", rs.get("advisor_cost_recommendation_count", "—"))
    m4.metric("Est. Annual Savings", f"{ccy} {rs.get('advisor_estimated_annual_savings', 0):,.2f}")

# ── Warnings ───────────────────────────────────────────────────────────────────
if st.session_state.get("azure_warnings"):
    with st.expander("⚠️ Azure data warnings", expanded=False):
        for w in st.session_state["azure_warnings"]:
            st.warning(w)

# ═══════════════════════════════════════════════════════════════════════════════
#  PAYG vs RI/SP Savings Analysis — Separate Options
# ═══════════════════════════════════════════════════════════════════════════════
if st.session_state.get("savings_analysis"):
    sa = st.session_state["savings_analysis"]
    ri = sa.get("ri_analysis", {})
    sp = sa.get("sp_analysis", {})
    hybrid = sa.get("hybrid_analysis", {})
    ri_kpi = ri.get("kpi", {})
    sp_kpi = sp.get("kpi", {})
    hybrid_kpi = hybrid.get("kpi", {})
    ccy_sa = st.session_state.get("summary", {}).get("currency", "USD")

    st.markdown('<hr class="section-divider">', unsafe_allow_html=True)
    st.markdown("### 💰 &nbsp; Pay-Go vs Commitment Savings Projections", unsafe_allow_html=True)

    pricing_note = ""
    if sa.get("has_retail_prices"):
        n_retail = ri_kpi.get("retail_prices_used", 0)
        pricing_note = f"  **{n_retail}** recommendation(s) priced via Azure Retail API."

    is_csv_source = sa.get("source") == "csv_detection"
    n_recs = ri_kpi.get("total_recommendations", 0)
    if is_csv_source:
        source_label = f"{n_recs} RI-eligible SKU groups detected from billing CSV"
    else:
        source_label = f"{n_recs} Azure Advisor recommendations"
    st.caption(
        f"Based on {sa.get('period_days', 0)}-day invoice run rate extrapolated to 3 years, "
        f"combined with {source_label}."
        + pricing_note
    )

    # ── Helper to render one variant's tabs ────────────────────────────────────
    def _render_variant_tabs(variant_data: dict, prefix: str) -> None:
        cats = variant_data.get("savings_by_category", [])
        regions = variant_data.get("savings_by_region", [])
        opps = variant_data.get("top_opportunities", [])

        tab_cat, tab_rgn, tab_top = st.tabs([
            f"{prefix} By Resource Type",
            f"{prefix} By Region",
            f"{prefix} Top Opportunities",
        ])

        with tab_cat:
            if cats:
                cat_df = pd.DataFrame(cats)
                left, right = st.columns([1, 1])
                with left:
                    chart_df = cat_df[["Resource Type", "Savings (3-Yr)"]].copy()
                    chart_df = chart_df[chart_df["Savings (3-Yr)"] > 0].head(15)
                    if not chart_df.empty:
                        st.bar_chart(chart_df.set_index("Resource Type"))
                with right:
                    st.dataframe(
                        cat_df.style.format({
                            "Current 3-Yr Cost": "${:,.2f}",
                            "Commitment 3-Yr Cost": "${:,.2f}",
                            "Savings (3-Yr)": "${:,.2f}",
                            "Annual Savings": "${:,.2f}",
                            "Monthly Savings": "${:,.2f}",
                            "Savings %": "{:.1f}%",
                            "% of Total Savings": "{:.1f}%",
                        }),
                        use_container_width=True, hide_index=True,
                    )
            else:
                st.info("No category-level savings data available.")

        with tab_rgn:
            if regions:
                rgn_df = pd.DataFrame(regions)
                left, right = st.columns([1, 1])
                with left:
                    chart_df = rgn_df[["Region", "Savings (3-Yr)"]].copy()
                    chart_df = chart_df[chart_df["Savings (3-Yr)"] > 0]
                    if not chart_df.empty:
                        st.bar_chart(chart_df.set_index("Region"))
                with right:
                    st.dataframe(
                        rgn_df.style.format({
                            "Current 3-Yr Cost": "${:,.2f}",
                            "Commitment 3-Yr Cost": "${:,.2f}",
                            "Savings (3-Yr)": "${:,.2f}",
                            "Annual Savings": "${:,.2f}",
                            "Savings %": "{:.1f}%",
                            "% of Total Savings": "{:.1f}%",
                        }),
                        use_container_width=True, hide_index=True,
                    )
            else:
                st.info("No region-level savings data available.")

        with tab_top:
            if opps:
                opp_df = pd.DataFrame(opps)
                display_cols = [c for c in opp_df.columns if c != "Description"]
                st.dataframe(
                    opp_df[display_cols].style.format({
                        "Current 3-Yr Cost": "${:,.2f}",
                        "Commitment 3-Yr Cost": "${:,.2f}",
                        "Savings (3-Yr)": "${:,.2f}",
                        "Annual Savings": "${:,.2f}",
                        "Savings %": "{:.1f}%",
                    }),
                    use_container_width=True, hide_index=True,
                )
            else:
                st.info("No opportunity data available.")

    # ── Hybrid strategy toggle ─────────────────────────────────────────────────
    show_hybrid = st.toggle(
        "🔀 Hybrid Strategy — SP for compute + RI for everything else",
        value=False,
        help=(
            "Combines Savings Plan rates for compute-eligible services "
            "(VMs, App Service, AKS, Functions) with Reserved Instance rates "
            "for SP-ineligible services (SQL DB, Cosmos DB, Redis, etc.)."
        ),
    )

    if show_hybrid and hybrid_kpi:
        # ── Single-column hybrid headline ──────────────────────────────────────
        st.markdown("#### Hybrid Strategy — Savings Plan + Reserved Instances")
        h_rate_src = hybrid_kpi.get("discount_rate_source", "estimated")
        h_rate_label = (
            "Retail API" if h_rate_src == "retail"
            else "mixed Retail + est." if h_rate_src == "mixed"
            else "estimated"
        )
        n_sp_recs = hybrid_kpi.get("sp_eligible_recs", 0)
        n_ri_recs = hybrid_kpi.get("ri_only_recs", 0)
        st.caption(
            f"{hybrid_kpi.get('discount_rate', 0) * 100:.0f}% weighted avg discount ({h_rate_label}) · "
            f"{n_sp_recs} SP-eligible + {n_ri_recs} RI-only recommendations · 3-year term"
        )

        col_h1, col_h2, col_h3 = st.columns(3)
        with col_h1:
            st.metric("Current 3-Yr Spend (PAYG)",
                       f"{ccy_sa} {hybrid_kpi.get('current_3yr_spend', 0):,.2f}")
        with col_h2:
            st.metric("Hybrid 3-Yr Cost",
                       f"{ccy_sa} {hybrid_kpi.get('commitment_3yr_cost', 0):,.2f}",
                       delta=f"-{ccy_sa} {hybrid_kpi.get('total_3yr_savings', 0):,.2f}",
                       delta_color="inverse")
        with col_h3:
            st.metric("Annual Hybrid Savings",
                       f"{ccy_sa} {hybrid_kpi.get('annual_savings', 0):,.2f}")

        # ── Comparison row: Pure RI vs Pure SP vs Hybrid ───────────────────────
        st.markdown("###### Strategy Comparison")
        cmp_cols = st.columns(3)
        with cmp_cols[0]:
            st.metric("Pure RI Savings (3-Yr)",
                       f"{ccy_sa} {ri_kpi.get('total_3yr_savings', 0):,.2f}")
        with cmp_cols[1]:
            st.metric("Pure SP Savings (3-Yr)",
                       f"{ccy_sa} {sp_kpi.get('total_3yr_savings', 0):,.2f}")
        with cmp_cols[2]:
            st.metric("Hybrid Savings (3-Yr)",
                       f"{ccy_sa} {hybrid_kpi.get('total_3yr_savings', 0):,.2f}")

        with st.expander("**Hybrid Strategy** (detail)", expanded=True):
            _render_variant_tabs(hybrid, "Hybrid")

    else:
        col_ri, col_sp = st.columns(2)
        with col_ri:
            st.markdown("#### Option A — Reserved Instances")
            ri_rate_src = ri_kpi.get("discount_rate_source", "estimated")
            ri_rate_label = (
                "Retail API" if ri_rate_src == "retail"
                else "mixed Retail + est." if ri_rate_src == "mixed"
                else "estimated"
            )
            st.caption(
                f"{ri_kpi.get('discount_rate', 0.40) * 100:.0f}% avg discount ({ri_rate_label}) · "
                f"Locked to specific SKU + region · 3-year term"
            )
            st.metric("Current 3-Yr Spend (PAYG)", f"{ccy_sa} {ri_kpi.get('current_3yr_spend', 0):,.2f}")
            st.metric("RI 3-Yr Cost", f"{ccy_sa} {ri_kpi.get('commitment_3yr_cost', 0):,.2f}",
                       delta=f"-{ccy_sa} {ri_kpi.get('total_3yr_savings', 0):,.2f}", delta_color="inverse")
            st.metric("Annual RI Savings", f"{ccy_sa} {ri_kpi.get('annual_savings', 0):,.2f}")
        with col_sp:
            st.markdown("#### Option B — Savings Plans")
            sp_rate_src = sp_kpi.get("discount_rate_source", "estimated")
            sp_rate_label = (
                "Retail API" if sp_rate_src == "retail"
                else "mixed Retail + est." if sp_rate_src == "mixed"
                else "estimated"
            )
            st.caption(
                f"{sp_kpi.get('discount_rate', 0.30) * 100:.0f}% avg discount ({sp_rate_label}) · "
                f"Flexible across SKU / region · 3-year term"
            )
            st.metric("Current 3-Yr Spend (PAYG)", f"{ccy_sa} {sp_kpi.get('current_3yr_spend', 0):,.2f}")
            st.metric("SP 3-Yr Cost", f"{ccy_sa} {sp_kpi.get('commitment_3yr_cost', 0):,.2f}",
                       delta=f"-{ccy_sa} {sp_kpi.get('total_3yr_savings', 0):,.2f}", delta_color="inverse")
            st.metric("Annual SP Savings", f"{ccy_sa} {sp_kpi.get('annual_savings', 0):,.2f}")

        with st.expander("**Option A — Reserved Instances** (detail)", expanded=True):
            _render_variant_tabs(ri, "RI")
        with st.expander("**Option B — Savings Plans** (detail)", expanded=False):
            _render_variant_tabs(sp, "SP")


# ── Commitment detail ──────────────────────────────────────────────────────────
if st.session_state.get("commitments"):
    with st.expander("Reserved Instances & Savings Plans", expanded=False):
        cdf = pd.DataFrame(st.session_state["commitments"])
        st.dataframe(cdf, use_container_width=True, hide_index=True)

# ── Advisor detail ─────────────────────────────────────────────────────────────
if st.session_state.get("recommendations"):
    with st.expander("Advisor Cost Recommendations", expanded=False):
        rdf = pd.DataFrame(st.session_state["recommendations"])
        st.dataframe(
            rdf.style.format({"AnnualSavingsEstimate": "${:,.2f}"}),
            use_container_width=True, hide_index=True,
        )


# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 3 — Executive Report Export
# ═══════════════════════════════════════════════════════════════════════════════
st.markdown('<hr class="section-divider">', unsafe_allow_html=True)
st.markdown("### 3 &nbsp;&nbsp; Executive Report", unsafe_allow_html=True)
st.caption("Generate a formatted Excel workbook with branded charts — ready for director-level distribution.")

if "summary" in st.session_state:
    if st.button("Generate Executive Report", type="primary"):
        with st.spinner("Building workbook…"):
            logger.info("User initiated Excel report generation")
            wb_bytes = build_excel_workbook(
                summary=st.session_state["summary"],
                pivots=st.session_state.get("pivots", {}),
                commitments=st.session_state.get("commitments", []),
                recommendations=st.session_state.get("recommendations", []),
                recommendation_summary=st.session_state.get("recommendation_summary", {}),
                cost_query_rows=st.session_state.get("cost_query_rows", []),
                savings_analysis=st.session_state.get("savings_analysis"),
            )
            st.download_button(
                label="⬇  Download Executive Report (.xlsx)",
                data=wb_bytes,
                file_name=f"Azure_MACC_FinOps_Report_{datetime.now():%Y%m%d}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                type="primary",
            )
else:
    st.info("Analyze an invoice export first to enable report generation.")

# ── Disclaimer footer ──────────────────────────────────────────────────────────
st.markdown("<hr style='margin-top:2rem;'>", unsafe_allow_html=True)
st.markdown(
    '<div style="font-size:0.75rem; color:#888; line-height:1.4; padding:0.5rem 0 1rem;">'
    '⚠️ <b>Disclaimer:</b> All analysis, savings estimates, and recommendations '
    'presented by Azure MACC Analyst are for <b>estimation and demonstrative '
    'purposes only</b>. Figures are approximations based on publicly available '
    'Azure retail pricing data and standard discount assumptions. '
    '<b>No guarantees</b> are provided regarding the accuracy, completeness, '
    'or realisation of projected savings. This tool does not constitute financial '
    'advice. Always validate findings with your Microsoft account team and '
    'conduct your own due diligence before making any commitment or purchasing '
    'decisions.</div>',
    unsafe_allow_html=True,
)
