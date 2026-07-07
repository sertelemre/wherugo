# WherUGo — geliştirme komutları (CONTRACTS.md §8)
# Kullanım: make install && make demo  →  http://localhost:8000

VENV ?= .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
PORT ?= 8000
SPEED ?= 20
SIM_HOURS ?= 12

.PHONY: install test demo demo-stop clean

$(VENV)/bin/python:
	python3 -m venv $(VENV)
	$(PIP) install -q --upgrade pip

install: $(VENV)/bin/python
	$(PIP) install -q -e ./ai -e ./backend -e ./edge pytest httpx
	@echo "Kurulum tamam. 'make test' veya 'make demo' çalıştırabilirsiniz."

test: install
	$(PY) -m pytest ai/tests backend/tests edge/tests -q

# Demo: backend'i başlatır, simülatörü hızlandırılmış modda üstünden akıtır.
# Dashboard: http://localhost:$(PORT)  (Authorization: Bearer demo otomatik)
demo: install
	@rm -f wherugo.db wherugo-edge-spool.sqlite
	@echo "Backend :$(PORT) üzerinde başlatılıyor..."
	@$(VENV)/bin/uvicorn wherugo_backend.app:create_app --factory --port $(PORT) & echo $$! > .backend.pid
	@sleep 2
	@echo "Edge simülatörü başlıyor ($(SIM_HOURS) saatlik gün, $(SPEED)x hız)..."
	@$(VENV)/bin/wherugo-edge --config deploy/edge-demo.yaml --source simulate \
		--speed $(SPEED) --duration $$(( $(SIM_HOURS) * 3600 )) --seed 42 & echo $$! > .edge.pid
	@echo ""
	@echo "==> Dashboard: http://localhost:$(PORT)   (durdurmak için: make demo-stop)"

demo-stop:
	-@kill $$(cat .edge.pid 2>/dev/null) 2>/dev/null; rm -f .edge.pid
	-@kill $$(cat .backend.pid 2>/dev/null) 2>/dev/null; rm -f .backend.pid
	@echo "Demo durduruldu."

clean: demo-stop
	rm -rf $(VENV) wherugo.db wherugo-edge-spool.sqlite* .pytest_cache
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null; true
