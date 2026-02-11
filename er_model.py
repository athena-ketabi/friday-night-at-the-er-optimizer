from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Tuple

import pulp


DEPARTMENTS = ("ED", "SD", "CC", "SU")
DEPT_NAME = {
    "ED": "Emergency Department",
    "SD": "Step Down",
    "CC": "Critical Care",
    "SU": "Surgery",
}

TOTAL_ROOMS = {
    "ED": 25,
    "SD": 30,
    "CC": 18,
    "SU": 9,
}

# State 0 (hardcoded from your scenario)
STATE0_PATIENTS = {
    "ED": 16,
    "SD": 22,
    "CC": 12,
    "SU": 4,
}

# Staff physically present in department at start (occupied + staffed-empty rooms)
STATE0_STAFF = {
    "ED": 18,
    "SD": 24,
    "CC": 13,
    "SU": 6,
}

FINANCIAL = {
    "ED": {"diversion": 5000, "waiting": 150, "extra_staff": 40},
    "SD": {"arrivals_waiting": 3750, "extra_staff": 40},
    "CC": {"arrivals_waiting": 3750, "extra_staff": 40},
    "SU": {"arrivals_waiting": 3750, "extra_staff": 40},
}

QUALITY = {
    "ED": {"diversion": 200, "waiting": 20, "extra_staff": 5},
    "SD": {"arrivals_waiting": 20, "requests_waiting": 20, "extra_staff": 5},
    "CC": {"arrivals_waiting": 20, "requests_waiting": 20, "extra_staff": 5},
    "SU": {"arrivals_waiting": 20, "requests_waiting": 20, "extra_staff": 5},
}


@dataclass
class DepartmentState:
    patients: int
    staff: int
    ext_waiting: int = 0  # queue of external arrivals (SD/CC/SU)
    ed_walkin_waiting: int = 0  # ED-only queue of walk-ins
    ed_ambulance_waiting: int = 0  # ED-only queue of ambulance arrivals
    req_waiting_mature: int = 0  # can be admitted this hour
    req_waiting_new: int = 0  # arrived this hour; can be admitted next hour


@dataclass
class Totals:
    ed_diversions: int = 0
    ed_waiting: int = 0
    ed_extra_staff: int = 0
    dep_arrivals_waiting: Dict[str, int] = field(
        default_factory=lambda: {d: 0 for d in ("SD", "CC", "SU")}
    )
    dep_requests_waiting: Dict[str, int] = field(
        default_factory=lambda: {d: 0 for d in ("SD", "CC", "SU")}
    )
    dep_extra_staff: Dict[str, int] = field(
        default_factory=lambda: {d: 0 for d in ("SD", "CC", "SU")}
    )
    throughput_admitted: int = 0
    discharged_out: int = 0

    def financial_cost(self) -> int:
        ed = (
            self.ed_diversions * FINANCIAL["ED"]["diversion"]
            + self.ed_waiting * FINANCIAL["ED"]["waiting"]
            + self.ed_extra_staff * FINANCIAL["ED"]["extra_staff"]
        )
        others = 0
        for d in ("SD", "CC", "SU"):
            others += self.dep_arrivals_waiting[d] * FINANCIAL[d]["arrivals_waiting"]
            others += self.dep_extra_staff[d] * FINANCIAL[d]["extra_staff"]
        return ed + others

    def quality_penalty(self) -> int:
        ed = (
            self.ed_diversions * QUALITY["ED"]["diversion"]
            + self.ed_waiting * QUALITY["ED"]["waiting"]
            + self.ed_extra_staff * QUALITY["ED"]["extra_staff"]
        )
        others = 0
        for d in ("SD", "CC", "SU"):
            others += self.dep_arrivals_waiting[d] * QUALITY[d]["arrivals_waiting"]
            others += self.dep_requests_waiting[d] * QUALITY[d]["requests_waiting"]
            others += self.dep_extra_staff[d] * QUALITY[d]["extra_staff"]
        return ed + others


@dataclass
class GameState:
    hour: int = 1
    depts: Dict[str, DepartmentState] = field(
        default_factory=lambda: {
            d: DepartmentState(
                patients=STATE0_PATIENTS[d],
                staff=STATE0_STAFF[d],
                ext_waiting=0,
                req_waiting_mature=0,
                req_waiting_new=0,
            )
            for d in DEPARTMENTS
        }
    )
    totals: Totals = field(default_factory=Totals)


@dataclass
class HourInput:
    external_arrivals: Dict[str, int]  # SD/CC/SU external arrivals
    ed_walkin_arrivals: int
    ed_ambulance_arrivals: int
    staff_delta: Dict[str, int]
    ready_to_exit: Dict[str, int]
    destinations: Dict[
        Tuple[str, str], int
    ]  # (source, target) where target in DEPARTMENTS or "OUT"


def _clamp_non_negative(value: int) -> int:
    return max(0, int(value))


def _apply_departures_and_requests(
    state: GameState, hour_input: HourInput
) -> Dict[str, int]:
    discharged_out = {d: 0 for d in DEPARTMENTS}

    for d in DEPARTMENTS:
        requested = _clamp_non_negative(hour_input.ready_to_exit.get(d, 0))
        actual_departures = min(requested, state.depts[d].patients)
        state.depts[d].patients -= actual_departures

        # Validate destination total softly by clipping to actual departures.
        remaining = actual_departures
        out_count = min(
            _clamp_non_negative(hour_input.destinations.get((d, "OUT"), 0)), remaining
        )
        remaining -= out_count
        discharged_out[d] = out_count

        for t in DEPARTMENTS:
            if t == d or remaining <= 0:
                continue
            req = min(
                _clamp_non_negative(hour_input.destinations.get((d, t), 0)), remaining
            )
            state.depts[t].req_waiting_new += req
            remaining -= req

        # If destination entries under-specify the exits, spill as OUT.
        if remaining > 0:
            discharged_out[d] += remaining

    return discharged_out


def _roll_request_age(state: GameState) -> None:
    for d in DEPARTMENTS:
        dept = state.depts[d]
        dept.req_waiting_mature += dept.req_waiting_new
        dept.req_waiting_new = 0


def optimize_hour(
    state: GameState,
    hour_input: HourInput,
    quality_weight: float = 1.0,
    flow_reward: float = 300.0,
) -> Tuple[GameState, Dict[str, Dict[str, int]], Dict[str, float]]:
    """
    Apply one game hour and solve a one-hour MILP for best admissions/staffing choices.

    Objective:
      Min (financial + quality_weight * quality - flow_reward * throughput)
    """
    # 1) Existing fresh requests become mature at hour start
    _roll_request_age(state)

    # 2) Apply ready-to-exit and create new transfer requests (mature next hour)
    discharged_out_by_dept = _apply_departures_and_requests(state, hour_input)

    # 3) Add external arrivals and staff shocks
    for d in DEPARTMENTS:
        dept = state.depts[d]
        if d == "ED":
            dept.ed_walkin_waiting += _clamp_non_negative(hour_input.ed_walkin_arrivals)
            dept.ed_ambulance_waiting += _clamp_non_negative(
                hour_input.ed_ambulance_arrivals
            )
        else:
            dept.ext_waiting += _clamp_non_negative(
                hour_input.external_arrivals.get(d, 0)
            )
        dept.staff = _clamp_non_negative(
            dept.staff + int(hour_input.staff_delta.get(d, 0))
        )

    # 4) Optimization: admissions and extra staffing
    prob = pulp.LpProblem(name=f"fnater_hour_{state.hour}", sense=pulp.LpMinimize)

    admit_ext = {
        d: pulp.LpVariable(f"admit_ext_{d}", lowBound=0, cat="Integer")
        for d in DEPARTMENTS
    }
    admit_ed_walkin = pulp.LpVariable("admit_ed_walkin", lowBound=0, cat="Integer")
    admit_ed_ambulance = pulp.LpVariable(
        "admit_ed_ambulance", lowBound=0, cat="Integer"
    )
    admit_req = {
        d: pulp.LpVariable(f"admit_req_{d}", lowBound=0, cat="Integer")
        for d in DEPARTMENTS
    }
    extra_staff = {
        d: pulp.LpVariable(f"extra_staff_{d}", lowBound=0, cat="Integer")
        for d in DEPARTMENTS
    }
    # ED can divert only ambulance arrivals (walk-ins must be accepted)
    divert_ed = pulp.LpVariable("divert_ed", lowBound=0, cat="Integer")

    # Queue bounds
    for d in DEPARTMENTS:
        dept = state.depts[d]
        if d != "ED":
            prob += admit_ext[d] <= dept.ext_waiting, f"ext_admit_bound_{d}"
        else:
            prob += admit_ext[d] == 0, "ed_ext_disabled"
        prob += admit_req[d] <= dept.req_waiting_mature, f"req_admit_bound_{d}"

    prob += (
        admit_ed_walkin <= state.depts["ED"].ed_walkin_waiting,
        "ed_walkin_admit_bound",
    )
    prob += (
        admit_ed_ambulance <= state.depts["ED"].ed_ambulance_waiting,
        "ed_ambulance_admit_bound",
    )
    prob += divert_ed <= state.depts["ED"].ed_ambulance_waiting, "ed_divert_bound"
    prob += (
        admit_ed_ambulance + divert_ed <= state.depts["ED"].ed_ambulance_waiting,
        "ed_arrival_split",
    )

    # Capacity and staffing constraints
    for d in DEPARTMENTS:
        dept = state.depts[d]
        ext_admit_term = (
            admit_ed_walkin + admit_ed_ambulance if d == "ED" else admit_ext[d]
        )
        patients_end = dept.patients + ext_admit_term + admit_req[d]
        prob += patients_end <= TOTAL_ROOMS[d], f"room_capacity_{d}"
        prob += patients_end <= dept.staff + extra_staff[d], f"staff_capacity_{d}"

    # End-of-hour waiting expressions
    ext_wait_end = {
        d: state.depts[d].ext_waiting - admit_ext[d] for d in ("SD", "CC", "SU")
    }
    ed_walkin_wait_end = state.depts["ED"].ed_walkin_waiting - admit_ed_walkin
    ed_ambulance_wait_end = (
        state.depts["ED"].ed_ambulance_waiting - admit_ed_ambulance - divert_ed
    )
    req_wait_end = {
        d: state.depts[d].req_waiting_mature
        - admit_req[d]
        + state.depts[d].req_waiting_new
        for d in DEPARTMENTS
    }

    # Objective pieces
    financial = (
        FINANCIAL["ED"]["diversion"] * divert_ed
        + FINANCIAL["ED"]["waiting"]
        * (ed_walkin_wait_end + ed_ambulance_wait_end + req_wait_end["ED"])
        + FINANCIAL["ED"]["extra_staff"] * extra_staff["ED"]
    )
    for d in ("SD", "CC", "SU"):
        financial += FINANCIAL[d]["arrivals_waiting"] * ext_wait_end[d]
        financial += FINANCIAL[d]["extra_staff"] * extra_staff[d]

    quality = (
        QUALITY["ED"]["diversion"] * divert_ed
        + QUALITY["ED"]["waiting"]
        * (ed_walkin_wait_end + ed_ambulance_wait_end + req_wait_end["ED"])
        + QUALITY["ED"]["extra_staff"] * extra_staff["ED"]
    )
    for d in ("SD", "CC", "SU"):
        quality += QUALITY[d]["arrivals_waiting"] * ext_wait_end[d]
        quality += QUALITY[d]["requests_waiting"] * req_wait_end[d]
        quality += QUALITY[d]["extra_staff"] * extra_staff[d]

    throughput = (
        admit_ed_walkin
        + admit_ed_ambulance
        + pulp.lpSum(admit_ext[d] for d in ("SD", "CC", "SU"))
        + pulp.lpSum(admit_req[d] for d in DEPARTMENTS)
    )
    prob += financial + quality_weight * quality - flow_reward * throughput

    status = prob.solve(pulp.PULP_CBC_CMD(msg=False))
    if status != pulp.LpStatusOptimal:
        raise RuntimeError(
            f"Optimization failed at hour {state.hour}: {pulp.LpStatus[status]}"
        )

    # 5) Apply optimized actions to state
    decisions: Dict[str, Dict[str, int]] = {}
    for d in DEPARTMENTS:
        ae = 0 if d == "ED" else int(round(pulp.value(admit_ext[d])))
        ar = int(round(pulp.value(admit_req[d])))
        es = int(round(pulp.value(extra_staff[d])))

        dept = state.depts[d]
        if d == "ED":
            ae_walkin = int(round(pulp.value(admit_ed_walkin)))
            ae_ambulance = int(round(pulp.value(admit_ed_ambulance)))
            ae = ae_walkin + ae_ambulance
            dept.patients = dept.patients + ae_walkin + ae_ambulance + ar
            dept.ed_walkin_waiting = dept.ed_walkin_waiting - ae_walkin
            dept.ed_ambulance_waiting = dept.ed_ambulance_waiting - ae_ambulance
        else:
            dept.patients = dept.patients + ae + ar
            dept.ext_waiting = dept.ext_waiting - ae
        dept.req_waiting_mature = dept.req_waiting_mature - ar
        dept.staff += es

        decisions[d] = {
            "admit_external": ae,
            "admit_requests": ar,
            "call_extra_staff": es,
        }
        if d == "ED":
            decisions[d]["admit_walkins"] = ae_walkin
            decisions[d]["admit_ambulance"] = ae_ambulance

    ed_div = int(round(pulp.value(divert_ed)))
    state.depts["ED"].ed_ambulance_waiting = max(
        0, state.depts["ED"].ed_ambulance_waiting - ed_div
    )
    decisions["ED"]["divert_ambulances"] = ed_div

    # 6) Update cumulative performance totals
    state.totals.ed_diversions += ed_div
    state.totals.ed_waiting += (
        state.depts["ED"].ed_walkin_waiting
        + state.depts["ED"].ed_ambulance_waiting
        + state.depts["ED"].req_waiting_mature
    )
    state.totals.ed_extra_staff += decisions["ED"]["call_extra_staff"]
    for d in ("SD", "CC", "SU"):
        state.totals.dep_arrivals_waiting[d] += state.depts[d].ext_waiting
        state.totals.dep_requests_waiting[d] += (
            state.depts[d].req_waiting_mature + state.depts[d].req_waiting_new
        )
        state.totals.dep_extra_staff[d] += decisions[d]["call_extra_staff"]

    admitted_total = sum(
        v["admit_external"] + v["admit_requests"] for v in decisions.values()
    )
    discharged_out_total = sum(discharged_out_by_dept.values())
    state.totals.throughput_admitted += admitted_total
    state.totals.discharged_out += discharged_out_total

    # 7) Move to next hour
    state.hour += 1

    objective_info = {
        "financial_total": float(state.totals.financial_cost()),
        "quality_total": float(state.totals.quality_penalty()),
        "admitted_this_hour": float(admitted_total),
        "discharged_out_this_hour": float(discharged_out_total),
        "objective_value": float(pulp.value(prob.objective)),
    }
    return state, decisions, objective_info
