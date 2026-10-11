#!/bin/bash
# 建一个科研项目的骨架。
#
#   ./init-project.sh ~/Code/my-new-paper
#   ./init-project.sh .                     # 在当前目录建
#
# 已存在的文件不会被覆盖——在已有项目里跑也安全，只补缺的。

set -euo pipefail

DEST="${1:?用法: init-project.sh <项目目录>}"
TPL="$(cd "$(dirname "${BASH_SOURCE[0]}")/../assets/templates" && pwd)"

mkdir -p "$DEST"/{related_work,复现,experiment,DataSet}
cd "$DEST"

put() { # $1=模板名 $2=目标路径
  if [ -e "$2" ]; then
    echo "  ·  $2  已存在，跳过"
  else
    cp "$TPL/$1" "$2"
    echo "  +  $2"
  fi
}

put AGENTS.md      AGENTS.md
put handoff.md     handoff.md
put brainstorm.md  brainstorm.md
put dataset.md     dataset.md
put results.md     experiment/results.md
put evaluation.md  experiment/evaluation.md

# .gitignore：数据和大文件别进版本库
if [ ! -e .gitignore ]; then
  cat > .gitignore <<'EOF'
DataSet/
related_work/*/paper.pdf
related_work/*/source/
experiment/*/checkpoints/
experiment/*/*.pt
*.orig
.shot-tmp/
__pycache__/
EOF
  echo "  +  .gitignore"
fi

cat <<EOF

骨架建好了：$(pwd)

下一步按顺序：

  1. 填 AGENTS.md 的 {{占位符}} —— 尤其是「核心主张」那一条，
     那是整个项目的锚，Agent 靠它判断什么该做什么不该做

  2. 抓文献：
       bash $(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/fetch-paper.sh <arxiv-id> <目录名>

  3. 计算方法复现任务先验证关键 baseline，写 复现/<方法>/复现报告.md
     文献调研、机制分析或直接讨论假设，不以复现为前置条件

  4. 按当前研究问题在 brainstorm.md 记录假设、依据与待验证事项

科研阶段完成或会话交接前更新已有 handoff.md。
EOF
