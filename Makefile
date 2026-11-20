.PHONY: infra down reset-db api test psql logs

infra:            ## start postgres, rabbitmq, mailpit
	docker compose up -d postgres rabbitmq mailpit
	docker compose run --rm db-upgrade

down:
	docker compose down

reset-db:         ## wipe the DB volume and re-apply db/migrations/*.sql
	docker compose down -v && $(MAKE) infra

api:              ## run the API locally with reload (needs `make infra`)
	cd backend && DEMO_MODE=1 uv run uvicorn alerting.api.app:app --reload --port 8000

test:
	cd backend && uv run pytest -q

psql:
	docker compose exec postgres psql -U alerting -d alerting

logs:
	docker compose logs -f --tail=100
