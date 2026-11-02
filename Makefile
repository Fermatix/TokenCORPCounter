PYTHON ?= python3
VENV := .venv
PY := $(VENV)/bin/python
out ?= token_report.xlsx

.PHONY: help setup count test

help:
	@echo "make setup                      create .venv and install dependencies"
	@echo "make count path=DIR [out=FILE]  count tokens for a folder or file (default out: $(out))"
	@echo "                                extra flags: args=\"--base64 --exclude '*.log'\""
	@echo "make test                       run the test suite"

$(PY):
	$(PYTHON) -m venv $(VENV)

$(VENV)/.installed: requirements.txt | $(PY)
	$(PY) -m pip install -q -r requirements.txt
	@touch $@

setup: $(VENV)/.installed

count: setup
	@test -n "$(path)" || { echo "usage: make count path=/folder/or/file [out=report.xlsx] [args=\"--base64\"]" >&2; exit 2; }
	$(PY) token_counter --path "$(path)" --out "$(out)" $(args)

test: setup
	$(PY) -m unittest discover -s tests -v
