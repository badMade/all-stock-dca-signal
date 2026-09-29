#!/bin/bash
set -e

echo "Setting up virtual environment with uv..."
uv venv .venv
source .venv/bin/activate

echo "Installing dev dependencies..."
uv pip install -r requirements-dev.txt

echo "Installing core project as editable..."
uv pip install -e .

echo "Setup complete."
