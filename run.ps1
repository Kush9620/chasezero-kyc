Set-Location $PSScriptRoot
Start-Process "http://127.0.0.1:8000/"
Start-Process "http://127.0.0.1:8000/docs"
& "$PSScriptRoot\.venv\Scripts\uvicorn.exe" backend.main:app --host 127.0.0.1 --port 8000 --reload
