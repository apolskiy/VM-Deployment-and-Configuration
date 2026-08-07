@echo off
rem One-click teardown. Destroys the cluster and updates the manifest.
rem Pass -Force to skip the confirmation prompt.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0teardown.ps1" %*
