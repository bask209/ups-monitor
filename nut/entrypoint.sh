#!/bin/sh
# NUT driver + upsd for the USB UPS. Container root == host user (rootless).
set -e
mkdir -p /var/state/ups
/usr/sbin/upsdrvctl -u root start
exec /usr/sbin/upsd -F -u root
