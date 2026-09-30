@echo off
chcp 65001 >nul
title 清理 NAS Safe 桌面助手旧版
powershell -ExecutionPolicy Bypass -NoProfile -WindowStyle Hidden -File "%~dp0cleanup_agent.ps1"
