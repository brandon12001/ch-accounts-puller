#!/usr/bin/env python3
"""
FX Prospecting Platform - Companies House triage.

Local web app over the v4 engine. Run with:
    streamlit run app.py

Three tabs:
  1. Single search  - confirm the right company, then triage it
  2. Bulk search     - paste names or upload a CSV, auto-match-and-skip, triage all
  3. Results         - everything run this session, searchable, export / append

Requires env vars: CH_API_KEY, ANTHROPIC_API_KEY (briefs off without it).
"""
import io
import time
import pandas as pd
import streamlit as st

import ch_engine as eng

st.set_page_config(page_title="FX Prospecting - CH Triage", layout="wide")

# ---- session state ----
if "results" not in st.session_state:
    st.session_state.results = []      # list of result dicts
if "candidates" not in st.session_state:
    st.session_state.candidates = []   # single-search shortlist awaiting confirm
if "pending_query" not in st.session_state:
    st.session_state.pending_query = ""

DISPLAY_COLS = ["grade", "score", "matched_name", "number", "sophistication",
                "overseas_parent", "one_liner", "fx_summary", "triggers", "angle",
                "red_flags", "turnover", "accounts_date", "match_reason", "error"]

GRADE_COLOR = {"A": "#00A86B", "B": "#7CB342", "C": "#F9A825", "D": "#BDBDBD"}


def add_result(res: dict):
    # de-dup on company number when present
    if res.get("number"):
        st.session_state.results = [
            r for r in st.session_state.results if r.get("number") != res["number"]
        ]
    st.session_state.results.append(res)


def keys_present():
    ok = bool(eng.API_KEY)
    brief = bool(eng.ANTHROPIC_KEY)
    return ok, brief


# ===========================================================================
# Header
# ===========================================================================
st.title("FX Prospecting — Companies House Triage")
ch_ok, brief_ok = keys_present()
c1, c2 = st.columns(2)
c1.metric("CH API key", "set" if ch_ok else "MISSING")
c2.metric("AI briefs", "on" if brief_ok else "off (no ANTHROPIC_API_KEY)")
if not ch_ok:
    st.error("Set CH_API_KEY in your environment before running searches.")
    st.stop()

tab_single, tab_bulk, tab_results = st.tabs(
    ["🔎 Single search", "📋 Bulk search", "📊 Results"]
)

# ===========================================================================
# TAB 1 — Single search (confirm then triage)
# ===========================================================================
with tab_single:
    st.subheader("Search one company")
    st.caption("Type a name or a company number. You confirm the right match before "
               "any accounts are pulled — no wrong-company guessing.")

    mode = st.radio("Search by", ["Name", "Company number"], horizontal=True)
    query = st.text_input("Company name or number", key="single_query")

    if st.button("Search", type="primary", disabled=not query):
        if mode == "Company number":
            with st.spinner("Pulling accounts and triaging…"):
                res = eng.process_company(number=query.strip(), do_brief=brief_ok)
            add_result(res)
            st.session_state.candidates = []
        else:
            with st.spinner("Finding candidates…"):
                cands, err = eng.search_candidates(query.strip(), limit=5)
            if err:
                st.error(err)
            elif not cands:
                st.warning("No Companies House match.")
            else:
                st.session_state.candidates = cands
                st.session_state.pending_query = query.strip()

    # Show shortlist to confirm
    if st.session_state.candidates:
        st.markdown("**Pick the right company:**")
        for i, c in enumerate(st.session_state.candidates):
            badge = {"exact": "🟢 exact", "strong": "🟢 strong",
                     "weak": "🟡 weak", "skip": "🔴 different"}[c["match_bucket"]]
            cols = st.columns([3, 1.2, 1, 2, 1.4])
            cols[0].markdown(f"**{c['title']}**  \n`{c['number']}` · {c['status']}")
            cols[1].markdown(badge)
            cols[2].markdown(c["incorporated"] or "—")
            cols[3].markdown(c["address"] or "—")
            if cols[4].button("Triage this", key=f"pick_{i}"):
                with st.spinner(f"Pulling accounts for {c['title']}…"):
                    res = eng.process_company(name=st.session_state.pending_query,
                                              preselected=c, do_brief=brief_ok)
                add_result(res)
                st.session_state.candidates = []
                st.rerun()
        st.caption("If none of these are right, refine the name and search again. "
                   "Nothing is pulled until you pick one.")

    # Show the most recent single result inline
    if st.session_state.results:
        last = st.session_state.results[-1]
        st.divider()
        g = last.get("grade", "")
        st.markdown(f"### {last.get('matched_name') or last.get('company')} "
                    f"<span style='background:{GRADE_COLOR.get(g,'#ccc')};color:white;"
                    f"padding:2px 10px;border-radius:6px;font-size:0.7em'>Grade {g}</span>",
                    unsafe_allow_html=True)
        if last.get("error"):
            st.warning(f"Note: {last['error']}")
        m = st.columns(4)
        m[0].metric("FX score", last.get("score", "—"))
        m[1].metric("Sophistication", last.get("sophistication", "—") or "—")
        m[2].metric("Overseas parent", last.get("overseas_parent", "—") or "—")
        m[3].metric("Turnover", last.get("turnover", "—") or "—")
        if last.get("one_liner"):
            st.markdown(f"**What they do:** {last['one_liner']}")
        if last.get("fx_summary"):
            st.markdown(f"**FX exposure:** {last['fx_summary']}")
        if last.get("triggers") and last["triggers"] != "none found":
            st.markdown(f"**Triggers:** {last['triggers']}")
        if last.get("angle"):
            st.success(f"**Opening angle:** {last['angle']}")
        if last.get("red_flags") and last["red_flags"] != "none":
            st.error(f"**Caution:** {last['red_flags']}")
        with st.expander("Regex findings + excerpts"):
            st.write(last.get("findings", ""))
            st.caption(last.get("excerpts", ""))

# ===========================================================================
# TAB 2 — Bulk search (auto match-and-skip)
# ===========================================================================
with tab_bulk:
    st.subheader("Bulk triage")
    st.caption("Paste one company name per line, or upload a CSV with a 'name' "
               "column (and optional 'number' column). Confident matches auto-run; "
               "anything that isn't clearly the right company is SKIPPED and flagged, "
               "not guessed.")

    up = st.file_uploader("CSV (optional)", type=["csv"])
    pasted = st.text_area("…or paste names, one per line", height=160)

    cola, colb = st.columns(2)
    allow_weak = cola.checkbox("Also accept weak matches (looser, riskier)", value=False)
    run_briefs = colb.checkbox("Generate AI briefs", value=brief_ok, disabled=not brief_ok)

    names, numbers = [], []
    if up is not None:
        df_in = pd.read_csv(up, dtype=str).fillna("")
        cols = {c.lower().strip(): c for c in df_in.columns}
        for _, row in df_in.iterrows():
            names.append(str(row.get(cols.get("name", ""), "")).strip())
            numbers.append(str(row.get(cols.get("number", ""), "")).strip())
    elif pasted.strip():
        names = [ln.strip() for ln in pasted.splitlines() if ln.strip()]
        numbers = [""] * len(names)

    st.write(f"**{len([n for n in names if n] + [x for x in numbers if x])} companies queued**"
             if (names or any(numbers)) else "")

    if st.button("Run bulk triage", type="primary",
                 disabled=not (names or any(numbers))):
        prog = st.progress(0.0)
        status = st.empty()
        skipped = []
        total = max(len(names), len(numbers))
        for i in range(total):
            nm = names[i] if i < len(names) else ""
            num = numbers[i] if i < len(numbers) else ""
            status.write(f"[{i+1}/{total}] {nm or num}")
            res = eng.process_company(name=nm, number=num, allow_weak=allow_weak,
                                      do_brief=run_briefs)
            add_result(res)
            if res.get("error", "").startswith("no confident match") or \
               res.get("error", "") == "no CH match":
                skipped.append(res)
            prog.progress((i + 1) / total)
            time.sleep(0.4)
        status.write("Done.")
        st.success(f"Processed {total}. "
                   f"{total - len(skipped)} matched, {len(skipped)} skipped for review.")
        if skipped:
            st.warning("Skipped (no confident match) — search these by hand in Single:")
            st.table(pd.DataFrame([{"name": s["company"],
                                    "reason": s.get("error", "")} for s in skipped]))

# ===========================================================================
# TAB 3 — Results
# ===========================================================================
with tab_results:
    st.subheader("Session results")
    if not st.session_state.results:
        st.info("No results yet. Run a single or bulk search.")
    else:
        df = pd.DataFrame(st.session_state.results)
        # filters
        f1, f2, f3 = st.columns(3)
        grades = f1.multiselect("Grade", ["A", "B", "C", "D"], default=["A", "B", "C"])
        soph = f2.multiselect("Sophistication",
                              sorted([s for s in df["sophistication"].unique() if s]),
                              default=[])
        hide_errors = f3.checkbox("Hide errored / skipped rows", value=True)

        view = df.copy()
        if grades:
            view = view[view["grade"].isin(grades)]
        if soph:
            view = view[view["sophistication"].isin(soph)]
        if hide_errors:
            view = view[view["error"] == ""]

        # sort: grade A->D then score desc
        grade_rank = {"A": 0, "B": 1, "C": 2, "D": 3, "": 4}
        view = view.assign(_g=view["grade"].map(grade_rank)) \
                   .sort_values(["_g", "score"], ascending=[True, False]) \
                   .drop(columns="_g")

        show = [c for c in DISPLAY_COLS if c in view.columns]
        st.dataframe(view[show], use_container_width=True, height=430)

        d1, d2 = st.columns(2)
        csv = view.to_csv(index=False).encode("utf-8-sig")
        d1.download_button("Download filtered CSV", csv,
                           file_name=f"call_sheet_{time.strftime('%d-%m-%Y_%H%M')}.csv",
                           mime="text/csv")
        full = df.to_csv(index=False).encode("utf-8-sig")
        d2.download_button("Download everything (all columns)", full,
                           file_name=f"call_sheet_full_{time.strftime('%d-%m-%Y_%H%M')}.csv",
                           mime="text/csv")

        st.caption(f"{len(view)} shown of {len(df)} total this session. "
                   "Fresh runs only — no stale re-run of an old commit.")
