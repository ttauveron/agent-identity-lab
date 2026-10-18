#!/bin/bash
set -euo pipefail

kc=/opt/keycloak/bin/kcadm.sh
for attempt in $(seq 1 60); do
  if "$kc" config credentials --config /tmp/kcadm.config --server http://keycloak:8080 \
      --realm master --user admin --password "$KEYCLOAK_ADMIN_PASSWORD" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

actor=$($kc get clients --config /tmp/kcadm.config -r lab -q clientId=finance-agent --fields id --format csv | tr -d '"\r\n')
admin=$($kc get clients --config /tmp/kcadm.config -r lab -q clientId=admin-permissions --fields id --format csv | tr -d '"\r\n')
test -n "$actor"
test -n "$admin"
root="clients/$admin/authz/resource-server"

if ! "$kc" get "$root/policy" --config /tmp/kcadm.config -r lab --fields name --format csv | grep -Fqx '"finance-agent-can-delegate"'; then
  sed "s/\${ACTOR_CLIENT_UUID}/$actor/g" /declarations/delegation-policy.json >/tmp/delegation-policy.json
  "$kc" create "$root/policy/client" --config /tmp/kcadm.config -r lab -f /tmp/delegation-policy.json >/dev/null
fi
if ! "$kc" get "$root/permission/scope" --config /tmp/kcadm.config -r lab --fields name --format csv | grep -Fqx '"finance-agent-delegate-users"'; then
  "$kc" create "$root/permission/scope" --config /tmp/kcadm.config -r lab -f /declarations/delegation-permission.json >/dev/null
fi
echo 'Keycloak declarations applied'
