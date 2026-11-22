#!/bin/sh
cd "$(dirname "$0")"
for k in "$@"; do
  sleep ${GAP:-230}
  n=0; until [ "$(./idle.sh)" -ge 60 ]; do sleep 15; n=$((n+1)); [ $n -gt 240 ] && { echo "user active too long at $k"; exit 1; }; done
  ./vmfront.sh >/dev/null || { echo "not front at $k"; exit 1; }
  ./vmctl click 987 853; sleep 1.5
  ./vmfront.sh >/dev/null || exit 1
  ./vmctl click 881 750; sleep 1.5
  ./shot.sh bl_term_$k >/dev/null
  SKIP_IDLE=1 ./batch_step.sh $k || { echo "step failed at $k"; exit 1; }
done
sleep 230
./vmfront.sh >/dev/null && ./shot.sh bl_final >/dev/null
echo "loop done $(TZ=Asia/Shanghai date +%H:%M:%S)"
