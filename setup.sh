#!/bin/bash
set -e

# Change to the repository root directory
cd "$(dirname "$0")"

echo "Setting up virtual environment with uv..."
uv venv .venv
source .venv/bin/activate

echo "Installing dev dependencies..."
uv pip install -r requirements-dev.txt

echo "Installing core project as editable..."
uv pip install -e .

echo "Setup complete."
