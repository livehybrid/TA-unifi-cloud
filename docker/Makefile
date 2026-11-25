# Local Splunk-in-Docker test harness for TA-unifi-cloud.
# Requires Docker + docker compose (+ python with pytest for `make test`).
#
#   make build   # ucc-gen build -> ../stage/TA-unifi-cloud
#   make up      # start Splunk 10 with the add-on mounted, wait for health
#   make test    # run the integration suite against the running container
#   make down    # stop + remove the container and its volumes
#   make all     # build + up + test + down
#
# The add-on is bind-mounted from ../stage, so re-run `make build` whenever the
# source under ../package changes.

COMPOSE   ?= docker compose
SERVICE    = splunk
CONTAINER  = ta_unifi_cloud_splunk
APP        = TA-unifi-cloud
REPO       = ..
STAGE      = $(REPO)/stage/$(APP)
TA_VERSION ?= 1.2.0

export SPLUNK_MGMT_URL   ?= https://127.0.0.1:8089
export SPLUNK_PASSWORD   ?= Changeme1!
export SPLUNK_CONTAINER  ?= $(CONTAINER)

.PHONY: build certs up down restart logs ps test unit all

build: ## Build the add-on into ../stage/TA-unifi-cloud
	cd $(REPO) && ucc-gen build --source package --ta-version $(TA_VERSION)
	rm -rf $(STAGE) && mkdir -p $(REPO)/stage
	cp -r $(REPO)/output/$(APP) $(STAGE)
	# Test container's splunk user (a different uid) must read the app and write
	# local/inputs.conf + checkpoints. Wide perms are fine on a throwaway copy.
	chmod -R 777 $(STAGE)
	$(MAKE) certs

certs: ## Throwaway self-signed certificate for the mock (CN/SAN mock)
	mkdir -p $(REPO)/stage/certs
	openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj /CN=mock \
	  -addext subjectAltName=DNS:mock,DNS:localhost \
	  -keyout $(REPO)/stage/certs/key.pem -out $(REPO)/stage/certs/cert.pem
	chmod 644 $(REPO)/stage/certs/*.pem

unit: ## Collector tests against the mock, no Splunk
	cd $(REPO) && pytest tests/unit -v

up: ## Start Splunk (detached) and block until healthy
	$(COMPOSE) up -d --wait --wait-timeout 360

down: ## Stop and remove Splunk + volumes
	$(COMPOSE) down -v

restart: down up ## Recreate the container

ps: ## Show container status
	$(COMPOSE) ps

logs: ## Tail Splunk logs
	$(COMPOSE) logs -f $(SERVICE)

test: ## Run the integration suite against the running container
	cd $(REPO) && pytest tests/ --ignore=tests/live --ignore=tests/unit -v -ra

all: build up test down ## Full cycle: build, start, test, tear down
