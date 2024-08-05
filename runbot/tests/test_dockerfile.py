# -*- coding: utf-8 -*-
import getpass
import logging
import os

from odoo import Command
from unittest.mock import patch, mock_open

from odoo.tests.common import Form, tagged, HttpCase
from .common import RunbotCase

_logger = logging.getLogger(__name__)

USERUID = os.getuid()
USERGID = os.getgid()
USERNAME = getpass.getuser()

@tagged('-at_install', 'post_install')
class TestDockerfile(RunbotCase, HttpCase):

    def test_docker_default(self):
        self.maxDiff = None
        self.assertEqual(
            self.env.ref('runbot.docker_default').dockerfile.replace('\n\n', '\n'),
            r"""FROM ubuntu:jammy
ENV LANG C.UTF-8
USER root
# Install debian packages with values {"$packages": "apt-transport-https build-essential ca-certificates curl ffmpeg file flake8 fonts-freefont-ttf fonts-noto-cjk gawk gnupg gsfonts libldap2-dev libjpeg9-dev libsasl2-dev libxslt1-dev lsb-release ocrmypdf sed sudo unzip xfonts-75dpi zip zlib1g-dev"}
RUN set -x ; \
    apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends apt-transport-https build-essential ca-certificates curl ffmpeg file flake8 fonts-freefont-ttf fonts-noto-cjk gawk gnupg gsfonts libldap2-dev libjpeg9-dev libsasl2-dev libxslt1-dev lsb-release ocrmypdf sed sudo unzip xfonts-75dpi zip zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*
# Install debian packages with values {"$packages": "python3 python3-dbfread python3-dev python3-gevent python3-pip python3-setuptools python3-wheel python3-markdown python3-mock python3-phonenumbers python3-websocket python3-google-auth libpq-dev python3-asn1crypto python3-jwt publicsuffix python3-xmlsec python3-aiosmtpd pylint"}
RUN set -x ; \
    apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3 python3-dbfread python3-dev python3-gevent python3-pip python3-setuptools python3-wheel python3-markdown python3-mock python3-phonenumbers python3-websocket python3-google-auth libpq-dev python3-asn1crypto python3-jwt publicsuffix python3-xmlsec python3-aiosmtpd pylint \
    && rm -rf /var/lib/apt/lists/*
# Install wkhtmltopdf
RUN curl -sSL https://nightly.odoo.com/deb/jammy/wkhtmltox_0.12.5-2.jammy_amd64.deb -o /tmp/wkhtml.deb \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get -y install --no-install-recommends --fix-missing -qq /tmp/wkhtml.deb \
    && rm -rf /var/lib/apt/lists/* \
    && rm /tmp/wkhtml.deb
# Install nodejs with values {"node_version": "20"}
RUN curl -s https://deb.nodesource.com/gpgkey/nodesource.gpg.key | gpg --dearmor | tee /usr/share/keyrings/nodesource.gpg > /dev/null \
    && echo "deb [signed-by=/usr/share/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x `lsb_release -c -s` main" > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y nodejs
RUN npm install -g rtlcss@3.4.0 es-check@6.0.0 eslint@8.1.0 prettier@2.7.1 eslint-config-prettier@8.5.0 eslint-plugin-prettier@4.2.1
# Install branch debian/control with values {"odoo_branch": "master"}
ADD https://raw.githubusercontent.com/odoo/odoo/master/debian/control /tmp/control.txt
RUN apt-get update \
    && sed -n '/^Depends:/,/^[A-Z]/p' /tmp/control.txt \
        | awk '/^ [a-z]/ { gsub(/,/,"") ; gsub(" ", "") ; print $NF }' | sort -u \
        | egrep -v 'postgresql-client' \
        | DEBIAN_FRONTEND=noninteractive xargs apt-get install -y -qq --no-install-recommends \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*
# Install pip packages with values {"$packages": "astroid==2.4.2 pylint==2.5.0"}
RUN python3 -m pip install --no-cache-dir astroid==2.4.2 pylint==2.5.0
# Install pip packages with values {"$packages": "ebaysdk==2.1.5 pdf417gen==0.7.1"}
RUN python3 -m pip install --no-cache-dir ebaysdk==2.1.5 pdf417gen==0.7.1
RUN curl -sSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | apt-key add - \
    && echo "deb http://apt.postgresql.org/pub/repos/apt/ `lsb_release -s -c`-pgdg main" > /etc/apt/sources.list.d/pgclient.list \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y postgresql-client-14 \
    && rm -rf /var/lib/apt/lists/*
# Install chrome with values {"chrome_version": "123.0.6312.58-1"}
RUN curl -sSL https://dl.google.com/linux/chrome/deb/pool/main/g/google-chrome-stable/google-chrome-stable_123.0.6312.58-1_amd64.deb -o /tmp/chrome.deb \
    && apt-get update \
    && apt-get -y install --no-install-recommends /tmp/chrome.deb \
    && rm /tmp/chrome.deb
# Install branch requirements with values {"odoo_branch": "master"}
ADD https://raw.githubusercontent.com/odoo/odoo/master/requirements.txt /tmp/requirements.txt
RUN python3 -m pip install --no-cache-dir -r /tmp/requirements.txt""" f"""
# Create user template with values {{"USERUID": {USERUID}, "USERGID": {USERGID}, "USERNAME": "{USERNAME}"}}
RUN groupadd -g {USERGID} {USERNAME} && useradd --create-home -u {USERUID} -g {USERNAME} -G audio,video {USERNAME}
# Switch user with values {{"USERNAME": "{USERNAME}"}}
USER {USERNAME}
""")

    def test_dockerfile_base_fields(self):
        dockerfile = self.env['runbot.dockerfile'].create({
            'name': 'Tests Ubuntu Focal (20.0)[Chrome 86]',
            'to_build': True,
            'layer_ids': [
                Command.create({
                    'name': 'Customized base',
                    'reference_dockerfile_id': self.env.ref('runbot.docker_default').id,
                    'values': {
                        'from': 'ubuntu:jammy',
                        'phantom': True,
                        'chrome_version': '86.0.4240.183-1',
                    },
                    'layer_type': 'reference_file',
                }),
                Command.create({
                    'name': 'Customized base',
                    'packages': 'babel==2.8.0',
                    'layer_type': 'reference_layer',
                    'reference_docker_layer_id': self.env.ref('runbot.docker_layer_pip_packages_template').id,
                }),
            ],
        })

        self.assertEqual(dockerfile.image_tag, 'odoo:TestsUbuntuFocal20.0Chrome86')
        self.assertTrue(dockerfile.dockerfile.startswith('FROM ubuntu:jammy'))
        self.assertIn('86.0.4240.183-1', dockerfile.dockerfile)
        self.assertIn('pip install --no-cache-dir babel==2.8.0', dockerfile.dockerfile)

        # test layer update
        dockerfile.layer_ids[0].values = {**dockerfile.layer_ids[0].values, 'chrome_version': '87.0.4240.183-1'}

        self.assertIn('Install chrome with values {"chrome_version": "87.0.4240.183-1"}', dockerfile.dockerfile)
