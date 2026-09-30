@echo off
rem Starts the DBS Reporting web chat on port 8000. Double-click to run, or use from Task Scheduler.
cd /d "%~dp0"
".venv\Scripts\python.exe" -m uvicorn dbs_reporting.web:app --host 0.0.0.0 --port 8000
