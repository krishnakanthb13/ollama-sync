@echo off
REM One `ollama run` per model that is available on the web as :cloud
REM but is not installed locally. Each line pulls the model if needed
REM and starts a chat session.
REM
REM Usage: run_cloud_models.bat
REM To only pull without starting a chat, change `run` to `pull`.

ollama run deepseek-v4-flash:0731-cloud
ollama run deepseek-v4-flash:preview-cloud
ollama run gemma4:cloud
ollama run kimi-k3:cloud

REM All done. Press any key to close...
pause
