# -*- coding: utf-8 -*-
import logging

from unittest.mock import patch, mock_open

from odoo.tests.common import Form, tagged, HttpCase
from .common import RunbotCase

_logger = logging.getLogger(__name__)


@tagged('-at_install', 'post_install')
class TestDockerfile(RunbotCase, HttpCase):

    def test_docker_default(self):
        self.maxDiff = None
        self.assertEqual(
            self.env.ref('runbot.docker_default').dockerfile.replace('\n\n', '\n'),
            r"""FROM ubuntu:jammy
ENV LANG C.UTF-8
USER root
RUN set -x ; \
    apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends apt-transport-https build-essential ca-certificates curl ffmpeg file fonts-freefont-ttf fonts-noto-cjk gawk gnupg gsfonts libldap2-dev libjpeg9-dev libsasl2-dev libxslt1-dev lsb-release node-less ocrmypdf sed sudo unzip xfonts-75dpi zip zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*
RUN set -x ; \
    apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3 python3-dbfread python3-dev python3-pip python3-setuptools python3-wheel python3-markdown python3-mock python3-phonenumbers plibpq-dev python3-gevent python3-websocket \
    && rm -rf /var/lib/apt/lists/*
# Install wkhtml
RUN curl -sSL https://github.com/wkhtmltopdf/wkhtmltopdf/releases/download/0.12.5/wkhtmltox_0.12.5-1.bionic_amd64.deb -o /tmp/wkhtml.deb \
    && apt-get update \
    && dpkg --force-depends -i /tmp/wkhtml.deb \
    && apt-get install -y -f --no-install-recommends \
    && rm /tmp/wkhtml.deb
# Install nodejs with values {"node_version": "20"}
RUN curl -sSL https://deb.nodesource.com/gpgkey/nodesource.gpg.key | apt-key add - \
    && echo "deb https://deb.nodesource.com/node_20.x `lsb_release -c -s` main" > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y nodejs
RUN npm install -g rtlcss es-check eslint
ADD https://raw.githubusercontent.com/brendangregg/FlameGraph/master/flamegraph.pl /usr/local/bin/flamegraph.pl
RUN chmod +rx /usr/local/bin/flamegraph.pl
# Install branch debian/control with values {"odoo_branch": "master"}
ADD https://raw.githubusercontent.com/odoo/odoo/master/debian/control /tmp/control.txt
RUN apt-get update \
    && sed -n '/^Depends:/,/^[A-Z]/p' /tmp/control.txt \
       | awk '/^ [a-z]/ { gsub(/,/,"") ; gsub(" ", "") ; print $NF }' | sort -u \
       | egrep -v 'postgresql-client' \
       | sed 's/python-imaging/python-pil/'| sed 's/python-pypdf/python-pypdf2/' \
       | DEBIAN_FRONTEND=noninteractive xargs apt-get install -y -qq \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m pip install --no-cache-dir coverage==4.5.4 astroid==2.4.2 pylint==2.5.0 flamegraph
RUN python3 -m pip install --no-cache-dir ebaysdk==2.1.5 pdf417gen==0.7.1
RUN curl -sSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | apt-key add - \
    && echo "deb http://apt.postgresql.org/pub/repos/apt/ `lsb_release -s -c`-pgdg main" > /etc/apt/sources.list.d/pgclient.list \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y postgresql-client-12 \
    && rm -rf /var/lib/apt/lists/*
# Install chrome with values {"chrome_version": "123.0.6312.58-1"}
RUN curl -sSL https://dl.google.com/linux/chrome/deb/pool/main/g/google-chrome-stable/google-chrome-stable_123.0.6312.58-1_amd64.deb -o /tmp/chrome.deb \
    && apt-get update \
    && apt-get -y install --no-install-recommends /tmp/chrome.deb \
    && rm /tmp/chrome.deb
# Install branch requirements with values {"odoo_branch": "master"}
ADD https://raw.githubusercontent.com/odoo/odoo/master/requirements.txt /root/requirements.txt
RUN python3 -m pip install --no-cache-dir -r /root/requirements.txt""")

    def test_dockerfile_base_fields(self):
        xml_content = """<t t-call="runbot.docker_base">
    <t t-set="custom_values" t-value="{
      'from': 'ubuntu:jammy',
      'phantom': True,
      'additional_pip': 'babel==2.8.0',
      'chrome_source': 'odoo',
      'chrome_version': '86.0.4240.183-1',
    }"/>
</t>
"""

        focal_template = self.env['ir.ui.view'].create({
            'name': 'docker_focal_test',
            'type': 'qweb',
            'key': 'docker.docker_focal_test',
            'arch_db': xml_content
        })

        dockerfile = self.env['runbot.dockerfile'].create({
            'name': 'Tests Ubuntu Focal (20.0)[Chrome 86]',
            'template_id': focal_template.id,
            'to_build': True
        })

        self.assertEqual(dockerfile.image_tag, 'odoo:TestsUbuntuFocal20.0Chrome86')
        self.assertTrue(dockerfile.dockerfile.startswith('FROM ubuntu:jammy'))
        self.assertIn(' apt-get install -y -qq google-chrome-stable=86.0.4240.183-1', dockerfile.dockerfile)
        self.assertIn('# Install phantomjs', dockerfile.dockerfile)
        self.assertIn('pip install --no-cache-dir babel==2.8.0', dockerfile.dockerfile)

        # test view update
        xml_content = xml_content.replace('86.0.4240.183-1', '87.0-1')
        dockerfile_form = Form(dockerfile)
        dockerfile_form.arch_base = xml_content
        dockerfile_form.save()

        self.assertIn('apt-get install -y -qq google-chrome-stable=87.0-1', dockerfile.dockerfile)

        # Ensure that only the test dockerfile will be found by docker_run
        self.env['runbot.dockerfile'].search([('id', '!=', dockerfile.id)]).update({'to_build': False})

        def write_side_effect(content):
            self.assertIn('apt-get install -y -qq google-chrome-stable=87.0-1', content)

        docker_build_mock = self.patchers['docker_build']
        docker_build_mock.return_value = (True, None)
        mopen = mock_open()
        rb_host = self.env['runbot.host'].create({'name': 'runbotxxx.odoo.com'})
        with patch('builtins.open', mopen) as file_mock:
            file_handle_mock = file_mock.return_value.__enter__.return_value
            file_handle_mock.write.side_effect = write_side_effect
            rb_host._docker_build()
