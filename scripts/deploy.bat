@echo off
rem One-click full deploy. Forwards any extra arguments to deploy.ps1.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy.ps1" %*
