.PHONY: help test lint format check-format security build clean clean-all

# Variables
PYTHON := python3
PIP := $(PYTHON) -m pip
PYTEST := $(PYTHON) -m pytest
PACKAGE_NAME := kbackup

# Default target
help:
	@echo "Available targets:"
	@echo "  help          - Show this help message"
	@echo "  test          - Run tests"
	@echo "  lint          - Run linting checks"
	@echo "  format        - Format code with black"
	@echo "  check-format  - Check code formatting without changes"
	@echo "  security      - Run security checks with bandit"
	@echo "  build         - Build distribution packages"
	@echo "  clean         - Clean build artifacts"
	@echo "  clean-all     - Clean all generated files including venv"

# Run tests
test:
	$(PYTEST)

# Lint code
lint:
	@echo "Running flake8..."
	-$(PYTHON) -m flake8 $(PACKAGE_NAME) tests --max-line-length=120 --exclude=__pycache__,*.pyc
	@echo "Running pylint..."
	-$(PYTHON) -m pylint $(PACKAGE_NAME) --disable=C0111 || true

# Format code
format:
	$(PYTHON) -m black $(PACKAGE_NAME) tests

# Check code formatting
check-format:
	$(PYTHON) -m black --check $(PACKAGE_NAME) tests

# Security checks
security:
	$(PYTHON) -m bandit -r $(PACKAGE_NAME)

# Build distribution packages
build: clean
	$(PIP) install --upgrade build
	$(PYTHON) -m build

# Clean build artifacts
clean:
	rm -rf build/
	rm -rf dist/
	rm -rf *.egg-info
	rm -rf .pytest_cache/
	rm -rf .coverage
	rm -rf htmlcov/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name '*.pyc' -delete
	find . -type f -name '*.pyo' -delete
	find . -type f -name '*.egg' -delete

# Clean all including virtual environment
clean-all: clean
	rm -rf venv
