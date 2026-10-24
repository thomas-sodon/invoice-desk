"""Repo-wide pytest config: registers the shared throwaway-Postgres fixtures."""

pytest_plugins = ["invoicedesk.testing.postgres"]
