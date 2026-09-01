#!/usr/bin/env bash
# Przestawia dev na main. Bez tego gałęzie rozjeżdżają się przy każdym
# landowaniu (squash-merge zmienia SHA, cron commituje dane wprost na main),
# a git skleja wtedy CITY_DISTRICT_DISPLAY linia po linii — bez zgłoszenia
# konfliktu i z wartościami spoza obu stron.
#
# Reset tylko gdy dev nie wnosi nic ponad main; sprawdzane próbnym scaleniem.

set -euo pipefail

git fetch --quiet origin main dev

main_tree=$(git rev-parse origin/main^{tree})

if ! merged_tree=$(git merge-tree --write-tree origin/main origin/dev 2>/dev/null | head -1); then
    echo "Próbne scalenie dev do main daje konflikt — pomijam sync."
    exit 0
fi

if [ "$merged_tree" != "$main_tree" ]; then
    echo "dev zawiera zmiany, których nie ma w main — pomijam sync."
    echo "Różnice:"
    git diff --stat origin/main origin/dev
    exit 0
fi

if [ "$(git rev-parse origin/dev)" = "$(git rev-parse origin/main)" ]; then
    echo "dev już wskazuje na main — nic do zrobienia."
    exit 0
fi

echo "dev nie wnosi nic ponad main — przestawiam dev na $(git rev-parse --short origin/main)."
git push --force-with-lease=refs/heads/dev:"$(git rev-parse origin/dev)" \
    origin "$(git rev-parse origin/main)":refs/heads/dev
echo "dev zsynchronizowany z main."
