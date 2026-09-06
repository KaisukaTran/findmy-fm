#!/bin/bash
source .venv/bin/activate
export PYTHONPATH=src
uvicorn findmy.api.main:app --host 0.0.0.0 --port 8000 --reload

