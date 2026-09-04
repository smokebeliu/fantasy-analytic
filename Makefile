DOCKER_COMPOSE ?= docker compose

.PHONY: help setup build db migrate discover start test logs down reset

help:
	@printf '%s\n' \
		'make start     Start PostgreSQL, run migrations and discovery' \
		'make db        Start PostgreSQL only' \
		'make migrate   Apply database migrations to the latest revision' \
		'make discover  Run discovery against the latest completed season' \
		'make test      Run unit tests inside Docker' \
		'make logs      Follow PostgreSQL logs' \
		'make down      Stop local containers' \
		'make reset     Remove containers and PostgreSQL data'

setup:
	@test -f .env || cp .env.example .env
	@mkdir -p data

build:
	$(DOCKER_COMPOSE) build discovery

db: setup
	$(DOCKER_COMPOSE) up -d --wait postgres

migrate: db
	$(DOCKER_COMPOSE) run --rm --build migrate

discover: migrate
	$(DOCKER_COMPOSE) run --rm --build discovery

start: db migrate discover
	@printf 'PostgreSQL is available on '
	@$(DOCKER_COMPOSE) port postgres 5432
	@printf 'Discovery artifacts are available in ./data\n'

test:
	$(DOCKER_COMPOSE) run --rm --no-deps --build \
		--entrypoint python3 discovery -m unittest discover -s tests -v

logs:
	$(DOCKER_COMPOSE) logs -f postgres

down:
	$(DOCKER_COMPOSE) down

reset:
	$(DOCKER_COMPOSE) down --volumes --remove-orphans
