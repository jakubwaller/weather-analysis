"""Smoke-test the Streamlit page itself against a seeded demo database.

AppTest executes dashboard/app.py in-process, so this catches wiring errors
(bad SQL, stale column names) that the pure-module tests cannot see.
"""

from pathlib import Path

from streamlit.testing.v1 import AppTest

from weather_analysis.demo import seed_demo_data

APP = str(Path(__file__).resolve().parent.parent / "dashboard" / "app.py")


def test_dashboard_renders_each_range(tmp_path, monkeypatch):
    db = tmp_path / "demo.db"
    seed_demo_data(db, days=10)
    monkeypatch.setenv("WEATHER_DB", str(db))

    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    assert at.title[0].value == "Weather analysis"
    assert at.metric  # KPI tiles rendered

    for choice in ("Last 24 hours", "All data", "Custom"):
        at.sidebar.radio[0].set_value(choice).run()
        assert not at.exception, choice


def test_dashboard_empty_database(tmp_path, monkeypatch):
    monkeypatch.setenv("WEATHER_DB", str(tmp_path / "empty.db"))
    # connect() creates the schema, so the file exists but has no rows
    from weather_analysis.db import connect

    connect(tmp_path / "empty.db").close()
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    assert "database is empty" in at.warning[0].value
