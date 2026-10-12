#!/bin/sh
# Ejecutar cuando el contenedor ya responde. El miembro se anuncia como
# 127.0.0.1:27017 para que la aplicación, en el host, pueda usar el replica set.
set -eu
cd "$(dirname "$0")/.."
docker compose exec -T mongodb mongosh --quiet --eval 'try { if (rs.status().ok) { quit(0); } } catch (e) { rs.initiate({_id: "rs0", members: [{_id: 0, host: "127.0.0.1:27017"}]}); }'
echo "replica_set=initiated_or_already_present"
