#!/usr/bin/env bash
set -euo pipefail

root="$1"
mkdir -m 700 "$root" "$root/runtime"
git archive "$ALGORITHM_REVISION" -- '*.py' ':(exclude)test_*.py' catalogue_identifiers.json requirements.txt static workspace_web \
  | tar -x -C "$root/runtime"
cp n12_acceptance.py multiview_acceptance.py "$root/runtime/"
input="$root/runtime/test_artifacts/recognition"
mkdir -p "$input"
cp test_artifacts/recognition/{lotnexo-next-acceptance-plan.json,lotnexo-next12-blind.zip} "$input/"

if ! command -v tesseract >/dev/null || ! tesseract --list-langs | grep -Fx eng >/dev/null; then
  ubuntu_sources=/etc/apt/sources.list.d/ubuntu.sources
  test -r "$ubuntu_sources"
  apt_options=(-o "Dir::Etc::sourcelist=$ubuntu_sources" -o Dir::Etc::sourceparts=- -o Acquire::Retries=0)
  timeout 60s sudo apt-get "${apt_options[@]}" -o APT::Update::Error-Mode=any update -qq
  timeout 60s sudo apt-get "${apt_options[@]}" install -y -qq tesseract-ocr tesseract-ocr-eng
fi
tesseract --list-langs | grep -Fx eng >/dev/null

cd "$root"
python3 -m venv .venv
timeout 180s .venv/bin/python -m pip install --quiet -r runtime/requirements.txt
env -u PYTHONPATH .venv/bin/python runtime/n12_acceptance.py | tee prepare.json
