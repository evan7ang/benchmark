SHELL := /bin/bash
ROOT  := $(shell cd $(dir $(lastword $(MAKEFILE_LIST))) && pwd)
S     := $(ROOT)/scripts

.PHONY: help env preflight containers engine motor data run matrix analyze verify clean-results

help:
	@echo "cache_control 场景测试"
	@echo ""
	@echo "  首次:"
	@echo "    make env             # cp env/env.example.sh -> env/env.sh（或 cp env/profiles/<机>.sh）"
	@echo "    make preflight       # 镜像/模型/卡/数据/驱动/motor 代码版本自检"
	@echo "    make data            # 从冻结源生成 S1/S4/S8/S32（不入 git）"
	@echo "    make containers      # 幂等创建引擎/网关容器（已存在则跳过）"
	@echo "    make engine          # 起引擎并校验 banner；写 results/logs/pool.env"
	@echo ""
	@echo "  跑一轮 / 跑矩阵:"
	@echo "    make motor ARM=decl  # 起网关（decl|off|life）"
	@echo "    make run DS=contend_sp7_p60_S8.jsonl ARM=decl"
	@echo "    make matrix          # 按 config/scenario.json 的 matrix 跑"
	@echo ""
	@echo "  看结果:"
	@echo "    make analyze"
	@echo "    make verify"

env:
	@if [ -f $(ROOT)/env/env.sh ]; then echo "env/env.sh 已存在，未覆盖"; else cp $(ROOT)/env/env.example.sh $(ROOT)/env/env.sh && echo "已创建 env/env.sh（请按本机修改）"; fi

preflight:
	@bash $(S)/00_preflight.sh

containers:
	@bash $(S)/01_create_containers.sh

engine:
	@bash $(S)/02_start_engine.sh

motor:
	@bash $(S)/03_start_motor.sh $(ARM)

data:
	@python3 $(ROOT)/data/gen_sp7.py --sessions 1,4,8,32

run:
	@bash $(S)/04_run_one.sh $(DS) $(ARM) $(or $(REP),1) $(or $(LANE),A)

matrix:
	@bash $(S)/05_run_matrix.sh $(ARGS)

analyze:
	@python3 $(S)/06_analyze.py

verify:
	@python3 $(S)/07_verify.py

clean-results:
	@rm -rf $(ROOT)/results/* && touch $(ROOT)/results/.gitkeep && echo "results/ 已清空"
