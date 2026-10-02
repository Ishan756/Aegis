"""Pydantic schemas and (later) persistence models.

``app/models`` holds the data contracts crossing module boundaries. Database
ORM models will live in :mod:`app.db` so the wire format can evolve without
touching the storage schema.
"""
