.PHONY: help demo init sync live report serve changes snapshots history test doctor

help:
	@echo "make demo     - offline end-to-end walkthrough (no network) + dashboard shell"
	@echo "make init     - create the database and register the 15 firms"
	@echo "make sync     - refresh from fixtures and diff"
	@echo "make live     - refresh official sources and report any coverage failures"
	@echo "make report   - regenerate the dashboard shell + CSVs/JSON into out/"
	@echo "make serve    - build + serve the dashboard and JSON API on :8000"
	@echo "make changes  - show the recent change log"
	@echo "make snapshots - list ingestion snapshots"
	@echo "make history  - show the roster as of the latest snapshot"
	@echo "make test     - run the unit test suite"
	@echo "make doctor   - check environment and config"

demo:
	python3 -m pinpoint demo

init:
	python3 -m pinpoint init

sync:
	python3 -m pinpoint sync --fixture-set current

live:
	python3 -m pinpoint sync --live

report:
	python3 -m pinpoint report

serve:
	python3 -m pinpoint serve --build

changes:
	python3 -m pinpoint changes --limit 40

snapshots:
	python3 -m pinpoint snapshots

history:
	python3 -m pinpoint history

test:
	python3 -m unittest discover -s tests -t . -v

doctor:
	python3 -m pinpoint doctor
