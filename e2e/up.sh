#!/usr/bin/env bash
# Starts MariaDB and Matomo (PHP built-in server) in Docker and installs Matomo.
#   MATOMO_IMAGE (default matomo:5-fpm-alpine), DB_IMAGE (default mariadb:11.4)
# Leaves: Matomo on http://127.0.0.1:18080, DB on 127.0.0.1:13306 (matomo/matomo, db matomo)
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
MATOMO_IMAGE=${MATOMO_IMAGE:-matomo:5-fpm-alpine}
DB_IMAGE=${DB_IMAGE:-mariadb:11.4}
docker rm -f msm-db msm-matomo >/dev/null 2>&1 || true
# Host network: no extra Docker network needed (some hosts have no free address pool).
docker run -d --name msm-db --network host --tmpfs /var/lib/mysql \
  -e MARIADB_ROOT_PASSWORD=root -e MARIADB_DATABASE=matomo -e MARIADB_USER=matomo \
  -e MARIADB_PASSWORD=matomo -e MYSQL_ROOT_PASSWORD=root -e MYSQL_DATABASE=matomo \
  -e MYSQL_USER=matomo -e MYSQL_PASSWORD=matomo "$DB_IMAGE" --port=13306 --bind-address=127.0.0.1 >/dev/null
docker run -d --name msm-matomo --network host \
  "$MATOMO_IMAGE" php -d memory_limit=1G -S 127.0.0.1:18080 -t /var/www/html >/dev/null
for i in $(seq 1 90); do
  # mysql images have no mariadb client; both entrypoints restart the server once.
  docker exec msm-db sh -c 'c=$(command -v mariadb || command -v mysql); "$c" -h127.0.0.1 -P13306 -umatomo -pmatomo -e "select 1" matomo' >/dev/null 2>&1 && break
  sleep 2
done
for i in $(seq 1 60); do
  curl -fsS -o /dev/null http://127.0.0.1:18080/ 2>/dev/null && break
  sleep 2
done
python3 "$here/install.py" http://127.0.0.1:18080
