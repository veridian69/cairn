.PHONY: build install clean clean-cache test tidy check integration license-check tooling-check

CACHE_DIR := $(CURDIR)/.cache
GOCACHE := $(CACHE_DIR)/go-build
GOMODCACHE := $(CACHE_DIR)/go-mod
GOFLAGS := -modcacherw
GO := env GOCACHE=$(GOCACHE) GOMODCACHE=$(GOMODCACHE) GOFLAGS=$(GOFLAGS) go
PYTHON ?= python3
PREFIX ?= $(HOME)/.local
# The default is the release version shared with Cairn's source distribution,
# so `make -C a2a build` and `a2a --version` agree with the package. A blind
# acceptance run found the documented path reporting `dev`; only an explicit
# VERSION= on the command line may override it.
ifndef VERSION
VERSION := $(shell $(PYTHON) -c 'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb"))["project"]["version"])' $(CURDIR)/../pyproject.toml)
endif
ifeq ($(strip $(VERSION)),)
$(error cannot read the release version from ../pyproject.toml; pass VERSION= explicitly)
endif
VERSION_PACKAGE := github.com/veridian69/cairn/a2a/cmd.version

$(CACHE_DIR):
	mkdir -p $(GOCACHE) $(GOMODCACHE)

build: $(CACHE_DIR)
	$(GO) build -trimpath -ldflags="-s -w -X $(VERSION_PACKAGE)=$(VERSION)" -o a2a .

install: build
	./scripts/install-user --binary "$(CURDIR)/a2a" --prefix "$(PREFIX)"

clean:
	rm -f a2a

clean-cache:
	sudo rm -rf $(CACHE_DIR)

test: $(CACHE_DIR)
	$(GO) test ./... -timeout 60s

tidy: $(CACHE_DIR)
	$(GO) mod tidy

check: $(CACHE_DIR) tooling-check license-check
	$(GO) vet ./...
	$(GO) test ./... -cover -timeout 60s
	$(GO) test -race ./... -timeout 120s

tooling-check:
	$(PYTHON) -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"'
	$(PYTHON) -m unittest discover -s integration -p 'test_*.py' -q
	$(PYTHON) -m py_compile scripts/install-user scripts/install-server scripts/garden-config scripts/garden-session

license-check: $(CACHE_DIR)
	$(PYTHON) ./scripts/check-licenses

# Explicit opt-in: only starts a disposable local Cairn using this test runtime.
GARDEN_CAIRN_PYTHON ?= $(abspath ../.venv/bin/python)
integration: $(CACHE_DIR) build
	test -x "$(GARDEN_CAIRN_PYTHON)"
	GARDEN_CAIRN_PYTHON="$(GARDEN_CAIRN_PYTHON)" $(GO) test -count=1 -race -timeout 60s ./internal/garden -run '^TestRealCairnGardenDelivery$$'
	GARDEN_TEST_BINARY="$(CURDIR)/a2a" $(GO) test -count=1 -race -timeout 60s ./internal/attention -run '^TestAttentionAdaptersEndToEndWithCentralGarden$$'
	cd .. && GARDEN_TEST_BINARY="$(CURDIR)/a2a" "$(GARDEN_CAIRN_PYTHON)" -m pytest tests/installer/test_garden_live.py -q -o addopts=''
