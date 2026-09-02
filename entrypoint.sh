#!/bin/bash

# Defaults for the connection *inside* the container, where the database is
# always reachable as the compose service `db` on 5432. Without them an unset
# variable writes an empty value, and Odoo dies parsing it with
# `ValueError: invalid literal for int() with base 10: ''` -- an error that
# says nothing about which setting is missing.
cat <<EOF > /etc/odoo.conf
[options]
addons_path = /opt/odoo/addons,/opt/odoo/custom_addons
data_dir = /var/lib/odoo

db_host = ${DB_HOST:-db}
db_port = ${DB_PORT:-5432}
db_user = ${DB_USER}
db_password = ${DB_PASSWORD}

admin_passwd = ${ADMIN_PASSWORD}
EOF

exec python3 /opt/odoo/odoo-bin -c /etc/odoo.conf