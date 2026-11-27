"""
LabLens - Clinical Laboratory insights for lab staff  (Module 7)

An internal decision-support tool for laboratory staff (no directly identifying patient data).
Pages:
  🏠 Home          the lab's story in plain English: 4 key numbers, what happened, what needs attention
  📊 Performance   charts with filters (PostgreSQL warehouse)
  🔧 Analyzers     warnings from the Isolation Forest model + "will this sample be late?" (Random Forest)
  💬 Ask LabLens   questions about our numbers (Llama -> safe SQL) and about our SOPs (RAG with sources)
  🧠 Briefing      AI explanation of what happened, with facts vs hypotheses and a number check
  🛡️ Data trust    data checks, cleaning rules, update history, AI activity log
  ℹ️ Help          how LabLens works + meaning of words

Run from the project folder (with .venv switched on):
    streamlit run app/streamlit_app.py
"""
import subprocess
import sys
import traceback
from pathlib import Path

import joblib
import pandas as pd
import plotly.express as px
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import analytics                                    # noqa: E402
from connectors import get_pg_connection            # noqa: E402

st.set_page_config(page_title="LabLens", page_icon="🧪", layout="wide")

# ---------------------------------------------------------------------------
# Theme: "periwinkle-leaning cornflower"
# ---------------------------------------------------------------------------
PERIWINKLE = "#6F87E6"      # main brand colour (charts, accents)
DEEP = "#4E67D3"            # buttons / text accents (readable on white)
TINT = "#EEF1FC"            # soft background tint
INK = "#1E2A4A"             # text
GOOD, WATCH, ACT = "#1E9E62", "#D99A00", "#D64545"
SERIES = [PERIWINKLE, "#F2A541", "#3FB6A8", "#D96C9A", "#8C6BD1", "#7A8599", "#E07A5F", "#2F4BA8"]
FONT = '"Source Sans Pro", "Source Sans 3", sans-serif'   # the same font Streamlit uses
STATUS = {"good": ("🟢", "Good", GOOD), "watch": ("🟠", "Watch", WATCH), "act": ("🔴", "Needs action", ACT)}
px.defaults.template = "plotly_white"
px.defaults.color_discrete_sequence = SERIES   # every chart uses the LabLens palette
QUARTER_NAMES = {1: "Jan–Mar", 2: "Apr–Jun", 3: "Jul–Sep", 4: "Oct–Dec"}

st.markdown(f"""
<style>
  .block-container {{padding-top: 2rem;}}
  .ll-header {{background: linear-gradient(90deg, {PERIWINKLE} 0%, #9AABF0 100%); color: white;
               padding: 18px 24px; border-radius: 14px; margin-bottom: 18px;}}
  .ll-header h1 {{color: white; margin: 0; font-size: 1.8rem;}}
  .ll-header p  {{color: white; margin: 4px 0 0 0; opacity: .95;}}
  .ll-card {{background: white; border: 1px solid #DDE3F7; border-left: 6px solid var(--c);
             border-radius: 12px; padding: 14px 16px; min-height: 118px; margin-bottom: 14px;}}
  .ll-card .label {{color: #5A6480; font-size: .9rem;}}
  .ll-card .value {{color: {INK}; font-size: 1.9rem; font-weight: 700; line-height: 1.2;}}
  .ll-card .status {{color: var(--c); font-weight: 600; font-size: .9rem;}}
  .ll-box {{background: {TINT}; border-radius: 12px; padding: 14px 18px; margin: 6px 0 14px 0;}}
  .ll-box li {{margin-bottom: 4px;}}
  /* Fix 12: hide the automatic link icons next to headings */
  [data-testid="stHeaderActionElements"], .stMarkdown h1 a, .stMarkdown h2 a, .stMarkdown h3 a,
  .stMarkdown h4 a, .stMarkdown h5 a {{display: none !important;}}
</style>""", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def pg(sql, params=None):
    """Fixed queries written by the developer (not by the AI)."""
    with get_pg_connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return pd.DataFrame(cur.fetchall(), columns=[c.name for c in cur.description])


@st.cache_data(ttl=300, show_spinner="Loading lab data...")
def load_tests():
    df = analytics.load_lab_tests()
    # keep complete months only (the newest month is dropped if it has only a few days of data)
    last_day = pd.to_datetime(df["order_datetime"]).max()
    if last_day.day < 25:
        df = df[df["year_month"] < last_day.strftime("%Y-%m")]
    return df


@st.cache_data(ttl=300, show_spinner="Checking the analyzers...")
def load_anomalies():
    return analytics.detect_instrument_anomalies(analytics.load_instrument_weeks())


@st.cache_resource
def load_delay_model():
    path = PROJECT_ROOT / "models" / "delay_model.joblib"
    return joblib.load(path) if path.exists() else None


@st.cache_resource(show_spinner="Learning typical result times from past tests (first time only)...")
def load_time_model():
    """Fix 15 - Model B (regression): how many minutes until the result?
    Three gradient-boosting models predict the typical time (median) and a low/high range (10th/90th percentile).
    The range is then widened using held-out tests so that about 8 in 10 real results fall inside it
    (a method called conformal calibration)."""
    import numpy as np
    from sklearn.compose import ColumnTransformer
    from sklearn.ensemble import HistGradientBoostingRegressor
    from sklearn.metrics import mean_absolute_error
    from sklearn.model_selection import train_test_split
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import OneHotEncoder

    data = load_tests()
    X, _ = analytics.delay_dataset(data)
    y = pd.to_numeric(data.loc[X.index, "tat_min"])
    X_train, X_cal, y_train, y_cal = train_test_split(X, y, test_size=0.25, random_state=42)

    def make(q):
        prep = ColumnTransformer([("cat", OneHotEncoder(handle_unknown="ignore", sparse_output=False),
                                   analytics.DELAY_FEATURES_CAT)], remainder="passthrough")
        model = HistGradientBoostingRegressor(loss="quantile", quantile=q, max_iter=300,
                                              learning_rate=0.05, random_state=42)
        return Pipeline([("prep", prep), ("model", model)]).fit(X_train, y_train)

    typical, low, high = make(0.5), make(0.1), make(0.9)
    # how far outside the range the held-out results fell -> widen the range by that amount
    outside = np.maximum(low.predict(X_cal) - y_cal, y_cal - high.predict(X_cal))
    widen = float(np.quantile(outside, 0.8))
    lo_c, hi_c = low.predict(X_cal) - widen, high.predict(X_cal) + widen
    return {"typical": typical, "low": low, "high": high, "widen": widen,
            "mae": round(mean_absolute_error(y_cal, typical.predict(X_cal)), 1),
            "baseline_mae": round(mean_absolute_error(y_cal, np.full(len(y_cal), y_train.median())), 1),
            "coverage": round(100 * float(((y_cal >= lo_c) & (y_cal <= hi_c)).mean())),
            "n_tests": len(X)}


@st.cache_data(ttl=60)
def last_update():
    try:
        r = pg("SELECT finished_at FROM etl.run_log WHERE status = 'SUCCESS' ORDER BY batch_id DESC LIMIT 1")
        return None if r.empty else pd.to_datetime(r.iloc[0, 0])
    except Exception:
        return None


def ollama_available():
    try:
        import ollama
        ollama.list()
        return True
    except Exception:
        return False


def friendly_error(where, exc):
    st.error(f"Sorry - **{where}** could not load right now. The other pages still work. "
             "Please check that the databases (and Ollama, for AI pages) are running, then press **Refresh**.")
    with st.expander("Technical details (for the IT team)"):
        st.code("".join(traceback.format_exception(exc))[-3000:])


def header(title, subtitle):
    st.markdown(f'<div class="ll-header"><h1>{title}</h1><p>{subtitle}</p></div>', unsafe_allow_html=True)


def card(col, label, value, status, tip):
    icon, word, color = STATUS[status]
    col.markdown(f'<div class="ll-card" style="--c:{color}" title="{tip}"><div class="label">{label} ⓘ</div>'
                 f'<div class="value">{value}</div><div class="status">{icon} {word}</div></div>',
                 unsafe_allow_html=True)


def style_fig(fig, height=360):
    fig.update_layout(template="plotly_white", height=height, margin=dict(l=10, r=10, t=50, b=10),
                      font=dict(color=INK, family=FONT), title_font=dict(size=16, family=FONT),
                      legend_title_text="",
                      colorway=SERIES)
    return fig


def how_to_read(container, what, read, good):
    """Fix 14: a small ⓘ button under a chart; clicking it explains the chart in plain words."""
    with container.popover("ⓘ How to read this chart"):
        st.markdown(f"**What it shows:** {what}  \n**How to read it:** {read}  \n**What good looks like:** {good}")


def period_label(df):
    first, last = pd.Period(df["year_month"].min()), pd.Period(df["year_month"].max())
    return f"{first.strftime('%b')} – {last.strftime('%b %Y')}" if first.year == last.year \
        else f"{first.strftime('%b %Y')} – {last.strftime('%b %Y')}"


def go_to(name):
    st.session_state["page"] = name


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
PAGE_NAMES = ["🏠 Home", "📊 Performance", "🔧 Analyzers", "💬 Ask LabLens", "🧠 Briefing",
              "🛡️ Data trust", "ℹ️ Help"]
st.sidebar.markdown(f"<h2 style='color:{DEEP};margin-bottom:0'>🧪 LabLens</h2>"
                    "<p style='margin-top:0;color:#5A6480'>Clinical laboratory insights</p>",
                    unsafe_allow_html=True)
page = st.sidebar.radio("Menu", PAGE_NAMES, key="page", label_visibility="collapsed")
st.sidebar.divider()
use_ai = st.sidebar.toggle("Use AI", value=True, key="use_ai",
                           help="Switch off to see how LabLens works without the AI (fallback mode).")
ollama_up = ollama_available()
llm_ok = use_ai and ollama_up
if llm_ok:
    st.sidebar.markdown("🟢 **AI is on** · Ollama (Llama 3.2)")
elif not use_ai:
    st.sidebar.markdown("⚪ **AI switched off** · fallback mode")
    st.sidebar.caption("Turn on “Use AI” to use the AI again. Charts and numbers still work.")
else:
    st.sidebar.markdown("🔴 **AI is off** · Ollama not running")
    st.sidebar.caption("Open the Ollama app to turn on the AI. Charts and numbers still work.")
updated = last_update()
st.sidebar.markdown(f"🗓️ Data last loaded **{updated:%d %b %Y, %H:%M}**" if updated is not None
                    else "🗓️ No data loaded yet")
if st.sidebar.button("🔄 Refresh", width="stretch"):
    st.cache_data.clear()
    st.rerun()
st.sidebar.caption("🔒 Private: the AI runs on this computer and no data leaves it. "
                   "For lab staff only · de-identified data.")


# ---------------------------------------------------------------------------
# 🏠 HOME
# ---------------------------------------------------------------------------
def page_home():
    df = load_tests()
    resulted = df[df["resulted_at"].notna()]
    header("LabLens · Lab summary", f"{period_label(df)} · {len(df):,} tests")

    # ---- 4 key numbers -----------------------------------------------------
    avg_tat = resulted["tat_min"].mean()
    monthly = resulted.groupby("year_month")["tat_min"].mean()
    last_month = monthly.iloc[-1]
    on_time = 100 * resulted["tat_met"].astype(bool).mean()
    rejected = 100 * (df["sample_status"] == "REJECTED").mean()
    flagged = load_anomalies()
    recent = flagged[flagged["week"] >= flagged["week"].max() - pd.Timedelta(weeks=3)]
    warnings = int(recent["is_anomaly"].sum())

    c = st.columns(4)
    card(c[0], "Average time to result", f"{avg_tat:.0f} min",
         "good" if last_month <= avg_tat else ("watch" if last_month <= 1.15 * avg_tat else "act"),
         f"From sample received to result released. Last month: {last_month:.0f} min. "
         "Green = last month was at or below the average.")
    card(c[1], "Results on time", f"{on_time:.0f}%",
         "good" if on_time >= 90 else ("watch" if on_time >= 80 else "act"),
         "Share of results released within the time target for that test. Goal: 90%.")
    card(c[2], "Samples rejected", f"{rejected:.1f}%",
         "good" if rejected <= 5 else ("watch" if rejected <= 8 else "act"),
         "Samples the lab could not test (clotted, wrong tube, mislabeled...). Green = 5% or less.")
    card(c[3], "Analyzer warnings · 4 weeks", f"{warnings}",
         "good" if warnings == 0 else ("watch" if warnings <= 2 else "act"),
         "Weeks in which an analyzer behaved unusually (errors, downtime, QC failures, slow results).")

    # ---- What happened (sentences written by code from the data) -----------
    q = resulted.groupby("quarter")["tat_min"].mean()
    worst_q = int(q.idxmax())
    others = resulted[resulted["quarter"] != worst_q]["tat_min"].mean()
    bullets = [f"Results were slowest in <b>{QUARTER_NAMES[worst_q]}</b>: {q[worst_q]:.0f} min on average, "
               f"compared with {others:.0f} min in the other months."]
    in_q = resulted[(resulted["quarter"] == worst_q) & resulted["instrument_code"].notna()]
    by_inst = in_q.groupby("instrument_code")["tat_min"].mean()
    if not by_inst.empty:
        bad = by_inst.idxmax()
        rest = in_q[in_q["instrument_code"] != bad]["tat_min"].mean()
        bullets.append(f"The main reason was analyzer <b>{bad}</b>: {by_inst[bad]:.0f} min per result in that "
                       f"period, while the other analyzers averaged {rest:.0f} min.")
        after = resulted[(resulted["quarter"] > worst_q) & (resulted["instrument_code"] == bad)]
        before = resulted[(resulted["quarter"] < worst_q) & (resulted["instrument_code"] == bad)]
        if not after.empty and not before.empty:
            if after["tat_min"].mean() <= 1.2 * before["tat_min"].mean():
                bullets.append(f"{bad} is <b>back to normal</b> since then ({after['tat_min'].mean():.0f} min, "
                               f"similar to {before['tat_min'].mean():.0f} min before the problem).")
            else:
                bullets.append(f"{bad} is <b>still slower than before</b> ({after['tat_min'].mean():.0f} min vs "
                               f"{before['tat_min'].mean():.0f} min).")
    st.markdown("#### What happened")
    st.markdown('<div class="ll-box"><ul>' + "".join(f"<li>{b}</li>" for b in bullets) + "</ul></div>",
                unsafe_allow_html=True)

    # ---- Chart ---------------------------------------------------------------
    m = monthly.reset_index()
    m["month"] = pd.to_datetime(m["year_month"]).dt.strftime("%b")
    fig = px.line(m, x="month", y="tat_min", markers=True,
                  labels={"month": "", "tat_min": "Minutes"}, title="Average time to result, month by month")
    fig.update_traces(line=dict(color=PERIWINKLE, width=3), marker=dict(size=9))
    q_months = m[pd.to_datetime(m["year_month"]).dt.quarter == worst_q]["month"]
    if not q_months.empty:
        fig.add_vrect(x0=q_months.iloc[0], x1=q_months.iloc[-1], fillcolor=ACT, opacity=0.08, line_width=0,
                      annotation_text="slow period", annotation_position="top left")
    st.plotly_chart(style_fig(fig, 320), width="stretch", theme=None)
    how_to_read(st, 'The average number of minutes from the lab receiving a sample to the result being released, for each month.',
                'Each dot is one month. Higher = slower results. The pink shaded area is the slowest period.',
                'A fairly flat line (around 40–45 minutes here). A sudden jump means something slowed the lab down.')

    # ---- Needs attention -----------------------------------------------------
    attention = []
    shift = resulted.groupby("shift_name").agg(tat=("tat_min", "mean"), techs=("techs_present", "mean"))
    if not shift.empty:
        slow, few = shift["tat"].idxmax(), shift["techs"].idxmin()
        if slow == few:
            attention.append(("act", f"{slow} shift is the slowest ({shift.loc[slow, 'tat']:.0f} min) and has the "
                                     f"fewest staff ({shift.loc[slow, 'techs']:.1f} people on average)."))
        else:
            attention.append(("watch", f"{slow} shift is the slowest ({shift.loc[slow, 'tat']:.0f} min)."))
    if on_time < 90:
        attention.append(("watch", f"Results on time is {on_time:.0f}%, below the 90% goal."))
    dept = df.groupby("department_name")["sample_status"].apply(lambda s: 100 * (s == "REJECTED").mean())
    if not dept.empty and dept.max() > 1.5 * rejected:
        attention.append(("watch", f"{dept.idxmax()} sends the most rejected samples ({dept.max():.1f}% vs "
                                   f"{rejected:.1f}% overall) - a labeling/collection refresher may help."))
    if warnings:
        names = ", ".join(sorted(recent[recent["is_anomaly"]]["instrument_code"].unique()))
        attention.append(("act", f"Analyzer warning in the last 4 weeks: {names}. See the Analyzers page."))
    else:
        attention.append(("good", "No analyzer warnings in the last 4 weeks."))
    st.markdown("#### Needs attention")
    st.markdown('<div class="ll-box"><ul style="list-style:none;padding-left:0">' +
                "".join(f"<li>{STATUS[s][0]} {t}</li>" for s, t in attention) + "</ul></div>",
                unsafe_allow_html=True)

    c1, c2, c3 = st.columns(3)
    c1.button("💬 Ask LabLens a question", on_click=go_to, args=("💬 Ask LabLens",), width="stretch")
    c2.button("🧠 Get the AI briefing", on_click=go_to, args=("🧠 Briefing",), width="stretch")
    c3.button("📊 See all charts", on_click=go_to, args=("📊 Performance",), width="stretch")


# ---------------------------------------------------------------------------
# 📊 PERFORMANCE
# ---------------------------------------------------------------------------
def page_performance():
    header("📊 Performance", "How fast and how well is the lab working? Use the filters to focus.")
    df = load_tests()
    with st.expander("🔎 Filters", expanded=True):
        c1, c2, c3, c4 = st.columns(4)
        months = sorted(df["year_month"].unique())
        start, end = c1.select_slider("Months", options=months, value=(months[0], months[-1]),
                                      format_func=lambda m: pd.Period(m).strftime("%b %Y"))
        depts = c2.multiselect("Department", sorted(df["department_name"].unique()), placeholder="All")
        cats = c3.multiselect("Test type", sorted(df["category"].unique()), placeholder="All")
        prio = c4.multiselect("Urgency", ["STAT", "ROUTINE"], placeholder="All",
                              help="STAT = urgent, ROUTINE = normal")
    f = df[(df["year_month"] >= start) & (df["year_month"] <= end)]
    if depts:
        f = f[f["department_name"].isin(depts)]
    if cats:
        f = f[f["category"].isin(cats)]
    if prio:
        f = f[f["priority"].isin(prio)]
    if f.empty:
        st.info("No tests match these filters. Try choosing more months or fewer filters.")
        return

    k = analytics.compute_kpis(f)
    c = st.columns(5)
    c[0].metric("Tests", f"{k['Tests ordered']:,}")
    c[1].metric("Average time to result", f"{k['Average TAT (min)']} min", help="Sample received → result released")
    c[2].metric("Results on time", f"{k['% within TAT target']}%", help="Goal: 90%")
    c[3].metric("Samples rejected", f"{k['Sample rejection rate (%)']}%")
    c[4].metric("Revenue", f"${k['Revenue (USD)']:,.0f}", help="From the payer fee schedule (Excel)")

    resulted = f[f["resulted_at"].notna()].copy()
    resulted["month"] = pd.to_datetime(resulted["year_month"])
    c1, c2 = st.columns(2)
    monthly = (resulted.groupby("month")
               .agg(avg=("tat_min", "mean"), on_time=("tat_met", lambda s: 100 * s.astype(bool).mean()))
               .reset_index())
    fig = px.line(monthly, x="month", y="avg", markers=True, labels={"month": "", "avg": "Minutes"},
                  title="Average time to result")
    fig.update_traces(line=dict(color=PERIWINKLE, width=3))
    c1.plotly_chart(style_fig(fig), width="stretch", theme=None)
    how_to_read(c1, 'The average time to result for each month, for the tests you selected with the filters.',
                'Each dot is one month. Higher = slower. Hover over a dot to see the exact minutes.',
                'A flat, low line. Use the filters to see whether one department or test type is driving a rise.')
    fig = px.bar(monthly, x="month", y="on_time", labels={"month": "", "on_time": "% on time"},
                 title="Results on time (goal 90%)", color_discrete_sequence=[PERIWINKLE])
    fig.add_hline(y=90, line_dash="dash", line_color=ACT, annotation_text="goal")
    c2.plotly_chart(style_fig(fig), width="stretch", theme=None)
    how_to_read(c2, 'The percentage of results released within their time target, for each month.',
                'Each bar is one month. The red dashed line is the 90% goal.',
                'Bars at or above the red line. Bars below it mean too many results were late that month.')

    c1, c2 = st.columns(2)
    inst = (resulted.dropna(subset=["instrument_code"]).groupby(["month", "instrument_code"])["tat_min"]
            .mean().reset_index())
    fig = px.line(inst, x="month", y="tat_min", color="instrument_code",
                  labels={"month": "", "tat_min": "Minutes", "instrument_code": "Analyzer"},
                  title="Time to result by analyzer")
    c1.plotly_chart(style_fig(fig), width="stretch", theme=None)
    how_to_read(c1, 'The average time to result for each analyzer (machine), month by month.',
                'Each coloured line is one analyzer. Click a name in the list on the right to hide or show its line.',
                'Each line stays at its own usual level. A line that suddenly climbs (like CHEM-02 in Jul–Sep) means that analyzer had problems.')
    shift = (resulted.groupby("shift_name").agg(avg=("tat_min", "mean"), staff=("techs_present", "mean"))
             .reindex(["Day", "Evening", "Night"]).dropna(how="all").reset_index())
    fig = px.bar(shift, x="shift_name", y="avg", text=shift["staff"].round(1).astype(str) + " staff",
                 labels={"shift_name": "", "avg": "Minutes"}, title="Time to result and staff on each shift",
                 color_discrete_sequence=[PERIWINKLE])
    c2.plotly_chart(style_fig(fig), width="stretch", theme=None)
    how_to_read(c2, 'The average time to result on the Day, Evening and Night shifts. The label on each bar is the average number of staff working.',
                'Taller bar = slower shift. Compare the height with the number of staff on the bar.',
                'Similar heights for all shifts. A tall bar with few staff suggests the shift needs more people.')

    c1, c2 = st.columns(2)
    dept = (f.groupby("department_name")["sample_status"].apply(lambda s: 100 * (s == "REJECTED").mean())
            .reset_index(name="rejected").sort_values("rejected"))
    fig = px.bar(dept, x="rejected", y="department_name", orientation="h",
                 labels={"rejected": "% samples rejected", "department_name": ""},
                 title="Rejected samples by department", color_discrete_sequence=[PERIWINKLE])
    c1.plotly_chart(style_fig(fig), width="stretch", theme=None)
    how_to_read(c1, 'The percentage of samples from each department that the lab could not test (rejected).',
                'Longer bar = more rejected samples from that department.',
                'Short bars (5% or less). A long bar shows where training on sample collection or labeling could help.')
    f2 = f.assign(month=pd.to_datetime(f["year_month"]))
    rev = f2.groupby(["month", "insurance_type"])["reimbursement_usd"].sum().reset_index()
    fig = px.bar(rev, x="month", y="reimbursement_usd", color="insurance_type",
                 labels={"month": "", "reimbursement_usd": "US dollars", "insurance_type": "Insurance"},
                 title="Revenue by insurance type")
    c2.plotly_chart(style_fig(fig), width="stretch", theme=None)
    how_to_read(c2, "How much money the tests earned each month, split by the patient's insurance type.",
                'Each bar is one month; the colours show how much came from each insurance type. Hover for exact amounts.',
                'Steady bars. A drop may mean fewer tests were ordered or more samples were rejected.')


# ---------------------------------------------------------------------------
# 🔧 ANALYZERS  (Model C warnings + Model A delay risk)
# ---------------------------------------------------------------------------
def page_analyzers():
    header("🔧 Analyzers", "Which analyzers are behaving unusually, and which samples may be late?")
    tab1, tab2 = st.tabs(["⚠️ Analyzer warnings", "⏱️ How long will this sample take?"])

    with tab1:
        st.caption("LabLens learns each analyzer's normal week (errors, downtime, quality-control failures, "
                   "workload, time to result) and warns when a week looks very different. "
                   "Method: Isolation Forest (machine learning, no labels needed).")
        flagged = load_anomalies()
        ev = analytics.anomaly_evaluation(flagged)
        c = st.columns(3)
        c[0].metric("Weeks checked", ev["Instrument-weeks analysed"], help="Every analyzer × every week")
        c[1].metric("Warnings raised", ev["Weeks flagged as abnormal"])
        c[2].metric("Known CHEM-02 problem weeks caught",
                    f"{ev['Problem weeks found by the model']} of {ev['Known CHEM-02 problem weeks (Jul-Sep)']}",
                    help="Checked against the incident reports (Jul–Sep 2026)")
        chosen = st.multiselect("Analyzers", sorted(flagged["instrument_code"].unique()),
                                default=["CHEM-01", "CHEM-02"])
        names = {"downtime_min": "Downtime (minutes)", "error_events": "Errors",
                 "qc_failures": "Quality-control failures", "avg_tat_min": "Time to result (minutes)"}
        metric = st.radio("Show", list(names), horizontal=True, format_func=names.get)
        view = flagged[flagged["instrument_code"].isin(chosen)]
        if view.empty:
            st.info("Choose at least one analyzer.")
        else:
            fig = px.line(view, x="week", y=metric, color="instrument_code",
                          labels={"week": "", metric: names[metric], "instrument_code": "Analyzer"},
                          title=f"{names[metric]} per week  (⭕ = warning)")
            alerts = view[view["is_anomaly"]]
            fig.add_scatter(x=alerts["week"], y=alerts[metric], mode="markers", name="⚠️ Warning",
                            marker=dict(size=13, symbol="circle-open", color=ACT, line=dict(width=2.5)))
            st.plotly_chart(style_fig(fig, 380), width="stretch", theme=None)
            how_to_read(st, 'How each analyzer behaved week by week for the measure you chose (downtime, errors, QC failures or time to result).',
                        'Each line is one analyzer. Red circles ⭕ mark weeks the model found unusual for that analyzer.',
                        'Low, steady lines with no circles. Several circles in a row (like CHEM-02 in Jul–Sep) point to a real problem.')
        st.markdown("**All warnings (most unusual first)**")
        table = (flagged[flagged["is_anomaly"]].sort_values("anomaly_score", ascending=False)
                 [["instrument_code", "week", "error_events", "downtime_min", "qc_failures", "avg_tat_min",
                   "anomaly_score"]]
                 .rename(columns={"instrument_code": "Analyzer", "week": "Week of", "error_events": "Errors",
                                  "downtime_min": "Downtime (min)", "qc_failures": "QC failures",
                                  "avg_tat_min": "Time to result (min)", "anomaly_score": "How unusual"}))
        table["Week of"] = pd.to_datetime(table["Week of"]).dt.strftime("%d %b %Y")
        table = table.round(1)
        st.dataframe(table, width="stretch", hide_index=True)

    with tab2:
        st.caption("Enter what you know when a sample arrives. LabLens estimates how long the result will take "
                   "and the risk that it will miss its time target.")
        model = load_delay_model()
        if model is None:
            st.warning("The prediction model is missing (models/delay_model.joblib). Run the Module 5 notebook first.")
            return
        df = load_tests()
        c1, c2, c3 = st.columns(3)
        category = c1.selectbox("Test type", sorted(df["category"].unique()))
        inst_list = sorted(df[df["category"] == category]["instrument_code"].dropna().unique())
        instrument = c1.selectbox("Analyzer", inst_list)
        priority = c2.selectbox("Urgency", ["STAT", "ROUTINE"], help="STAT = urgent")
        shift = c2.selectbox("Shift", ["Day", "Evening", "Night"])
        day = c3.selectbox("Day", ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"])
        depts = df[["department_code", "department_name"]].drop_duplicates().sort_values("department_name")
        dept_name = c3.selectbox("Ordering department", depts["department_name"])
        dept = depts[depts["department_name"] == dept_name]["department_code"].iloc[0]
        c4, c5, c6 = st.columns(3)
        transport = c4.selectbox("How it arrived", ["Pneumatic tube", "Courier", "Hand delivery"])
        month = c5.selectbox("Month", list(range(1, 13)), index=7,
                             format_func=lambda m: pd.Timestamp(2026, m, 1).strftime("%B"))
        staffing = c6.slider("Staff on shift (% of planned)", 30, 100, 100)
        row = pd.DataFrame([{"category": category, "priority": priority, "instrument_code": instrument,
                             "shift_name": shift, "day_name": day, "department_code": dept,
                             "transport_method": transport, "month": str(month), "staffing_pct": staffing}])
        risk = float(model.predict_proba(row)[0, 1]) * 100
        level = "act" if risk >= 60 else ("watch" if risk >= 35 else "good")
        label = {"act": "HIGH risk - run this sample first or use a backup analyzer",
                 "watch": "MEDIUM risk - keep an eye on it", "good": "LOW risk"}[level]

        tm = load_time_model()
        typical = max(1.0, float(tm["typical"].predict(row)[0]))
        low = max(1.0, float(tm["low"].predict(row)[0]) - tm["widen"])
        high = max(typical, float(tm["high"].predict(row)[0]) + tm["widen"])
        same = df[(df["category"] == category) & (df["priority"] == priority)]["tat_target_min"]
        target = float(same.median()) if not same.dropna().empty else None
        t_status = "good" if target is None or typical <= 0.8 * target else ("watch" if typical <= target else "act")

        c1, c2, c3 = st.columns(3)
        card(c1, "Expected time to result", f"about {typical:.0f} min", t_status,
             "Typical time for samples with these details")
        c1.markdown(f"Most results like this take **{low:.0f}–{high:.0f} min**")
        card(c2, "Time target for this test", f"{target:.0f} min" if target else "-", "good",
             "Usual target for this test type and urgency")
        card(c3, "Risk of a late result", f"{risk:.0f}%", level, "Relative risk score for ranking samples")
        st.markdown(f"**{STATUS[level][0]} {label}**")
        with st.popover("ⓘ How is this calculated?"):
            st.markdown(
                f"**Expected time:** learned from **{tm['n_tests']:,} past tests** with similar details "
                f"(gradient boosting regression). On average the estimate is about **{tm['mae']:.0f} minutes off** "
                f"(a simple guess would be {tm['baseline_mae']:.0f} minutes off). The range is set so that about "
                f"**{tm['coverage']} in 100** real results fall inside it.  \n"
                "**Risk:** a Random Forest model ranks how likely a sample is to be late compared with others. "
                "It is for putting samples in order, not an exact chance.  \n"
                "Use both as a **guide, not a promise**.")


# ---------------------------------------------------------------------------
# 💬 ASK LABLENS  (numbers via Llama -> SQL, documents via RAG)
# ---------------------------------------------------------------------------
NUMBER_EXAMPLES = [
    "Which analyzer had the most downtime in quarter 3?",
    "Compare the average turnaround time for day, evening and night shifts",
    "Which department has the highest sample rejection rate?",
    "What is the monthly average turnaround time for CHEM-02?",
    "Which tests generated the most revenue?",
    "How many critical results were there for each test?",
]
DOC_EXAMPLES = [
    "What should staff do when a chemistry analyzer shows error E-417?",
    "What are the turnaround time targets for STAT tests?",
    "When must a specimen be rejected?",
    "What problems were reported with CHEM-02?",
]
VERIFIED_FALLBACK = {   # used only when the AI is off (fallback plan) - checked by hand
    "Downtime by analyzer in Jul–Sep": "SELECT instrument, SUM(downtime_minutes) AS downtime_min "
                                       "FROM ai_instrument_events WHERE quarter = 3 "
                                       "GROUP BY instrument ORDER BY downtime_min DESC",
    "Time to result by shift": "SELECT shift, ROUND(AVG(tat_minutes)::numeric, 1) AS avg_minutes "
                               "FROM ai_lab_tests WHERE tat_minutes IS NOT NULL GROUP BY shift ORDER BY avg_minutes DESC",
    "Rejected samples by department": "SELECT department, ROUND(100.0 * AVG(CASE WHEN sample_status = 'REJECTED' "
                                      "THEN 1 ELSE 0 END), 2) AS rejected_pct FROM ai_lab_tests "
                                      "GROUP BY department ORDER BY rejected_pct DESC",
    "Time to result by quarter": "SELECT quarter, ROUND(AVG(tat_minutes)::numeric, 1) AS avg_minutes "
                                 "FROM ai_lab_tests WHERE tat_minutes IS NOT NULL GROUP BY quarter ORDER BY quarter",
}


def example_buttons(examples, state_key, ncols):
    cols = st.columns(ncols)
    for i, q in enumerate(examples):
        cols[i % ncols].button(q, key=f"{state_key}_{i}", width="stretch",
                               on_click=lambda q=q: st.session_state.update({state_key: q}))


def ask_and_clear_buttons(label, state_key):
    """Ask/Search button + a ✖ Clear button that empties the question box."""
    b1, b2, _ = st.columns([1, 1, 6])
    asked = b1.button(label, type="primary", key=f"go_{state_key}", width="stretch")
    b2.button("✖ Clear", key=f"clear_{state_key}", width="stretch",
              on_click=lambda: st.session_state.update({state_key: ""}))
    return asked


def ask_numbers():
    import nl2sql
    st.caption("Type a question in plain English. LabLens writes a database query, checks that it is safe "
               "(read-only, approved tables only), runs it and explains the answer.")
    if not llm_ok:
        st.warning("The AI is off, so you can't type your own question right now. "
                   "Here are ready-made answers that don't need the AI.")
        choice = st.selectbox("Ready-made question", list(VERIFIED_FALLBACK))
        r = nl2sql.run_user_sql(VERIFIED_FALLBACK[choice], question=f"(fallback) {choice}")
        st.dataframe(r["data"], width="stretch", hide_index=True)
        with st.expander("Show the query"):
            st.code(VERIFIED_FALLBACK[choice], language="sql")
        return

    st.markdown("**Try one:**")
    example_buttons(NUMBER_EXAMPLES, "q_numbers", 3)
    question = st.text_input("Your question", key="q_numbers",
                             placeholder="e.g. Which analyzer had the most errors in August?")
    asked = ask_and_clear_buttons("Ask", "q_numbers")
    if not asked or not question:
        return
    if len(question.strip()) < 8:
        st.warning("Please type a full question, for example: “Average time to result by department”.")
        return
    with st.spinner("LabLens is thinking... this usually takes 10–30 seconds."):
        r = nl2sql.ask(question)

    if r["status"] == "BLOCKED":
        st.error(f"🛡️ For safety, this request was blocked and nothing was run. ({r['validation']})")
        if r["sql"]:
            with st.expander("Show the query the AI tried to write"):
                st.code(r["sql"], language="sql")
        return
    if r["status"] == "INVALID_QUESTION":
        st.warning(r["validation"])
        return
    if r["status"] == "SQL_ERROR":
        st.error("LabLens could not answer this one, even after trying to fix its own query. "
                 "Try asking in a different way, or be more specific (which months, which analyzer...).")
        with st.expander("Show the query it tried"):
            st.code(r["sql"] or "", language="sql")
        return

    st.markdown("#### Answer")
    st.markdown(f'<div class="ll-box">{r["explanation"]}</div>', unsafe_allow_html=True)
    if r["data"].empty:
        st.info("The query worked, but nothing matched. Try a wider time period.")
    else:
        st.dataframe(r["data"], width="stretch", hide_index=True)
        num_cols = r["data"].select_dtypes("number").columns
        if 1 < len(r["data"]) <= 40 and len(num_cols) and len(r["data"].columns) >= 2:
            fig = px.bar(r["data"], x=r["data"].columns[0], y=num_cols[-1], color_discrete_sequence=[PERIWINKLE])
            st.plotly_chart(style_fig(fig, 320), width="stretch", theme=None)
            how_to_read(st, 'The answer table above, drawn as bars so it is easier to compare.',
                        'Each bar is one row of the table. Longer bar = bigger number.',
                        'Always check the bars against the table and the explanation.')
        st.download_button("⬇️ Download as CSV", r["data"].to_csv(index=False), "lablens_answer.csv", "text/csv")
    with st.expander("🔍 How LabLens got this answer"):
        st.markdown(f"✅ **Safety check passed:** {r['validation']}  \n"
                    f"⏱️ {r.get('seconds', '?')} seconds · attempts: {r['attempts']}")
        st.code(r["sql"], language="sql")
    st.caption("The explanation is written by AI - please check it against the table.")


def ask_documents():
    import rag
    st.caption("Answers come only from the lab's approved policies, SOPs and incident reports, "
               "and always show where they came from.")
    st.markdown("**Try one:**")
    example_buttons(DOC_EXAMPLES, "q_docs", 2)
    question = st.text_input("Your question", key="q_docs")
    where = st.radio("Look in", ["Everything", "Policies & SOPs", "Incident reports"], horizontal=True)
    source = {"Everything": None, "Policies & SOPs": "lab_documents", "Incident reports": "incident_reports"}[where]
    asked = ask_and_clear_buttons("Search", "q_docs")
    if not asked or not question:
        return

    if not llm_ok:   # fallback: plain keyword search, no AI
        st.warning("The AI is off - showing a simple word search instead.")
        words = [w.strip("?.,") for w in question.split() if len(w) > 3][:5]
        if not words:
            st.info("Please type a longer question.")
            return
        hits = pg("SELECT title, section, content FROM ai.rag_chunks WHERE " +
                  " OR ".join(["content ILIKE %s"] * len(words)) + " LIMIT 5", [f"%{w}%" for w in words])
        if hits.empty:
            st.info("No document contains these words.")
        for _, h in hits.iterrows():
            with st.expander(f"📄 {h['title']} — {h['section']}"):
                st.write(h["content"])
        return

    with st.spinner("Reading the documents... this usually takes 10–30 seconds."):
        r = rag.answer(question, source=source)
    if r["status"] != "SUCCESS":
        st.info(f"📭 {r['answer']} Try different words, or check the Help page for what the documents cover.")
        return
    st.markdown("#### Answer")
    st.markdown(f'<div class="ll-box">{r["answer"]}</div>', unsafe_allow_html=True)
    st.markdown("#### Where this comes from")
    for i, s in enumerate(r["sources"], start=1):
        match = float(s["similarity"])
        strength = "strong" if match >= 0.6 else "fair"
        with st.expander(f"[{i}] {s['title']} — {s['section']}   ·  {strength} match ({match:.2f})"):
            st.write(s["content"])


def page_ask():
    header("💬 Ask LabLens", "Ask about our numbers or about our policies and SOPs, in plain English.")
    tab1, tab2 = st.tabs(["📈 About our numbers", "📄 About our policies & SOPs"])
    with tab1:
        ask_numbers()
    with tab2:
        ask_documents()


# ---------------------------------------------------------------------------
# 🧠 BRIEFING
# ---------------------------------------------------------------------------
def page_briefing():
    import insights
    header("🧠 Briefing", "A short explanation of what happened, for managers. "
                          "Facts come from the database; AI ideas are clearly marked.")
    question = st.text_input("Question for the briefing", "Why did laboratory turnaround time get worse last quarter?")
    if not llm_ok:
        st.warning("The AI is off. Open the Ollama app, press Refresh, and try again. "
                   "The Home page already shows the main facts without AI.")
        return
    if not st.button("Write the briefing", type="primary"):
        st.caption("Takes about 20–60 seconds.")
        return
    with st.spinner("Collecting facts, checking analyzers and reading reports..."):
        r = insights.generate_insight(question)
    st.markdown(r["briefing"])
    bad = [n for n in r["unsupported_numbers"] if "UNSUPPORTED" in n]
    if not r["unsupported_numbers"]:
        st.success("✅ Number check: every number above comes straight from the database.")
    elif bad:
        st.error("⚠️ Number check: some numbers written by the AI could not be found in the facts: " + ", ".join(bad))
    else:
        st.success("✅ Number check: the numbers written by the AI match the facts (" +
                   ", ".join(r["unsupported_numbers"]) + ").")
    with st.expander("📎 Evidence used"):
        for key, (name, df) in r["facts"].items():
            st.markdown(f"**[{key}] {name}**")
            st.dataframe(df, width="stretch", hide_index=True)
        st.markdown(f"**[M1] Analyzer warning model:** {r['model_note']}")
        for i, d in enumerate(r["documents"], start=1):
            st.markdown(f"**[D{i}] {d['title']} — {d['section']}**")
            st.caption(d["content"])
    st.caption(f"Written in {r['seconds']} s by a private AI model running on this computer.")


# ---------------------------------------------------------------------------
# 🛡️ DATA TRUST
# ---------------------------------------------------------------------------
DQ_RULES = {
    "DQ01": "Fix ID formats (spaces, lower-case codes)",
    "DQ02": "Remove duplicate records",
    "DQ03": "Turn text into numbers (“$19.41”, “22.5 C”)",
    "DQ04": "Make category spellings the same (“NIGHT”, “Nights” → Night)",
    "DQ05": "Make date formats the same",
    "DQ06": "Catch impossible values (negative staff, impossible temperatures)",
    "DQ07": "Check records match across systems",
    "DQ08": "Handle missing values",
}


def page_trust():
    header("🛡️ Data trust", "Where the data comes from, how it was cleaned and checked, and what the AI did.")
    runs = pg("""SELECT batch_id, run_type, status, started_at, finished_at, rows_extracted, rows_loaded,
                        dq_issues, message FROM etl.run_log ORDER BY batch_id DESC LIMIT 20""")
    if runs.empty:
        st.warning("No data has been loaded yet. Run: python src/etl_pipeline.py --full")
        return
    last = runs.iloc[0]
    checks = pg("SELECT passed FROM etl.reconciliation WHERE batch_id = %s", (int(last.batch_id),))
    passed, total = int(checks["passed"].sum()) if not checks.empty else 0, len(checks)
    c = st.columns(4)
    card(c[0], "Last update", f"{pd.to_datetime(last.finished_at):%d %b}", "good" if last.status == "SUCCESS" else "act",
         f"Update #{last.batch_id} ({last.run_type})")
    card(c[1], "Checks passed", f"{passed} of {total}", "good" if total and passed == total else "act",
         "Counts in the sources are compared with counts in the warehouse")
    card(c[2], "Problems found & fixed", f"{int(runs['dq_issues'].sum()):,}", "good",
         "Messy values corrected or set aside by the cleaning rules")
    total_tests = int(pg("SELECT COUNT(*) FROM dw.fact_lab_test").iloc[0, 0])
    card(c[3], "Tests in the warehouse", f"{total_tests:,}", "good",
         f"The last update ({last.run_type}) added or changed {int(last.rows_loaded):,} rows")
    st.markdown("**Sources:** MySQL (orders, samples, results) · MongoDB (sample tracking, analyzer logs, "
                "incidents, SOPs) · Excel (payer fees) · CSV (staff roster) → cleaned by Python → PostgreSQL warehouse")

    tabs = st.tabs(["✅ Data checks", "🧹 Cleaning rules", "🕒 Update history", "🤖 AI activity log"])
    with tabs[0]:
        rec = pg("""SELECT check_name AS "Check", source_count AS "In source", target_count AS "In warehouse",
                           CASE WHEN passed THEN '✅ Pass' ELSE '❌ Fail' END AS "Result", batch_id AS "Update #"
                      FROM etl.reconciliation ORDER BY checked_at DESC LIMIT 30""")
        st.dataframe(rec, width="stretch", hide_index=True)
    with tabs[1]:
        rules = pg("""SELECT rule_code, source_name, COUNT(*) AS issues FROM etl.dq_issue
                       GROUP BY rule_code, source_name ORDER BY rule_code""")
        if rules.empty:
            st.info("No problems were found in the data.")
        else:
            all_rules = [f"{code} · {text}" for code, text in DQ_RULES.items()]
            rules["rule"] = rules["rule_code"] + " · " + rules["rule_code"].map(DQ_RULES).fillna("")
            missing = [r for r in all_rules if r not in set(rules["rule"])]   # rules that found 0 problems
            if missing:
                rules = pd.concat([rules, pd.DataFrame({"rule": missing, "issues": 0,
                                                        "source_name": rules["source_name"].iloc[0]})])
            fig = px.bar(rules, y="rule", x="issues", color="source_name", orientation="h",
                         labels={"rule": "", "issues": "Problems fixed", "source_name": "Source"},
                         title="Problems found and fixed, by cleaning rule",
                         category_orders={"rule": all_rules})
            totals = rules.groupby("rule")["issues"].sum().reindex(all_rules).fillna(0)
            fig.add_scatter(x=totals.values, y=totals.index, mode="text", showlegend=False, hoverinfo="skip",
                            text=[f"  {int(v)}" + ("  ✅ nothing wrong found" if v == 0 else "") for v in totals.values],
                            textposition="middle right", textfont=dict(family=FONT, color=INK))
            fig.update_xaxes(range=[0, totals.max() * 1.35])
            st.plotly_chart(style_fig(fig, 420), width="stretch", theme=None)
            how_to_read(st, 'How many data problems each cleaning rule found and fixed during the data loads, and in which source.',
                        'Each bar is one rule (DQ01–DQ08); the colours show which source the problems came from. The number at the end is the total.',
                        'Problems being found and fixed is good - it means messy data was cleaned before reaching the reports. 0 means that check found nothing wrong.')
            st.markdown("**Latest examples**")
            st.dataframe(pg("""SELECT rule_code AS "Rule", source_name AS "Source", record_ref AS "Record",
                                      issue AS "Problem", action_taken AS "What we did"
                                 FROM etl.dq_issue ORDER BY issue_id DESC LIMIT 50"""),
                         width="stretch", hide_index=True)
    with tabs[2]:
        st.dataframe(runs.rename(columns={"batch_id": "Update #", "run_type": "Type", "status": "Status",
                                          "started_at": "Started", "finished_at": "Finished",
                                          "rows_extracted": "Rows read", "rows_loaded": "Rows loaded",
                                          "dq_issues": "Problems fixed", "message": "Notes"}),
                     width="stretch", hide_index=True)
        st.markdown("**Bookmarks** - where the next update will continue from (only new data is loaded)")
        marks = pg("SELECT source_name AS \"Source\", last_value AS \"Loaded up to\", "
                   "updated_at AS \"Updated\" FROM etl.watermark ORDER BY source_name")
        is_file = marks["Source"].str.startswith("file.")
        marks.loc[is_file, "Loaded up to"] = ("File fingerprint " + marks.loc[is_file, "Loaded up to"].str[:8] +
                                              "… (reloaded only if the file changes)")
        st.dataframe(marks, width="stretch", hide_index=True)
        st.caption("For databases, the bookmark is the date and time of the last record loaded. For files, it is a "
                   "fingerprint (checksum) of the file: if the file has not changed, it is skipped.")
        if st.button("▶️ Load new data now"):
            with st.spinner("Loading new data... (up to a minute)"):
                out = subprocess.run([sys.executable, str(PROJECT_ROOT / "src" / "etl_pipeline.py")],
                                     capture_output=True, text=True, cwd=PROJECT_ROOT, timeout=600)
            if out.returncode == 0:
                st.success("✅ Update finished. Press Refresh in the menu to see the new numbers.")
            else:
                st.error("The update failed and was rolled back - no data was changed.")
            with st.expander("Update log"):
                st.code((out.stdout + out.stderr)[-4000:])
            st.cache_data.clear()
    with tabs[3]:
        st.caption("Every question asked to the AI, the query it wrote, and whether it was allowed or blocked.")
        log = pg("""SELECT asked_at AS "When", feature AS "Feature", question AS "Question", status AS "Result",
                           validation AS "Safety check", generated_sql AS "Query", duration_sec AS "Seconds"
                      FROM ai.query_log ORDER BY log_id DESC LIMIT 100""")
        if log.empty:
            st.info("No AI questions have been asked yet.")
        else:
            c = st.columns(3)
            c[0].metric("Questions asked", len(log))
            c[1].metric("Blocked for safety", int((log["Result"] == "BLOCKED").sum()))
            c[2].metric("Average answer time", f"{pd.to_numeric(log['Seconds']).mean():.0f} s")
            st.dataframe(log, width="stretch", hide_index=True)


# ---------------------------------------------------------------------------
# ℹ️ HELP
# ---------------------------------------------------------------------------
GLOSSARY = pd.DataFrame([
    ("Time to result (TAT)", "Minutes from the lab receiving a sample to the result being released."),
    ("Results on time", "Share of results released within the time target for that test (goal 90%)."),
    ("STAT", "Urgent test - needs a fast result (for example from the Emergency Department)."),
    ("ROUTINE", "Normal, non-urgent test."),
    ("Rejected sample", "A sample the lab cannot test - e.g. clotted, wrong tube, mislabeled, too little blood."),
    ("Analyzer", "The machine that runs tests (e.g. CHEM-02 = chemistry analyzer number 2)."),
    ("Downtime", "Minutes an analyzer could not run tests."),
    ("QC (quality control)", "Daily check with known samples to prove an analyzer gives correct results."),
    ("Critical result", "A result so far from normal that the doctor must be called right away."),
    ("SOP", "Standard Operating Procedure - the lab's written step-by-step instructions."),
    ("Analyzer warning", "A week where an analyzer behaved very differently from its normal pattern."),
    ("Hypothesis", "A possible explanation suggested by the AI - not proven by the data."),
    ("Expected time to result", "LabLens's estimate of how long a sample will take, shown as a typical time and a range."),
    ("Risk score", "How likely a sample is to be late compared with other samples (for ordering work, not an exact chance)."),
    ("Match score", "How closely a document passage matches your question (0 to 1). Strong = 0.6 or more."),
    ("Cleaning rule (DQ01–DQ08)", "An automatic check that finds and fixes messy data (duplicates, wrong formats...) before it reaches the reports."),
    ("Data load (full / incremental)", "Copying data into LabLens. Full = everything again; incremental = only what is new since the last load."),
    ("Bookmark", "Where the last data load stopped, so the next load only picks up new records."),
    ("Data check", "Comparing the number of records in the source with the number in LabLens, to make sure nothing was lost."),
    ("Fallback mode", "How LabLens works when the AI is off: charts still work and questions use ready-made answers or a word search."),
], columns=["Word", "Meaning"])

COLOURS = pd.DataFrame([
    ("Average time to result (Home)", "Last month at or below average", "Up to 15% above average", "More than 15% above"),
    ("Expected time to result", "Well within the target", "Close to the target", "Over the target"),
    ("Results on time", "90% or more", "80% – 90%", "Below 80%"),
    ("Samples rejected", "5% or less", "5% – 8%", "More than 8%"),
    ("Analyzer warnings (last 4 weeks)", "None", "1 – 2", "3 or more"),
    ("Risk of a late result", "Below 35%", "35% – 60%", "60% or more"),
    ("Data checks", "All passed", "-", "Any check failed"),
], columns=["Measure", "🟢 Good", "🟠 Watch", "🔴 Needs action"])


def page_help():
    header("ℹ️ Help", "How LabLens works, and what the words mean.")
    st.markdown("#### How LabLens works")
    st.markdown(f"""
<div class="ll-box">
<b>1. Collect</b> - orders, samples and results (MySQL) · sample tracking, analyzer logs, incidents and SOPs (MongoDB)
· payer fees (Excel) · staff roster (CSV)<br>
<b>2. Clean & check</b> - Python fixes messy values with 8 cleaning rules and checks that nothing was lost<br>
<b>3. Store</b> - everything goes into one reporting database (PostgreSQL)<br>
<b>4. Learn</b> - three machine-learning models: analyzer warnings, late-result risk and expected time to result<br>
<b>5. Ask</b> - a private AI (Llama, running on this computer) answers questions, always showing its query or its sources
</div>""", unsafe_allow_html=True)
    st.markdown("#### What each page is for")
    st.markdown("""
- **🏠 Home** - the lab's situation in 15 seconds.
- **📊 Performance** - all charts, with filters.
- **🔧 Analyzers** - analyzer warnings, and how long a sample will take (and its risk of being late).
- **💬 Ask LabLens** - ask about numbers, or about policies and SOPs.
- **🧠 Briefing** - an AI-written explanation for managers, with every number checked.
- **🛡️ Data trust** - how the data was cleaned and checked, and a log of everything the AI did.
""")
    st.markdown("#### What the colours mean")
    st.caption("The same three colours are used everywhere in LabLens, always together with a word.")
    st.dataframe(COLOURS, width="stretch", hide_index=True, height=35 * (len(COLOURS) + 1) + 3)
    st.markdown("#### Meaning of words")
    st.dataframe(GLOSSARY, width="stretch", hide_index=True, height=35 * (len(GLOSSARY) + 1) + 3)
    st.markdown("#### Good to know")
    st.markdown("""
- LabLens is for **lab and hospital staff**. It holds **no patient names** (de-identified data).
- The AI **cannot change any data**: it only reads approved tables, with a 5-second limit and at most 200 rows.
- If the AI is off (or the **Use AI** switch in the menu is off), charts and numbers still work, and **Ask LabLens** offers ready-made answers.
- AI answers can be wrong - always check them against the table or the source shown underneath.
""")


# ---------------------------------------------------------------------------
PAGES = {"🏠 Home": page_home, "📊 Performance": page_performance, "🔧 Analyzers": page_analyzers,
         "💬 Ask LabLens": page_ask, "🧠 Briefing": page_briefing, "🛡️ Data trust": page_trust,
         "ℹ️ Help": page_help}
try:
    PAGES[page]()
except Exception as exc:   # one broken page never crashes the whole app
    friendly_error(page, exc)
