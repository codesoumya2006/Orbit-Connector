import os
from pathlib import Path
import subprocess
import sys


def test_importing_application_does_not_create_database_schema():
    backend_dir = Path(__file__).resolve().parents[1]
    script = """
from sqlalchemy import MetaData

def reject_schema_creation(*args, **kwargs):
    raise AssertionError("Application startup must not create database schema.")

MetaData.create_all = reject_schema_creation
import app.main
"""
    environment = os.environ.copy()
    environment["DATABASE_URL"] = "sqlite://"

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=backend_dir,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
