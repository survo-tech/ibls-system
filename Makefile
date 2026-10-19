# India Business Leaders Survey outreach system. `make help` lists targets.
TRACKER ?= data/IBLS_outreach_tracker.xlsx
EMAILS  ?=
PER_MAILBOX ?=
WHERE   ?= --remote

.PHONY: help install test test-py test-node typecheck e2e dev migrate-local migrate-remote import-local import-remote \
        seed-test seed-test-local send-local deploy smoke kobo-form hash-password fake-run

help:
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/'

install: ## npm + python dev dependencies
	npm ci
	pip install -r requirements-dev.txt

test: typecheck test-node test-py ## all offline tests

typecheck: ## TypeScript type check
	npx tsc --noEmit

test-node: ## scheduling unit tests
	node --test test/*.test.mjs

test-py: ## python job tests (mocked Gmail API/SMTP/IMAP/Kobo)
	python3 -m unittest discover -s tests -t .

e2e: ## full local run: fresh local D1 + import + wrangler dev + python jobs + HTTP suite
	scripts/local_e2e.sh

dev: ## wrangler dev with local D1 (uses .dev.vars)
	@[ -f .dev.vars ] || cp .dev.vars.example .dev.vars
	npx wrangler dev --local --test-scheduled

migrate-local: ## apply migrations to the local D1
	npx wrangler d1 migrations apply ibls --local

migrate-remote: ## apply migrations to the production D1
	npx wrangler d1 migrations apply ibls --remote

import-local: ## import all contacts into the local D1
	python3 scripts/import_contacts.py --tracker $(TRACKER) --apply --local

import-remote: ## import all contacts into the production D1
	python3 scripts/import_contacts.py --tracker $(TRACKER) --apply --remote

seed-test: ## seed/reset test contacts in production D1 (forces test_mode=true): make seed-test EMAILS=a@x,b@y [PER_MAILBOX=1]
	@[ -n "$(EMAILS)" ] || { echo "usage: make seed-test EMAILS=you@example.com,colleague@example.org [PER_MAILBOX=1]"; exit 1; }
	python3 scripts/seed_test.py --emails "$(EMAILS)" $(if $(PER_MAILBOX),--per-mailbox,) --apply $(WHERE)
	@echo "Now run the 'send' workflow manually (Actions > send > Run workflow) or 'make send-local'."

seed-test-local: ## same against the local D1
	$(MAKE) seed-test WHERE=--local EMAILS="$(EMAILS)" PER_MAILBOX="$(PER_MAILBOX)"

send-local: ## run the sender once from this machine (needs API_TOKEN and GOOGLE_SA_JSON in the environment)
	python3 jobs/send.py

deploy: ## migrations + deploy (normally done by GitHub Actions on push to main)
	npx wrangler d1 migrations apply ibls --remote
	npx wrangler deploy

smoke: ## post-deploy smoke test (BASE, API_TOKEN, DASH_USER, DASH_PASS optional)
	scripts/smoke_test.sh

kobo-form: ## rebuild the Kobo XLSForm with the hidden rid field (SRC=path to original xlsx)
	python3 kobo/build_kobo_form.py "$(SRC)" kobo/Self_india_business_leaders_survey_v2_rid.xlsx

hash-password: ## print a DASHBOARD_USERS entry: make hash-password USER=alice
	python3 scripts/hash_password.py $(USER)

fake-run: ## full fake-mail campaign with all contacts against a local Worker (nothing is sent)
	scripts/fake_full_run.sh all
