.PHONY: check snapshot install-local install-claude install-grok publish

check:
	./scripts/run_checks.sh

snapshot:
	python3 scripts/toolchain_snapshot.py . --execute

install-local:
	codex plugin marketplace add "$(CURDIR)"
	codex plugin add autonom@autonom

install-claude:
	./scripts/install_skills.sh claude

install-grok:
	./scripts/install_skills.sh grok

publish:
	./scripts/publish_private_repo.sh

cli-version:
	python3 scripts/autonom.py version
