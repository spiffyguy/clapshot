#!/bin/bash

# This a Docker-side support script to run a demo of Clapshot (API server + Nginx)
# in a single Docker container for demo and testing purposes.

DIR=${DIR:-"/mnt/clapshot-data/data"}
URL_BASE=${URL_BASE:-"127.0.0.1:8080/"}

# Use same URL base as index.html for API server (as Nginx proxies localhost:8095/api to /api)
# - Also enable basic auth logout button
cat > /etc/clapshot_client.conf << EOF
{
  "ws_url": "ws://${URL_BASE}api/ws",
  "upload_url": "http://${URL_BASE}api/upload",
    "user_menu_extra_items": [
        { "label": "My Videos", "type": "url", "data": "/" }
    ],
  "user_menu_show_basic_auth_logout": true
}
EOF

# Assume user accesses this at $URL_BASE
sed -i "s/^url-base.*/url-base = http://${URL_BASE}/g" /etc/clapshot-server.conf
echo "migrate = true" >> /etc/clapshot-server.conf

# Make server data dir and log accessible to docker user
chown -R docker "$DIR"
touch "$DIR/clapshot.log"
chown docker "$DIR/clapshot.log"
ln -s "$DIR/clapshot.log" /var/log/

# Start nginx (in the background)
nginx
php-fpm7.4

# Disable log buffering for better docker experience
export ENV PYTHONDONTWRITEBYTECODE=1
export ENV PYTHONUNBUFFERED=1

set -v

echo <<- "EOF"
==============================================
  _____                   _
 |  __ \                 (_)
 | |__) _   _ _ __  _ __  _ _ __   __ _
 |  _  | | | | '_ \| '_ \| | '_ \ / _` |
 | | \ | |_| | | | | | | | | | | | (_| |
 |_|  \_\__,_|_| |_|_| |_|_|_| |_|\__, |
     _____ _                 _     __/ | _
    / ____| |               | |   |___/ | |
   | |    | | __ _ _ __  ___| |__   ___ | |_
   | |    | |/ _` | '_ \/ __| '_ \ / _ \| __|
   | |____| | (_| | |_) \__ | | | | (_) | |_
    \_____|_|\__,_| .__/|___|_| |_|\___/ \__|
                  | |
                  |_|

---  Browse http://127.0.0.1:8080/         for Clapshot
---  or     http://127.0.0.1/8080/htadmin  for user management
---
---  Default users:
---   - admin:admin     (can edit other people's videos)
---   - demo:demo
---   - alice:alice123
---
---  User management admin:
---   - htadmin:admin   (only for /htadmin)
==============================================================
EOF

# Dig up start command from systemd script and run it as docker user instead of www-data
CMD=$(grep 'Exec' /lib/systemd/system/clapshot-server.service | sed 's/^.*=//')
sudo -u docker $CMD &

# Follow server log
tail -f /var/log/clapshot.log
