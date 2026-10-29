#!/usr/bin/env bash
# 구성 B 를 GPU(Colab Pro+) 에서 재는 스크립트 (SCRUM-45). 합성 데이터만 — 실데이터(data/mine)·API 키는 올리지 않는다.
#
# Colab 셀에서:
#   !curl -fsSL https://raw.githubusercontent.com/weeast1521/overseas-tax-agent/main/scripts/colab_config_b.sh | bash -s -- qwen2.5:7b eval/holdout_f07.txt
#   (두 번째 인자 생략 시 eval/local_smoke.txt 13건. 모델 여러 개는 쉼표: qwen2.5:7b,llama3.1:8b,exaone3.5:7.8b)
# 끝나면 /content/results_B_colab.zip 을 내려받아 맥의 results/ 에 푼다 → python -m eval.compare results/A_t2sql6.jsonl results/B_colab_*.jsonl
#
# GPU 메모리: 7~8B q4 ≈ 5GB. T4(16GB) 로 충분, A100 이면 더 빠름. 코퍼스 색인(bge-m3)도 같은 Ollama 로 만든다 → 외부 호출 0.
set -euo pipefail
MODELS="${1:-qwen2.5:7b}"
ONLY="${2:-eval/local_smoke.txt}"
TIMEOUT="${3:-120}"

cd /content
if [ ! -d overseas-tax-agent ]; then git clone -q https://github.com/weeast1521/overseas-tax-agent.git; fi
cd overseas-tax-agent && git pull -q
pip install -q -r requirements.txt

# Ollama (GPU 자동 인식)
if ! command -v ollama >/dev/null; then curl -fsSL https://ollama.com/install.sh | sh >/dev/null; fi
(OLLAMA_HOST=0.0.0.0:11434 OLLAMA_KEEP_ALIVE=24h nohup ollama serve > /content/ollama.log 2>&1 &)
for i in $(seq 1 30); do curl -fs localhost:11434/api/tags >/dev/null && break; sleep 1; done
ollama pull bge-m3 >/dev/null
IFS=',' read -ra MS <<< "$MODELS"
for m in "${MS[@]}"; do ollama pull "$m" >/dev/null; done
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader || true

# 환경: 폐쇄망 구성. 키 없음, 실데이터 없음
export LLM_PROVIDER=ollama JUDGE_IMPL=local EMBEDDER=ollama RAG_STORE=local OLLAMA_NUM_CTX=16384 TRADES_FILE=data/synthetic/trades.json
unset ANTHROPIC_API_KEY || true
# 코퍼스: git 에 있는 법령·시행령 조각만 (국세청 책자·증권사 안내는 맥에만) → 색인
python scripts/index_corpus.py >/dev/null 2>&1 || python scripts/index_corpus.py

GPU=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 | tr ' ' '_' | tr -cd '[:alnum:]_' || echo gpu)
for m in "${MS[@]}"; do
  TAG="colab_${GPU}_$(echo "$m" | tr ':/' '__')_t${TIMEOUT}"
  OLLAMA_MODEL="$m" LOCAL_JUDGE_MODEL="$m" python -m eval.run --config B --tag "$TAG" --only "$ONLY" --timeout "$TIMEOUT"
  python -m eval.report "results/B_${TAG}.jsonl" | head -40 || true
done
python scripts/show_latency.py "$(ls -t results/trace_B_colab_*.db | head -1)" | tail -3 || true
cd /content && zip -qr results_B_colab.zip overseas-tax-agent/results && echo "→ /content/results_B_colab.zip (맥의 results/ 에 풀기)"
