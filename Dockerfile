FROM python:3.12-slim

ENV LANG C.UTF-8

RUN apt-get update && apt-get install -y \
    git \
    build-essential \
    libpq-dev \
    node-less \
    npm \
    curl \
    postgresql-client \
    libldap2-dev \
    libsasl2-dev \
    && rm -rf /var/lib/apt/lists/*

# wkhtmltopdf (mínimo)
#RUN apt-get update && apt-get install -y wkhtmltopdf

WORKDIR /opt/odoo

# copiar código
COPY . /opt/odoo

######## instalar dependencias

#RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir -r /opt/odoo/requirements.txt
#WORKDIR /opt/odoo
#RUN pip install --no-cache-dir -r requirements.txt

#COPY requirements.txt /tmp/requirements.txt
#RUN pip3 install --no-cache-dir -r /tmp/requirements.txt && \
#    pip3 install --no-cache-dir websocket-client

##############



# copiar config
COPY ./config/odoo.conf /etc/odoo.conf

EXPOSE 8069

#CMD ["python3", "odoo-bin", "-c", "/etc/odoo.conf"]
COPY entrypoint.sh /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
#CMD ["sleep", "infinity"]