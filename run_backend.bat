@echo off
REM === Rebate 백엔드 실행 (포트 8800) ===
cd /d C:\rebate
if not exist logs mkdir logs
"C:\Users\PC68\miniconda3\Scripts\uv.exe" run uvicorn backend.main:app --host 0.0.0.0 --port 8800 > logs\backend.log 2>&1
