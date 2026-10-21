.PHONY: test run import mytax
test:
	python -m pytest -q
run:
	uvicorn app.main:app --reload --port 8000
# 사용: make import MIRAE=~/Downloads/mirae.csv NAMUH=~/Downloads/namuh.csv
import:
	python scripts/import_my_trades.py $(if $(MIRAE),--mirae $(MIRAE)) $(if $(NAMUH),--namuh $(NAMUH))
mytax:
	python scripts/my_tax.py --lots
