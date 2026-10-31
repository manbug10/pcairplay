@echo off
rem Double-clickable doctor.ps1. -ExecutionPolicy Bypass -File is what keeps this
rem working on a repo downloaded as a ZIP, where Mark-of-the-Web would block it.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0doctor.ps1"
pause
