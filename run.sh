#!/usr/bin/env bash
# 一键自检 + 确定性演练 + 生成报告。仅需 Python 3.11 标准库。
set -euo pipefail
cd "$(dirname "$0")"

echo "==> 运行单元测试"
python3 -m unittest discover -s tests -v

echo "==> 运行端到端演练（基准日 2026-10-20，全新状态）"
python3 -m procurement demo --fresh

echo "==> 完成。请查看 output/procurement_report.md 与 output/buyer_todo.json"
