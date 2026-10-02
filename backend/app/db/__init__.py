"""Database access layer.

Will hold the SQLAlchemy async engine, session factory and migration runner.
Aegis targets PostgreSQL, but nothing connects to it yet, so the dependency is
not installed and the service starts without a database.
"""
