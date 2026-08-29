@echo off
REM One `ollama run` per model that is available on the web as :cloud
REM but is not installed locally. Each line pulls the model if needed
REM and starts a chat session.
REM
REM Usage: run_cloud_models.bat
REM To only pull without starting a chat, change `run` to `pull`.

ollama run deepseek-v4-pro:0813-cloud
ollama run glm-5.3-flash:cloud
ollama run glm-5.3:cloud

REM All done. Press any key to close...
pause
