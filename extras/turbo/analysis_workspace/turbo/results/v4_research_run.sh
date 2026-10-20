#!/usr/bin/env bash
cd "/c/tradebot turbo"
export PYTHONUTF8=1
for w in resume cooldown universe params; do
  venv/Scripts/python turbo/v4_research.py $w --workers 5 > turbo/results/v4_research_$w.log 2>&1
  echo "$w done" >> turbo/results/v4_research_progress.log
done
