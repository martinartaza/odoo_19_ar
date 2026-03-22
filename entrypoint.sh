#!/bin/bash

cat <<EOF > /etc/odoo.conf
[options]
addons_path = /opt/odoo/addons,/opt/odoo/custom_addons
data_dir = /var/lib/odoo

db_host = ${DB_HOST}
db_port = ${DB_PORT}
db_user = ${DB_USER}
db_password = ${DB_PASSWORD}

admin_passwd = ${ADMIN_PASSWORD}
EOF

exec python3 /opt/odoo/odoo-bin -c /etc/odoo.conf