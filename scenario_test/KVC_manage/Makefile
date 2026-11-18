ENV_FILE ?= env/env.sh

env:
	@test -f "$(ENV_FILE)" || cp env/profiles/3.2.sh "$(ENV_FILE)"
	@echo "environment: $(ENV_FILE)"

data:
	python3 data/gen_lpstd.py --out data/generated/lpstd_large_v16_a4x16.jsonl

preflight:
	bash scripts/00_preflight.sh

containers:
	bash scripts/01_create_containers.sh

matrix:
	bash scripts/05_run_matrix.sh

analyze:
	@test -n "$(RESULT)" || (echo "usage: make analyze RESULT=results/<dir>" >&2; exit 2)
	python3 scripts/06_analyze.py "$(RESULT)"

verify:
	python3 scripts/07_verify.py
