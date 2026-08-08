@echo off
REM One `ollama run` per model that is available on the web as :cloud
REM but is not installed locally. Each line pulls the model if needed
REM and starts a chat session.
REM
REM Usage: run_cloud_models.bat
REM To only pull without starting a chat, change `run` to `pull`.

ollama run deepseek-v4-flash:0731-cloud
ollama run deepseek-v4-flash:cloud
ollama run deepseek-v4-flash:preview-cloud
ollama run deepseek-v4-pro:cloud
ollama run gemma4:31b-cloud
ollama run gemma4:cloud
ollama run glm-5.1:cloud
ollama run glm-5.2:cloud
ollama run gpt-oss:120b-cloud
ollama run gpt-oss:20b-cloud
ollama run kimi-k2.6:cloud
ollama run kimi-k2.7-code:cloud
ollama run kimi-k3:cloud
ollama run minimax-m2.7:cloud
ollama run minimax-m3:cloud
ollama run mistral-large-3:675b-cloud
ollama run nemotron-3-nano:30b-cloud
ollama run nemotron-3-super:cloud
ollama run nemotron-3-ultra:cloud
ollama run qwen3.5:397b-cloud
ollama run qwen3.5:cloud

REM All done. Press any key to close...
pause
