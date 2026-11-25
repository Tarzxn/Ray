#!/usr/bin/env bash
set -euo pipefail
umask 077

: "${CODESPACE_NAME:?Run this inside your GitHub Codespace.}"
command -v ollama >/dev/null || { echo 'Install the Ollama Linux runtime first.' >&2; exit 1; }
state=/workspaces/.beam-cloud
mkdir -p "$state"
export OLLAMA_HOST=127.0.0.1:11434
export OLLAMA_NO_CLOUD=1
export OLLAMA_MODELS=/workspaces/.ray-models
export OLLAMA_NUM_PARALLEL=1
url=http://127.0.0.1:11434

if ! curl -fsS --max-time 2 "$url/api/version" >/dev/null 2>&1; then
    nohup ollama serve </dev/null >>"$state/local-model.log" 2>&1 &
    for ((attempt=0; attempt<30; attempt++)); do
        if curl -fsS --max-time 2 "$url/api/version" >/dev/null 2>&1; then break; fi
        sleep 1
    done
fi

ollama pull qwen2.5-coder:3b
curl -fsS --max-time 180 "$url/api/chat" \
    -H 'Content-Type: application/json' \
    -d '{"model":"qwen2.5-coder:3b","messages":[{"role":"user","content":"Reply with OK."}],"stream":false,"options":{"num_predict":16,"num_ctx":8192}}' \
    | python3 -c 'import json, sys; response = json.load(sys.stdin); sys.exit(0 if response.get("done") and response.get("message", {}).get("content", "").strip() else "Model smoke test failed")'
touch "$state/local-inference"
echo 'Codespace inference is enabled. Run this script again after the Codespace restarts.'
