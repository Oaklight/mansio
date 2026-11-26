# Makefile for mansio packages

# Variables
PACKAGE_NAME := mansio
CORE_DIR := packages/mansio-core
PACKAGE_DIR := packages/mansio-web
CLIENT_DIR := packages/mansio-client
DOCKER_IMAGE := oaklight/mansio
CORE_DIST_DIR := $(CORE_DIR)/dist
DIST_DIR := $(PACKAGE_DIR)/dist
CLIENT_DIST_DIR := $(CLIENT_DIR)/dist
VERSION := $(shell grep -oE '__version__[[:space:]]*=[[:space:]]*"[^"]+"' $(PACKAGE_DIR)/src/mansio_web/__init__.py | grep -oE '"[^"]+"' | tr -d '"' || echo "0.0.1")

# Optional variables
V ?= $(VERSION)
PYPI_MIRROR ?=
REGISTRY_MIRROR ?=

# Default target
all: format lint test

# ──────────────────────────────────────────────
# Formatting
# ──────────────────────────────────────────────

format:
	@echo "Running ruff check --fix..."
	ruff check --fix packages/
	@echo "Running ruff format..."
	ruff format packages/
	@echo "Format complete."

# ──────────────────────────────────────────────
# Linting & Type Checking
# ──────────────────────────────────────────────

lint:
	@echo "Running ruff check..."
	ruff check packages/
	@echo "Running ty check..."
	ty check
	@echo "Type check complete."

# ──────────────────────────────────────────────
# Testing
# ──────────────────────────────────────────────

test:
	@echo "Running core tests..."
	pytest $(CORE_DIR)/tests/ -v --tb=short
	@echo "Running web tests..."
	pytest $(PACKAGE_DIR)/tests/ -v --tb=short
	@echo "Running client tests..."
	pytest $(CLIENT_DIR)/tests/ -v --tb=short
	@echo "Tests completed."

# ──────────────────────────────────────────────
# Core package targets
# ──────────────────────────────────────────────

build-core: clean-core
	@echo "Building mansio-core version $(V)..."
	python -m build $(CORE_DIR)/
	@echo "Build complete. Distribution files are in $(CORE_DIST_DIR)/"

push-core:
	@echo "Pushing mansio-core to PyPI..."
	twine upload $(CORE_DIST_DIR)/*
	@echo "mansio-core pushed to PyPI."

clean-core:
	@echo "Cleaning up core build and distribution files..."
	rm -rf $(CORE_DIST_DIR) $(CORE_DIR)/src/*.egg-info
	@echo "Core cleanup complete."

# ──────────────────────────────────────────────
# Server package targets
# ──────────────────────────────────────────────

build-package: clean-package
	@echo "Building $(PACKAGE_NAME) version $(V)..."
	python -m build $(PACKAGE_DIR)/
	@echo "Build complete. Distribution files are in $(DIST_DIR)/"

push-package:
	@echo "Pushing $(PACKAGE_NAME) to PyPI..."
	twine upload $(DIST_DIR)/*
	@echo "Package pushed to PyPI."

clean-package:
	@echo "Cleaning up build and distribution files..."
	rm -rf $(DIST_DIR) $(PACKAGE_DIR)/src/*.egg-info
	@echo "Cleanup complete."

# Aliases
build: build-package
push: push-package
clean: clean-package

# ──────────────────────────────────────────────
# Client package targets
# ──────────────────────────────────────────────

build-client:
	@echo "Building mansio-client..."
	python -m build $(CLIENT_DIR)/
	@echo "Build complete. Distribution files are in $(CLIENT_DIST_DIR)/"

push-client:
	@echo "Pushing mansio-client to PyPI..."
	twine upload $(CLIENT_DIST_DIR)/*
	@echo "Client package pushed to PyPI."

clean-client:
	@echo "Cleaning up client build files..."
	rm -rf $(CLIENT_DIST_DIR) $(CLIENT_DIR)/src/*.egg-info
	@echo "Client cleanup complete."

# ──────────────────────────────────────────────
# Docker
# ──────────────────────────────────────────────

build-docker:
	@echo "Building Docker image $(DOCKER_IMAGE):$(V)..."
	@BUILD_ARGS=""; \
	if [ -n "$(REGISTRY_MIRROR)" ]; then \
		echo "Using registry mirror: $(REGISTRY_MIRROR)"; \
		BUILD_ARGS="$$BUILD_ARGS --build-arg REGISTRY_MIRROR=$(REGISTRY_MIRROR)"; \
	fi; \
	LOCAL_WHEEL=""; \
	if [ -d "$(DIST_DIR)" ] && [ -n "$$(ls -A $(DIST_DIR)/*$(V)*.whl 2>/dev/null)" ]; then \
		LOCAL_WHEEL=$$(ls $(DIST_DIR)/*$(V)*.whl | head -n 1 | xargs basename); \
		echo "Found local wheel: $$LOCAL_WHEEL"; \
		BUILD_ARGS="$$BUILD_ARGS --build-arg LOCAL_WHEEL=$$LOCAL_WHEEL"; \
		if [ -d "$(CORE_DIST_DIR)" ] && [ -n "$$(ls -A $(CORE_DIST_DIR)/*.whl 2>/dev/null)" ]; then \
			LOCAL_CORE_WHEEL=$$(ls $(CORE_DIST_DIR)/*.whl | head -n 1 | xargs basename); \
			echo "Found local core wheel: $$LOCAL_CORE_WHEEL"; \
			BUILD_ARGS="$$BUILD_ARGS --build-arg LOCAL_CORE_WHEEL=$$LOCAL_CORE_WHEEL"; \
		fi; \
	elif echo "$(V)" | grep -qE '^[0-9]+\.[0-9]+'; then \
		echo "Using version from PyPI: $(V)"; \
		BUILD_ARGS="$$BUILD_ARGS --build-arg PACKAGE_VERSION=$(V)"; \
	elif [ -d "$(DIST_DIR)" ] && [ -n "$$(ls -A $(DIST_DIR)/*.whl 2>/dev/null)" ]; then \
		LOCAL_WHEEL=$$(ls $(DIST_DIR)/*.whl | head -n 1 | xargs basename); \
		echo "Non-version tag '$(V)', using local wheel: $$LOCAL_WHEEL"; \
		BUILD_ARGS="$$BUILD_ARGS --build-arg LOCAL_WHEEL=$$LOCAL_WHEEL"; \
	else \
		echo "No local wheel found, will install latest from PyPI"; \
	fi; \
	if [ -n "$(PYPI_MIRROR)" ]; then \
		echo "Using PyPI mirror: $(PYPI_MIRROR)"; \
		BUILD_ARGS="$$BUILD_ARGS --build-arg PYPI_MIRROR=$(PYPI_MIRROR)"; \
	fi; \
	docker build -f docker/Dockerfile $$BUILD_ARGS -t $(DOCKER_IMAGE):$(V) -t $(DOCKER_IMAGE):latest .
	@echo "Docker image built successfully."

push-docker:
	@echo "Pushing Docker images $(DOCKER_IMAGE):$(V) and $(DOCKER_IMAGE):latest..."
	docker push $(DOCKER_IMAGE):$(V)
	docker push $(DOCKER_IMAGE):latest
	@echo "Docker images pushed successfully."

clean-docker:
	@echo "Cleaning Docker images..."
	docker rmi $(DOCKER_IMAGE):latest 2>/dev/null || true
	docker rmi $(DOCKER_IMAGE):$(V) 2>/dev/null || true

# ──────────────────────────────────────────────
# Help
# ──────────────────────────────────────────────

help:
	@echo "Available targets:"
	@echo ""
	@echo "Development:"
	@echo "  format         - Run ruff check --fix and ruff format"
	@echo "  lint           - Run ruff check and ty type checker"
	@echo "  test           - Run core, web and client tests with pytest"
	@echo ""
	@echo "Core (mansio-core):"
	@echo "  build-core     - Build the core package"
	@echo "  push-core      - Push mansio-core to PyPI"
	@echo "  clean-core     - Clean up core build files"
	@echo ""
	@echo "Server (mansio):"
	@echo "  build-package  - Build the server package"
	@echo "  push-package   - Push the server package to PyPI"
	@echo "  clean-package  - Clean up build and distribution files"
	@echo ""
	@echo "Client (mansio-client):"
	@echo "  build-client   - Build the client package"
	@echo "  push-client    - Push the client package to PyPI"
	@echo "  clean-client   - Clean up client build files"
	@echo ""
	@echo "Docker:"
	@echo "  build-docker   - Build Docker image (local x64)"
	@echo "  push-docker    - Push Docker image to registry"
	@echo "  clean-docker   - Clean Docker images"
	@echo ""
	@echo "Aliases:"
	@echo "  build          - Alias for build-package"
	@echo "  push           - Alias for push-package"
	@echo "  clean          - Alias for clean-package"
	@echo ""
	@echo "Composite targets:"
	@echo "  all            - Run format, lint, and test (default)"
	@echo ""
	@echo "Variables:"
	@echo "  V=<version|tag>          - Docker image tag (default: auto-detected from __init__.py)"
	@echo "  PYPI_MIRROR=<url>        - PyPI mirror URL"
	@echo "  REGISTRY_MIRROR=<host>   - Docker registry mirror"
	@echo ""
	@echo "Detected version: $(VERSION)"

.PHONY: all format lint test build-core push-core clean-core build-package push-package clean-package build push clean build-client push-client clean-client build-docker push-docker clean-docker help
