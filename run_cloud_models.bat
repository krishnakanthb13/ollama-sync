@echo off
REM One `ollama run` per model that is available on the web as :cloud
REM but is not installed locally. Each line pulls the model if needed
REM and starts a chat session.
REM
REM Usage: run_cloud_models.bat
REM To only pull without starting a chat, change `run` to `pull`.

ollama run beam:cloud
ollama run clef:27b-cloud
ollama run clef:cloud
ollama run deepseek-v4.1-flash:1017-cloud
ollama run deepseek-v4.1-flash:cloud
ollama run minimax-m3.1-flash:cloud
ollama run mistral-large-4:cloud
ollama run muse-spark-open:cloud

REM All done. Press any key to close...
pause
