@echo off
rem Controls the EvoMesh Windows service: status | start | stop | restart | logs
rem See scripts\evomesh-service.ps1.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\evomesh-service.ps1" %*
