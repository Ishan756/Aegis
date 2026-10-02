"""Cache and queue layer.

Will wrap Redis for ephemeral state: deployment status, job locks and the
checkpoint store backing long-running agent runs. Redis-compatible in design,
but no client is installed yet.
"""
