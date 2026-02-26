"""Azure MACC Analyst — FinOps Executive Dashboard."""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

APP_VERSION = "1.1.1"

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
    list_reservations,
    list_savings_plans,
    list_subscriptions,
    query_cost_by_service,
)
from src.demo_data import generate_demo_data
from src.excel_export import build_excel_workbook
from src.file_loader import convert_excel_to_csv


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
    analyze_clicked = st.button("Analyze", type="primary", use_container_width=True)

if selected_file:
    st.session_state["csv_path"] = selected_file

if analyze_clicked:
    if not selected_file:
        st.warning("Select an invoice file first.")
    else:
        try:
            logger.info("User clicked Analyze for file: %s", selected_file)
            analysis_file = selected_file

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
                logger.info("Invoice analysis completed successfully")
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

        # Advisor
        progress.progress(60, text="Fetching Advisor recommendations…")
        all_recs: list = []
        for sid in sub_ids:
            try:
                all_recs.extend(list_advisor_cost_recommendations(credential, sid))
            except Exception as ex:
                logger.warning("Failed Advisor query for subscription %s: %s", sid, ex)
                warnings.append(_azure_warning("Advisor", ex, sid))

        # Cost Management queries (all subscriptions)
        progress.progress(80, text="Running Cost Management queries…")
        p_start = st.session_state.get("summary", {}).get("period_start")
        p_end = st.session_state.get("summary", {}).get("period_end")
        cq_rows: list[dict] = []
        for sid in sub_ids:
            if p_start and p_end:
                try:
                    cq_rows.extend(
                        query_cost_by_service(
                            credential=credential,
                            scope=f"/subscriptions/{sid}",
                            start_date=f"{p_start}T00:00:00Z",
                            end_date=f"{p_end}T23:59:59Z",
                        )
                    )
                except Exception as ex:
                    logger.warning("Cost query failed for subscription %s: %s", sid, ex)
                    warnings.append(_azure_warning("Cost query", ex, sid))

        st.session_state["commitments"] = commitment_rows(commitments)
        st.session_state["recommendations"] = recommendation_rows(all_recs)
        st.session_state["recommendation_summary"] = build_recommendation_summary(all_recs, commitments)
        st.session_state["cost_query_rows"] = cq_rows
        st.session_state["azure_warnings"] = warnings

        # ── Build RI / SP savings analysis ─────────────────────────────────────
        csv_path = st.session_state.get("csv_path", "")
        if csv_path and all_recs:
            try:
                savings_data = build_savings_analysis(csv_path, all_recs)
                st.session_state["savings_analysis"] = savings_data
            except Exception as ex:
                logger.exception("Savings analysis failed")
                warnings.append(f"Savings analysis: {ex}")
                st.session_state["savings_analysis"] = None
        else:
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
#  PAYG vs RI/SP Savings Analysis
# ═══════════════════════════════════════════════════════════════════════════════
if st.session_state.get("savings_analysis"):
    sa = st.session_state["savings_analysis"]
    kpi = sa.get("kpi", {})
    ccy_sa = st.session_state.get("summary", {}).get("currency", "USD")

    st.markdown('<hr class="section-divider">', unsafe_allow_html=True)
    st.markdown("### 💰 &nbsp; Pay-Go vs RI / SP Savings Projection", unsafe_allow_html=True)
    st.caption(
        f"Based on {sa.get('period_days', 0)}-day invoice run rate extrapolated to 3 years, "
        f"combined with {kpi.get('total_recommendations', 0)} Azure Advisor recommendations."
    )

    # ── Savings headline KPIs ──────────────────────────────────────────────────
    sk1, sk2, sk3, sk4, sk5 = st.columns(5)
    sk1.metric("Current 3-Yr Spend (PAYG)", f"{ccy_sa} {kpi.get('current_3yr_spend', 0):,.2f}")
    sk2.metric("RI/SP 3-Yr Cost", f"{ccy_sa} {kpi.get('ri_sp_3yr_cost', 0):,.2f}")
    sk3.metric("Total 3-Year Savings", f"{ccy_sa} {kpi.get('total_3yr_savings', 0):,.2f}")
    sk4.metric("Savings %", f"{kpi.get('savings_pct', 0):.1f}%")
    sk5.metric("Annual Savings", f"{ccy_sa} {kpi.get('annual_savings', 0):,.2f}")

    # ── Tabs: By Category / By Region / Top Opportunities ─────────────────────
    tab_cat, tab_rgn, tab_top = st.tabs([
        "By Resource Type", "By Region", "Top Opportunities",
    ])

    with tab_cat:
        cats = sa.get("savings_by_category", [])
        if cats:
            cat_df = pd.DataFrame(cats)
            left, right = st.columns([1, 1])
            with left:
                chart_df = cat_df[["Resource Type", "Net Savings (3-Yr)"]].copy()
                chart_df = chart_df[chart_df["Net Savings (3-Yr)"] > 0].head(15)
                if not chart_df.empty:
                    st.bar_chart(chart_df.set_index("Resource Type")["Net Savings (3-Yr)"])
            with right:
                st.dataframe(
                    cat_df.style.format({
                        "Current 3-Yr Cost": "${:,.2f}",
                        "RI/SP 3-Yr Cost": "${:,.2f}",
                        "Net Savings (3-Yr)": "${:,.2f}",
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
        regions = sa.get("savings_by_region", [])
        if regions:
            rgn_df = pd.DataFrame(regions)
            left, right = st.columns([1, 1])
            with left:
                chart_df = rgn_df[["Region", "Net Savings (3-Yr)"]].copy()
                chart_df = chart_df[chart_df["Net Savings (3-Yr)"] > 0]
                if not chart_df.empty:
                    st.bar_chart(chart_df.set_index("Region")["Net Savings (3-Yr)"])
            with right:
                st.dataframe(
                    rgn_df.style.format({
                        "Current 3-Yr Cost": "${:,.2f}",
                        "RI/SP 3-Yr Cost": "${:,.2f}",
                        "Net Savings (3-Yr)": "${:,.2f}",
                        "Annual Savings": "${:,.2f}",
                        "Savings %": "{:.1f}%",
                        "% of Total Savings": "{:.1f}%",
                    }),
                    use_container_width=True, hide_index=True,
                )
        else:
            st.info("No region-level savings data available.")

    with tab_top:
        opps = sa.get("top_opportunities", [])
        if opps:
            opp_df = pd.DataFrame(opps)
            display_cols = [c for c in opp_df.columns if c != "Description"]
            st.dataframe(
                opp_df[display_cols].style.format({
                    "Current 3-Yr Cost": "${:,.2f}",
                    "RI/SP 3-Yr Cost": "${:,.2f}",
                    "Net Savings (3-Yr)": "${:,.2f}",
                    "Annual Savings": "${:,.2f}",
                    "Savings %": "{:.1f}%",
                }),
                use_container_width=True, hide_index=True,
            )
        else:
            st.info("No opportunity data available.")

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
