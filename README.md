# Friday Night at the ER Optimizer

Streamlit app that models the game as a connected hospital-flow optimization problem.

It starts from your fixed **State 0** and, each simulated hour, takes:
- ED walk-in arrivals, ED ambulance arrivals, and external arrivals for other departments,
- ready-to-exit counts and destinations,
- event-card staffing changes,

then solves a mixed-integer optimization to recommend:
- how many arrivals to admit,
- how many transfer requests to admit,
- how many extra staff to call,
- and (for Emergency) how many ambulance arrivals to divert.

ED arrival logic:
- Walk-ins cannot be diverted and are always accepted into ED flow (roomed or waiting).
- Ambulance arrivals can be diverted, with the configured diversion penalty.

## Model Summary

Departments:
- `ED` (Emergency Department)
- `SD` (Step Down)
- `CC` (Critical Care)
- `SU` (Surgery)

Objective per hour:

`minimize financial_cost + quality_weight * quality_penalty - flow_reward * admitted_patients`

subject to:
- room capacities,
- staffing coverage (`patients <= staff + extra_staff`),
- queue/admission bounds,
- ED diversion/admission split.

The app accumulates financial and quality totals across hours.

## State 0 (hardcoded)

- **ED**: 25 rooms, 16 patients, 18 staff
- **SD**: 30 rooms, 22 patients, 24 staff
- **CC**: 18 rooms, 12 patients, 13 staff
- **SU**: 9 rooms, 4 patients, 6 staff

## Run with uv

```bash
uv sync
uv run streamlit run app.py
```

## Files

- `app.py`: Streamlit UI and hourly workflow
- `er_model.py`: optimization model and game state transitions
- `pyproject.toml`: dependencies/config for `uv`
