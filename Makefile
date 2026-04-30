.PHONY: build up down restart logs run shell clean

COMPOSE = docker compose
SERVICE = scraper

## Build the scraper image
build:
	$(COMPOSE) build $(SERVICE)

## Start all containers in the background
up:
	$(COMPOSE) up -d $(SERVICE)

## Stop and remove containers (keeps volumes)
down:
	$(COMPOSE) down

## Restart the scraper container
restart:
	$(COMPOSE) restart $(SERVICE)

## Rebuild the image and recreate the container
rerun: build
	$(COMPOSE) up -d --force-recreate $(SERVICE)

## Follow scraper logs
logs:
	$(COMPOSE) logs -f $(SERVICE)

## Run a single scan and exit (does not start the interval loop)
run:
	$(COMPOSE) run --rm $(SERVICE)

## Open a shell inside the scraper container
shell:
	$(COMPOSE) exec $(SERVICE) /bin/bash

## Start scraper + Ollama sidecar
up-ollama:
	$(COMPOSE) --profile ollama up -d

## Stop everything including Ollama
down-ollama:
	$(COMPOSE) --profile ollama down

## Remove containers and all named volumes (destructive — clears seen-listings cache)
clean:
	$(COMPOSE) down -v
