@echo off
cd /d "%~dp0"
start "" "http://127.0.0.1:8000/"
start "" "http://127.0.0.1:8000/docs"
.venv\Scripts\uvicorn.exe backend.main:app --host 127.0.0.1 --port 8000 --reload
