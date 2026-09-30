#!/usr/bin/env bash
# Serverni GitHub'dagi yangi versiyaga yangilash (git orqali).
#
#   ssh server 'baxt_cargo/deploy/update.sh'
#
# Serverdagi kod QO'LDA o'zgartirilgan bo'lsa — hech narsa qilmaydi va nima
# o'zgarganini ko'rsatadi (2026-09-29 da boshqa AI panel parolini o'chirib
# qo'ygan edi — bunday o'zgarish endi sezilmay qolmaydi).
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "DIQQAT: serverdagi kod qo'lda o'zgartirilgan — yangilash to'xtatildi:"
  git status --short --untracked-files=no
  echo "Ko'rish: git diff   |   Bekor qilish: git checkout -- <fayl>"
  exit 1
fi

before=$(git rev-parse --short HEAD)
git fetch -q origin
git merge -q --ff-only origin/main
after=$(git rev-parse --short HEAD)

.venv/bin/python -m py_compile ./*.py
.venv/bin/python -c "import db; db.init()"          # yangi ustunlar (_migrate)
sudo systemctl restart baxt-web baxt-bot baxt-listener
sleep 3
systemctl is-active baxt-web baxt-bot baxt-listener

if [ "$before" = "$after" ]; then
  echo "O'zgarish yo'q ($after)"
else
  echo "Yangilandi: $before -> $after"
  git log --oneline "$before..$after"
fi
