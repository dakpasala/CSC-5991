#!/bin/sh
# Turn the lab router's QoS test setup on or off. Runs ON the router
# (OpenWrt, BusyBox ash). Approved by the professor for test runs only:
# install it before a QoS experiment, remove it right after.
#
#   sh /root/csc5991/qos.sh up      # shape lan2 at 100 Mbit + priority set
#   sh /root/csc5991/qos.sh down    # remove both (and nothing else)
#   sh /root/csc5991/qos.sh status
#
# What "up" does:
#   1. Installs kmod-sched-cake + tc-full from the official OpenWrt repo
#      (first time only).
#   2. Puts cake on lan2's egress -- traffic from the router toward the
#      Linux box -- at 100 Mbit, diffserv4, flowblind:
#        - lan2 egress, not WAN ingress: ingress shaping runs before the
#          firewall, so the priority marks below would be invisible to it;
#          egress on lan2 runs after the firewall, and the test traffic
#          (downloads to the Linux box) flows that way anyway.
#        - 100 Mbit: well under the ~570 Mbit link, so the router's CPU is
#          never the bottleneck and congestion is easy to create on purpose.
#        - diffserv4: packets marked DSCP AF41 go to cake's "Video" tin,
#          served ahead of best-effort traffic.
#        - flowblind: cake's per-flow fairness would protect a video flow
#          from a handful of downloads all by itself; turning it off means
#          the only protection is the classifier's priority, which is what
#          the experiment measures.
#   3. Creates nftables table inet csc5991 with a set of prioritized
#      connections (live-router -priority-enforce fills it) and one rule:
#      server->client packets of a connection in the set get DSCP AF41.
#      Set entries carry a timeout, so marks lapse on their own if
#      live-router stops.
#
# "down" removes the cake qdisc from lan2 (lan2 goes back to its default)
# and deletes table inet csc5991. It does not touch fw4 or anything else.
set -e

IFACE=${IFACE:-lan2}
RATE=${RATE:-100Mbit}

up() {
    if ! apk info -e kmod-sched-cake >/dev/null 2>&1 || ! apk info -e tc-full >/dev/null 2>&1; then
        apk update >/dev/null
        apk add kmod-sched-cake tc-full
    fi
    tc qdisc replace dev "$IFACE" root cake bandwidth "$RATE" diffserv4 flowblind
    nft delete table inet csc5991 2>/dev/null || true
    nft -f - <<'EOF'
table inet csc5991 {
    # client ip . protocol . client port . server ip . server port
    set video_flows {
        type ipv4_addr . inet_proto . inet_service . ipv4_addr . inet_service
        flags timeout
        timeout 3m
    }
    chain forward_mark {
        type filter hook forward priority mangle; policy accept;
        # Download direction (server -> client): mark for cake's Video tin.
        ip daddr . meta l4proto . th dport . ip saddr . th sport @video_flows ip dscp set af41 counter
    }
}
EOF
    status
}

down() {
    tc qdisc del dev "$IFACE" root 2>/dev/null || true
    nft delete table inet csc5991 2>/dev/null || true
    echo "removed: cake on $IFACE and table inet csc5991"
    status
}

status() {
    echo "--- qdisc on $IFACE"
    tc qdisc show dev "$IFACE"
    echo "--- priority set / marking rule"
    nft list table inet csc5991 2>/dev/null || echo "(table inet csc5991 not present)"
}

case "$1" in
    up) up ;;
    down) down ;;
    status) status ;;
    *) echo "usage: $0 up|down|status" >&2; exit 1 ;;
esac
