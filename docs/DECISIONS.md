# Architecture Decision Log

## ADR-001: Use HVE as an engineering workflow, not a runtime dependency
Status: Accepted

Use HVE Core Research -> Plan -> Implement patterns with GitHub Copilot during development.
OPTIMA runtime must not depend on HVE.

## ADR-002: Repo-owned specifications are the source of truth
Status: Accepted

Keep product scope, architecture, and engineering instructions in version control so Copilot sessions inherit stable context.

## ADR-003: Python + FastAPI backend
Status: Accepted

Reason: rapid hackathon development, strong AI/Azure SDK ecosystem, testability.

## ADR-004: Streamlit demo UI
Status: Accepted for MVP

Reason: optimize development speed and judge-facing visualization.
A production web frontend is roadmap work.

## ADR-005: Deterministic/explainable planner first
Status: Accepted

Do not build RL/ML planning during the hackathon.
Use rules plus historical strategy statistics.

## ADR-006: Foundry Model Router is a comparator/candidate strategy
Status: Accepted

OPTIMA must differentiate at execution-plan level and quality verification, not claim model routing as novel.

## ADR-007: Azure-native target with local fakes
Status: Accepted

Core engine must run in tests without paid cloud calls.
Azure implementations plug into interfaces.

## ADR-008: Bicep + azd for infrastructure/deployment
Status: Accepted

Keep Azure deployment reproducible and Copilot-friendly.
