PYTHON  ?= python3
PREFIX  ?= /usr/local
DESTDIR ?=
REF     ?= main

.PHONY: help test lint check golden sync-catalog man install uninstall clean

help:
	@echo "make test           run the unit tests"
	@echo "make lint           compile-check the sources, shellcheck the generated scripts"
	@echo "make check          lint + test"
	@echo "make golden         regenerate the golden script fixtures"
	@echo "make sync-catalog   regenerate the plugin catalog (REF=$(REF))"
	@echo "make man            preview the man page"
	@echo "make install        install to PREFIX=$(PREFIX)"

test:
	$(PYTHON) -m unittest discover -s tests -t . -v

lint:
	$(PYTHON) -m compileall -q must_gather_sos tools tests
	@if command -v shellcheck >/dev/null 2>&1; then \
		shellcheck -s bash share/completion/must-gather-sos.bash; \
		for f in tests/data/golden/*.sh; do shellcheck -s bash "$$f" || exit 1; done; \
		echo "shellcheck: clean"; \
	else \
		echo "shellcheck not installed, skipping"; \
	fi
	@for f in tests/data/golden/*.sh; do bash -n "$$f" || exit 1; done
	@echo "bash -n: clean"

check: lint test

golden:
	MGS_UPDATE_GOLDEN=1 $(PYTHON) -m unittest discover -s tests -t .
	@git --no-pager diff --stat -- tests/data/golden || true

sync-catalog:
	$(PYTHON) tools/sync_catalog.py --ref $(REF)
	@git --no-pager diff --stat -- must_gather_sos/data || true

man:
	man -l share/man/must-gather-sos.1

install:
	$(PYTHON) -m pip install --prefix=$(PREFIX) --root=$(DESTDIR)/ .
	install -Dm644 share/man/must-gather-sos.1 \
		$(DESTDIR)$(PREFIX)/share/man/man1/must-gather-sos.1
	install -Dm644 share/completion/must-gather-sos.bash \
		$(DESTDIR)$(PREFIX)/share/bash-completion/completions/must-gather-sos
	install -Dm644 share/completion/_must-gather-sos \
		$(DESTDIR)$(PREFIX)/share/zsh/site-functions/_must-gather-sos

uninstall:
	$(PYTHON) -m pip uninstall -y must-gather-sos
	rm -f $(DESTDIR)$(PREFIX)/share/man/man1/must-gather-sos.1
	rm -f $(DESTDIR)$(PREFIX)/share/bash-completion/completions/must-gather-sos
	rm -f $(DESTDIR)$(PREFIX)/share/zsh/site-functions/_must-gather-sos

clean:
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	rm -rf build dist ./*.egg-info
