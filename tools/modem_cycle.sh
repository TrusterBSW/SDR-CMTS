#!/bin/bash
# Power-cycles the SFR modem through its Home Assistant smart plug, then waits until it is back
# online (DHCP lease on the laptop's modem NIC + ping 9.9.9.9 through it). Prints a timeline.
#   modem_cycle.sh [off_seconds]
set -u
HA=${HA:-http://10.5.1.203:8123}
PLUG=${PLUG:-switch.0x70b3d52b600443a8}
NIC=${NIC:-enp0s20f0u4}
off=${1:-10}
auth="Authorization: Bearer $(cat ~/.config/ha_token)"
ts() { date +%T.%3N; }
svc() { curl -s -m 10 -o /dev/null -w "%{http_code}" -H "$auth" -H "Content-Type: application/json" \
  -d "{\"entity_id\": \"$PLUG\"}" "$HA/api/services/switch/$1"; }
power() { curl -s -m 5 -H "$auth" "$HA/api/states/${PLUG/switch./sensor.}_power" | python3 -c "import json,sys;print(json.load(sys.stdin)['state'])"; }
echo "$(ts) coupure ($(svc turn_off))"; sleep "$off"
echo "$(ts) remise sous tension ($(svc turn_on))"
t0=$SECONDS; got=""
while [ $((SECONDS - t0)) -lt 600 ]; do
  if [ -z "$got" ]; then
    # the modem first leases 192.168.100.x itself (gateway 192.168.100.1) until it is online:
    # wait for SFR's lease
    gw=$(nmcli -t -f DHCP4.OPTION dev show "$NIC" 2>/dev/null | sed -n 's/.*:routers = //p')
    if [ "$gw" = 192.168.100.1 ]; then
      gw=""
      [ $(((SECONDS - t0) % 15)) = 0 ] && nmcli --wait 0 con up sfr-lab >/dev/null 2>&1   # new DHCP
    fi
    if [ -z "$gw" ]; then
      nmcli -t -f GENERAL.STATE dev show "$NIC" | grep -q "^GENERAL.STATE:[0-9]* (disconnected)" && nmcli --wait 0 con up sfr-lab >/dev/null 2>&1
    else
      got=1; echo "$(ts) bail DHCP SFR ($(ip -4 -br addr show "$NIC" | grep -oE '88\.[0-9.]+')) après $((SECONDS - t0)) s"
      sudo ip route replace 9.9.9.9 via "$gw" dev "$NIC"
    fi
  fi
  if [ -n "$got" ] && ping -c 1 -W 1 -I "$NIC" 9.9.9.9 >/dev/null 2>&1; then
    echo "$(ts) en ligne (ping 9.9.9.9 par le modem) après $((SECONDS - t0)) s, prise $(power) W"; exit 0
  fi
  sleep 1
done
echo "$(ts) !! pas revenu en ligne après 600 s"; exit 1
