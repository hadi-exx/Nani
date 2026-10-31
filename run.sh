#!/usr/bin/env bash
# Jalankan dashboard memakai venv lokal proyek.
cd "$(dirname "$0")" || exit 1
exec ./.venv/bin/python app.py "$@"
