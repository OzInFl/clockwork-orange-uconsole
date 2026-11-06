# Pager Recorder (:8091) + System Spy (:8092) + GPS Status (:8093) + APRS Monitor (:8094): stdlib Python apps sharing /opt/clockwork-orange/lib
# (co_radio = RTL sharing with Looking Glass, co_web, co_decode). Tools they drive (rtl_fm, rtl_power,
# multimon-ng, rtl_433, readsb) come from step 60; python3-serial is for the nRF52840 802.15.4 sniffer.
for p in python3-serial python3-skyfield satdump python3-aprslib libjs-leaflet direwolf; do dpkg -s $p >/dev/null 2>&1 || sudo apt-get install -y -q $p; done   # skyfield + SatDump: weather-sat passes / Meteor LRPT
# Restart only the apps whose own files changed (a shared lib change restarts all): restarting one mid-job would cut off
# its recording (a Spy weather-sat pass, a Pager session), so an unrelated deploy must leave it running.
libchg=0
for f in co_radio.py co_web.py co_decode.py; do put /opt/clockwork-orange/lib/$f && libchg=1 || true; done
declare -A chg
for n in pager spy gps aprs; do
  chg[$n]=$libchg
  for f in $n.py index.html; do put /opt/clockwork-orange/$n/$f && chg[$n]=1 || true; done
  put /usr/local/bin/co-$n 0755 || true
  put /etc/systemd/system/co-$n.service && chg[$n]=1 || true
done
sudo systemctl daemon-reload
for n in pager spy gps aprs; do
  sudo systemctl enable -q co-$n
  if [ "${chg[$n]}" = 1 ] || ! systemctl is-active -q co-$n; then sudo systemctl restart co-$n; fi
done
sleep 2
for p in 8091 8092 8093 8094; do curl -fs -o /dev/null http://localhost:$p/api/state && echo "  :$p up" || echo "!! :$p not answering"; done
