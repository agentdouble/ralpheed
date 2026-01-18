#!/bin/bash
set -e

MAX_ITERATIONS=${1:-10}
SCRIPT_DIR="$(cd "$(dirname \
  "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(pwd -P)"

echo "🚀 Starting Ralph"

for i in $(seq 1 $MAX_ITERATIONS); do
  echo "═══ Iteration $i ═══"

  PROMPT_TEMPLATE="$(cat "$SCRIPT_DIR/prompt.md")"
  PRD_PATH="${RALPHEED_PRD_PATH:-$HOME/.ralpheed/prd/default/prd.json}"
  PROGRESS_PATH="${RALPHEED_PROGRESS_PATH:-$(dirname "$PRD_PATH")/progress.txt}"
  PROGRESS_DIR="$(dirname "$PROGRESS_PATH")"
  mkdir -p "$PROGRESS_DIR"
  touch "$PROGRESS_PATH"
  PROMPT="$(printf "%s" "$PROMPT_TEMPLATE" \
    | sed "s|{{CONTROL_ROOT}}|$SCRIPT_DIR|g" \
    | sed "s|{{WORKSPACE_ROOT}}|$WORKSPACE_ROOT|g" \
    | sed "s|{{PROGRESS_PATH}}|$PROGRESS_PATH|g" \
    | sed "s|{{PRD_PATH}}|$PRD_PATH|g")"

  OUTPUT=$(codex exec --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox -m gpt-5.2-codex \
    -c model_reasoning_effort="xhigh" \
    --add-dir "$SCRIPT_DIR" \
    "$PROMPT" 2>&1 \
    | tee /dev/stderr) || true
  
  if echo "$OUTPUT" | \
    grep -q "<promise>COMPLETE</promise>"
  then
    echo "✅ Done!"
    exit 0
  fi
  
  sleep 2
done

echo "⚠️ Max iterations reached"
exit 1
