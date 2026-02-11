from __future__ import annotations

import streamlit as st

from er_model import DEPARTMENTS, DEPT_NAME, GameState, HourInput, optimize_hour

DEPT_COLOR = {
    "ED": "#B22222",  # dark red
    "SD": "#1B3A6B",  # dark blue
    "CC": "#6A1B9A",  # purple
    "SU": "#00897B",  # turquoise
}
DEPT_BG = {
    "ED": "#FDEAEA",
    "SD": "#E3EAF5",
    "CC": "#F3E5F5",
    "SU": "#E0F2F1",
}


def _inject_css() -> None:
    st.markdown(
        """
        <style>
        /* Force light backgrounds */
        .stApp { background-color: #FFFFFF; }
        section[data-testid="stSidebar"] { background-color: #F5F5F5; }

        .dept-card {
            border-left: 5px solid;
            border-radius: 6px;
            padding: 12px 16px;
            margin-bottom: 10px;
        }
        .dept-card h4 { margin: 0 0 6px 0; }
        .dept-card ul { margin: 4px 0 0 18px; padding: 0; }
        .dept-card li { margin-bottom: 2px; }

        .dept-input-header {
            border-left: 5px solid;
            border-radius: 6px;
            padding: 8px 14px;
            margin-bottom: 4px;
        }
        .dept-input-header h4 { margin: 0; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _dept_card(dept_key: str, body_html: str) -> None:
    color = DEPT_COLOR[dept_key]
    bg = DEPT_BG[dept_key]
    st.markdown(
        f'<div class="dept-card" style="border-color:{color}; background:{bg};">'
        f'<h4 style="color:{color};">{DEPT_NAME[dept_key]}</h4>'
        f"{body_html}</div>",
        unsafe_allow_html=True,
    )


def _init_session() -> None:
    if "game_state" not in st.session_state:
        st.session_state.game_state = GameState()
    if "history" not in st.session_state:
        st.session_state.history = []


def _department_snapshot_table(state: GameState) -> list[dict]:
    rows = []
    for d in DEPARTMENTS:
        dept = state.depts[d]
        rows.append(
            {
                "Department": DEPT_NAME[d],
                "Patients": dept.patients,
                "Staff": dept.staff,
                "Arrivals waiting": dept.ext_waiting if d != "ED" else 0,
                "ED walk-ins waiting": dept.ed_walkin_waiting if d == "ED" else 0,
                "ED ambulance waiting": dept.ed_ambulance_waiting if d == "ED" else 0,
                "Requests (mature)": dept.req_waiting_mature,
                "Requests (new)": dept.req_waiting_new,
            }
        )
    return rows


def _dept_input_header(dept_key: str) -> None:
    color = DEPT_COLOR[dept_key]
    bg = DEPT_BG[dept_key]
    st.markdown(
        f'<div class="dept-input-header" style="border-color:{color}; background:{bg};">'
        f'<h4 style="color:{color};">{DEPT_NAME[dept_key]}</h4></div>',
        unsafe_allow_html=True,
    )


def _collect_hour_input() -> tuple[HourInput | None, list[str]]:
    errors: list[str] = []
    ext_arrivals: dict[str, int] = {}
    ed_walkin_arrivals = 0
    ed_ambulance_arrivals = 0
    staff_delta: dict[str, int] = {}
    ready: dict[str, int] = {}
    destinations: dict[tuple[str, str], int] = {}

    st.subheader("Hourly Inputs")
    st.caption(
        "Enter this hour's card data per department, then click Optimize."
    )

    for d in DEPARTMENTS:
        with st.container(border=True):
            color = DEPT_COLOR[d]
            _dept_input_header(d)

            # --- Arrivals & staff delta ---
            if d == "ED":
                cols = st.columns(3)
                ed_walkin_arrivals = cols[0].number_input(
                    "Walk-in arrivals",
                    min_value=0, value=0, step=1,
                    key="arr_ED_walkin",
                    help="Walk-ins cannot be diverted.",
                )
                ed_ambulance_arrivals = cols[1].number_input(
                    "Ambulance arrivals",
                    min_value=0, value=0, step=1,
                    key="arr_ED_ambulance",
                    help="Can be diverted ($5,000 penalty).",
                )
                staff_delta[d] = cols[2].number_input(
                    "Staff delta (event card)",
                    value=0, step=1,
                    key=f"staff_delta_{d}",
                    help="Negative = staff out, positive = staff return.",
                )
            else:
                cols = st.columns(2)
                ext_arrivals[d] = cols[0].number_input(
                    "New arrivals",
                    min_value=0, value=0, step=1,
                    key=f"arr_{d}",
                )
                staff_delta[d] = cols[1].number_input(
                    "Staff delta (event card)",
                    value=0, step=1,
                    key=f"staff_delta_{d}",
                    help="Negative = staff out, positive = staff return.",
                )

            # --- Ready to exit + destinations ---
            ready[d] = st.number_input(
                "Ready to exit",
                min_value=0, value=0, step=1,
                key=f"ready_{d}",
            )
            if ready[d] > 0:
                st.markdown(
                    f'<span style="color:{color}; font-size:0.85em;">'
                    f"Destination split (must sum to {ready[d]})</span>",
                    unsafe_allow_html=True,
                )
                dest_cols = st.columns(5)
                targets = ["OUT"] + [t for t in DEPARTMENTS if t != d]
                split_vals: dict[str, int] = {}
                for i, t in enumerate(targets):
                    label = "OUT (discharge)" if t == "OUT" else DEPT_NAME[t]
                    split_vals[t] = dest_cols[i].number_input(
                        label,
                        min_value=0, value=0, step=1,
                        key=f"dest_{d}_{t}",
                    )
                split_sum = sum(split_vals.values())
                if split_sum != ready[d]:
                    errors.append(
                        f"{DEPT_NAME[d]} destination split is {split_sum}, "
                        f"but ready-to-exit is {ready[d]}."
                    )
                for t, v in split_vals.items():
                    destinations[(d, t)] = int(v)

    if errors:
        return None, errors

    return (
        HourInput(
            external_arrivals={d: int(ext_arrivals.get(d, 0)) for d in DEPARTMENTS},
            ed_walkin_arrivals=int(ed_walkin_arrivals),
            ed_ambulance_arrivals=int(ed_ambulance_arrivals),
            staff_delta={d: int(staff_delta[d]) for d in DEPARTMENTS},
            ready_to_exit={d: int(ready[d]) for d in DEPARTMENTS},
            destinations=destinations,
        ),
        [],
    )


def _totals_panel(state: GameState) -> None:
    financial = state.totals.financial_cost()
    quality = state.totals.quality_penalty()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Cumulative cost", f"${financial:,.0f}")
    c2.metric("Quality penalty", f"{quality:,.0f}")
    c3.metric("Admitted", f"{state.totals.throughput_admitted}")
    c4.metric("Discharged", f"{state.totals.discharged_out}")


def _build_player_actions(state: GameState, decisions: dict) -> dict[str, list[str]]:
    actions: dict[str, list[str]] = {}
    for d in DEPARTMENTS:
        rec = decisions[d]
        dept = state.depts[d]
        if d == "ED":
            actions[d] = [
                f"Admit walk-ins: {rec.get('admit_walkins', 0)}",
                f"Admit ambulance arrivals: {rec.get('admit_ambulance', 0)}",
                f"Admit transfer requests: {rec.get('admit_requests', 0)}",
                f"Divert ambulances: {rec.get('divert_ambulances', 0)}",
                (
                    f"Call {rec.get('call_extra_staff', 0)} extra staff &mdash; "
                    f"hold {dept.ed_walkin_waiting} walk-ins, "
                    f"{dept.ed_ambulance_waiting} ambulance, "
                    f"{dept.req_waiting_mature + dept.req_waiting_new} requests"
                ),
            ]
        else:
            actions[d] = [
                f"Admit arrivals: {rec.get('admit_external', 0)}",
                f"Admit transfer requests: {rec.get('admit_requests', 0)}",
                f"Call extra staff: {rec.get('call_extra_staff', 0)}",
                f"Hold arrivals to next hour: {dept.ext_waiting}",
                f"Hold requests to next hour: {dept.req_waiting_mature + dept.req_waiting_new}",
            ]
    return actions


def _render_action_cards(
    player_actions: dict[str, list[str]],
) -> None:
    for d in DEPARTMENTS:
        items = "".join(f"<li>{line}</li>" for line in player_actions[d])
        _dept_card(d, f"<ul>{items}</ul>")


def main() -> None:
    st.set_page_config(
        page_title="Friday Night at the ER Optimizer",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    _inject_css()
    _init_session()

    st.title("Friday Night at the ER - Optimization Assistant")
    st.caption(
        "State 0 is hardcoded. Enter each hour's card data, then optimize "
        "for best actions across all departments."
    )

    state: GameState = st.session_state.game_state
    st.info(f"Current simulation hour: **{state.hour} / 24**")

    with st.sidebar:
        st.header("Objective weights")
        quality_weight = st.slider(
            "Quality penalty weight",
            min_value=0.0,
            max_value=5.0,
            value=1.0,
            step=0.1,
        )
        flow_reward = st.slider(
            "Flow reward (per patient admitted)",
            min_value=0.0,
            max_value=1000.0,
            value=300.0,
            step=25.0,
        )
        if st.button("Reset to State 0"):
            st.session_state.game_state = GameState()
            st.session_state.history = []
            st.rerun()

    # Current state
    st.subheader("Current Department State")
    st.dataframe(_department_snapshot_table(state), use_container_width=True)
    _totals_panel(state)

    # Hourly inputs
    hour_input, errors = _collect_hour_input()
    if errors:
        for err in errors:
            st.error(err)

    run = st.button("Optimize this hour", type="primary", disabled=hour_input is None)
    if run and hour_input is not None:
        try:
            new_state, decisions, objective_info = optimize_hour(
                state=state,
                hour_input=hour_input,
                quality_weight=quality_weight,
                flow_reward=flow_reward,
            )
        except Exception as exc:
            st.exception(exc)
            return

        st.session_state.game_state = new_state
        player_actions = _build_player_actions(new_state, decisions)
        st.session_state.history.append(
            {
                "hour_completed": new_state.hour - 1,
                "decisions": decisions,
                "player_actions": player_actions,
                "objective_info": objective_info,
            }
        )

        st.success("Optimization complete.")
        st.subheader("Player Action Card (Do This Now)")
        _render_action_cards(player_actions)

        c1, c2 = st.columns(2)
        c1.metric("Admitted this hour", int(objective_info["admitted_this_hour"]))
        c2.metric("Discharged this hour", int(objective_info["discharged_out_this_hour"]))
        st.rerun()

    # History
    st.subheader("Decision History")
    if not st.session_state.history:
        st.caption("No hours optimized yet.")
    else:
        for row in reversed(st.session_state.history):
            hour = row["hour_completed"]
            with st.expander(f"Hour {hour}", expanded=False):
                oi = row["objective_info"]
                c1, c2 = st.columns(2)
                c1.metric("Admitted", int(oi["admitted_this_hour"]))
                c2.metric("Discharged", int(oi["discharged_out_this_hour"]))
                _render_action_cards(row["player_actions"])


if __name__ == "__main__":
    main()
