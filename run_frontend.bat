@echo off
REM === Rebate 프론트엔드 실행 (Vite, 포트 5173) ===
cd /d C:\rebate\frontend
if not exist ..\logs mkdir ..\logs
call "C:\Program Files\nodejs\npm.cmd" run dev > ..\logs\frontend.log 2>&1
