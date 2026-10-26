# ai-ops-agent 常用命令（Windows 下可用 git-bash / make，也可直接跑对应 python 命令）

PY ?= python
PORT ?= 8080

.PHONY: help install dev test demo lint build frontend compose-up compose-down clean

help:
	@echo "install      安装后端依赖（pip install -r requirements.txt）"
	@echo "dev          本地启动服务（mock 模式，端口 $(PORT)）"
	@echo "test         运行单元测试"
	@echo "demo         运行端到端验收自证脚本"
	@echo "build        构建前端静态产物"
	@echo "frontend     前端开发服务器"
	@echo "compose-up   docker compose 一键启动"
	@echo "compose-down 停止并移除容器"
	@echo "clean        清理运行时数据与构建产物"

install:
	$(PY) -m pip install -r requirements.txt

dev:
	MOCK_MODE=true PORT=$(PORT) $(PY) -m app.main

test:
	$(PY) -m pytest -q

demo:
	$(PY) scripts/demo.py

build:
	cd frontend && npm install && npm run build

frontend:
	cd frontend && npm run dev

compose-up:
	cd deploy && docker compose up -d --build

compose-down:
	cd deploy && docker compose down

clean:
	rm -rf data logs frontend/dist .pytest_cache
	find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
