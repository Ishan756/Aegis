"""LangGraph workflows.

State graphs that chain agents and tools together, for example:

    analyze_repo -> build_plan -> approve -> deploy -> verify -> heal

Each graph owns a typed state object (see :mod:`app.models`) so transitions are
explicit and checkpointable. Populated in stage 5 (orchestration) and later.
"""
