# -*- coding: utf-8 -*-
from __future__ import annotations

import base64
import collections
import colorsys
import hashlib
import io
import json
import logging
import math
import pathlib
from email.utils import formatdate
from itertools import chain, product
from typing import Tuple, cast, Mapping

import markdown
import markupsafe
import werkzeug.exceptions
import werkzeug.wrappers
from PIL import Image, ImageDraw, ImageFont

from odoo.http import Controller, route, request
from odoo.tools import file_open


_logger = logging.getLogger(__name__)

LIMIT = 20
class MergebotDashboard(Controller):
    @route('/runbot_merge', auth="public", type="http", website=True, sitemap=True)
    def dashboard(self):
        projects = request.env['runbot_merge.project'].with_context(active_test=False).sudo().search([])
        stagings = {
            branch: projects.env['runbot_merge.stagings'].search([
                ('target', '=', branch.id)], order='staged_at desc', limit=6)
            for project in projects
            for branch in project.branch_ids
            if branch.active
        }
        prefetch_set = list({
            id
            for stagings in stagings.values()
            for id in stagings.ids
        })
        for st in stagings.values():
            st._prefetch_ids = prefetch_set

        return request.render('runbot_merge.dashboard', {
            'projects': projects,
            'stagings_map': stagings,
        })

    @route('/runbot_merge/<int:branch_id>', auth='public', type='http', website=True, sitemap=False)
    def stagings(self, branch_id, until=None, state=''):
        branch = request.env['runbot_merge.branch'].browse(branch_id).sudo().exists()
        if not branch:
            raise werkzeug.exceptions.NotFound()

        staging_domain = [('target', '=', branch.id)]
        if until:
            staging_domain.append(('staged_at', '<=', until))
        if state:
            staging_domain.append(('state', '=', state))

        stagings = request.env['runbot_merge.stagings'].with_context(active_test=False).sudo().search(staging_domain, order='staged_at desc', limit=LIMIT + 1)

        return request.render('runbot_merge.branch_stagings', {
            'branch': branch,
            'stagings': stagings[:LIMIT],
            'until': until,
            'state': state,
            'next': stagings[-1].staged_at if len(stagings) > LIMIT else None,
        })

    def _entries(self):
        changelog = pathlib.Path(__file__).parent.parent / 'changelog'
        if changelog.is_dir():
            return [
                (d.name, [f.read_text(encoding='utf-8') for f in d.iterdir() if f.is_file()])
                for d in changelog.iterdir()
            ]
        return []

    def entries(self, item_converter):
        entries = collections.OrderedDict()
        for key, items in sorted(self._entries(), reverse=True):
            entries.setdefault(key, []).extend(map(item_converter, items))
        return entries

    @route('/runbot_merge/changelog', auth='public', type='http', website=True, sitemap=True)
    def changelog(self):
        md = markdown.Markdown(extensions=['nl2br'], output_format='html5')
        entries = self.entries(lambda t: markupsafe.Markup(md.convert(t)))
        return request.render('runbot_merge.changelog', {
            'entries': entries,
        })

    @route('/<org>/<repo>/pull/<int(min=1):pr><any("", ".png"):png>', auth='public', type='http', website=True, sitemap=False)
    def pr(self, org, repo, pr, png):
        pr_id = request.env['runbot_merge.pull_requests'].sudo().search([
            ('repository.name', '=', f'{org}/{repo}'),
            ('number', '=', int(pr)),
        ])
        if not pr_id:
            raise werkzeug.exceptions.NotFound()
        if not pr_id.repository.group_id <= request.env.user.groups_id:
            _logger.warning(
                "Access error: %s (%s) tried to access %s but lacks access",
                request.env.user.login,
                request.env.user.name,
                pr_id.display_name,
            )
            raise werkzeug.exceptions.NotFound()

        if png:
            return raster_render(pr_id)

        st = {}
        if pr_id.statuses:
            # normalise `statuses` to map to a dict
            st = {
                k: {'state': v} if isinstance(v, str) else v
                for k, v in json.loads(pr_id.statuses_full).items()
            }
        return request.render('runbot_merge.view_pull_request', {
            'pr': pr_id,
            'merged_head': json.loads(pr_id.commits_map).get(''),
            'statuses': st
        })

def raster_render(pr):
    default_headers = {
        'Content-Type': 'image/png',
        'Last-Modified': formatdate(),
        # - anyone can cache the image, so public
        # - crons run about every minute so that's how long a request is fresh
        # - if the mergebot can't be contacted, allow using the stale response (no must-revalidate)
        # - intermediate caches can recompress the PNG if they want (pillow is not a very good PNG generator)
        # - the response is mutable even during freshness, technically (as there
        #   is no guarantee the freshness window lines up with the cron, plus
        #   some events are not cron-based)
        # - maybe don't allow serving the stale image *while* revalidating?
        # - allow serving a stale image for a day if the server returns 500
        'Cache-Control': 'public, max-age=60, stale-if-error=86400',
    }
    if if_none_match := request.httprequest.headers.get('If-None-Match'):
        # just copy the existing value out if we received any
        default_headers['ETag'] = if_none_match

    # weak validation: check the latest modification date of all objects involved
    project, repos, branches, genealogy = pr.env.ref('runbot_merge.dashboard-pre')\
        ._run_action_code_multi({'pr': pr})

    # last-modified should be in RFC2822 format, which is what
    # email.utils.formatdate does (sadly takes a timestamp but...)
    last_modified = formatdate(max((
        o.write_date
        for o in chain(
            project,
            repos,
            branches,
            genealogy,
            genealogy.all_prs | pr,
        )
    )).timestamp())
    # The (304) response must not contain a body and must include the headers
    # that would have been sent in an equivalent 200 OK response
    headers = {**default_headers, 'Last-Modified': last_modified}
    if request.httprequest.headers.get('If-Modified-Since') == last_modified:
        return werkzeug.wrappers.Response(status=304, headers=headers)

    with file_open('web/static/fonts/google/Open_Sans/Open_Sans-Regular.ttf', 'rb') as f:
        font = ImageFont.truetype(f, size=16, layout_engine=0)
        f.seek(0)
        supfont = ImageFont.truetype(f, size=10, layout_engine=0)
    with file_open('web/static/fonts/google/Open_Sans/Open_Sans-Bold.ttf', 'rb') as f:
        bold = ImageFont.truetype(f, size=16, layout_engine=0)

    batches = pr.env.ref('runbot_merge.dashboard-prep')._run_action_code_multi({
        'pr': pr,
        'repos': repos,
        'branches': branches,
        'genealogy': genealogy,
    })

    # getbbox returns (left, top, right, bottom)

    rows = {b: font.getbbox(b.name)[3] for b in branches}
    rows[None] = max(bold.getbbox(r.name)[3] for r in repos)

    columns = {r: bold.getbbox(r.name)[2] for r in repos}
    columns[None] = max(font.getbbox(b.name)[2] for b in branches)

    etag = hashlib.sha256(f"(P){pr.id},{pr.repository.id},{pr.target.id}".encode())
    # repos and branches should be in a consistent order so can just hash that
    etag.update(''.join(f'(R){r.name}' for r in repos).encode())
    etag.update(''.join(f'(T){b.name},{b.active}' for b in branches).encode())
    # and product of deterministic iterations should be deterministic
    for r, b in product(repos, branches):
        ps = batches[r, b]
        etag.update(f"(B){ps['state']},{ps['detached']},{ps['active']}".encode())
        # technically label (state + blocked) does not actually impact image
        # render (though subcomponents of state do) however blocked is useful
        # to force an etag miss so keeping it
        # TODO: blocked includes draft & merge method, maybe should change looks?
        etag.update(''.join(
            f"(PS){p['label']},{p['closed']},{p['number']},{p['checked']},{p['reviewed']},{p['attached']}"
            for p in ps['prs']
        ).encode())

        w = h = 0
        for p in ps['prs']:
            _, _, ww, hh = font.getbbox(f" #{p['number']}")
            w += ww + supfont.getbbox(' '.join(filter(None, [
                'error' if p['pr'].error else '',
                '' if p['checked'] else 'unchecked',
                '' if p['reviewed'] else 'unreviewed',
                '' if p['attached'] else 'detached',
            ])))[2]
            h = max(hh, h)
        rows[b] = max(rows.get(b, 0), h)
        columns[r] = max(columns.get(r, 0), w)

    etag = headers['ETag'] = base64.b32encode(etag.digest()).decode()
    if if_none_match == etag:
        return werkzeug.wrappers.Response(status=304, headers=headers)

    pad_w, pad_h = 20, 5
    image_height = sum(rows.values()) + 2 * pad_h * len(rows)
    image_width = sum(columns.values()) + 2 * pad_w * len(columns)
    im = Image.new("RGB", (image_width+1, image_height+1), color='white')
    draw = ImageDraw.Draw(im, 'RGB')
    draw.font = font

    # for reasons of that being more convenient we store the bottom of the
    # current row, so getting the top edge requires subtracting h
    w = left = bottom = 0
    for b, r in product(chain([None], branches), chain([None], repos)):
        left += w

        opacity = 1.0 if b is None or b.active else 0.5
        background = BG['info'] if b == pr.target or r == pr.repository else BG[None]
        w, h = columns[r] + 2 * pad_w, rows[b] + 2 * pad_h

        if r is None: # branch cell in row
            left = 0
            bottom += h
            if b:
                draw.rectangle(
                    (left + 1, bottom - h + 1, left+w - 1, bottom - 1),
                    background,
                )
                draw.text(
                    (left + pad_w, bottom - h + pad_h),
                    b.name,
                    fill=blend(TEXT, opacity, over=background),
                )
        elif b is None: # repo cell in top row
            draw.rectangle((left + 1, bottom - h + 1, left+w - 1, bottom - 1), background)
            draw.text((left + pad_w, bottom - h + pad_h), r.name, fill=TEXT, font=bold)
        # draw the bottom-right edges of the cell
        draw.line([
            (left, bottom), # bottom-left
            (left + w, bottom), # bottom-right
            (left+w, bottom-h) # top-right
        ], fill=(172, 176, 170))
        if r is None or b is None:
            continue

        ps = batches[r, b]

        bgcolor = BG[ps['state']]
        if pr in ps['pr_ids']:
            bgcolor = lighten(bgcolor, by=-0.05)
        background = blend(bgcolor, opacity, over=background)
        draw.rectangle((left + 1, bottom - h + 1, left+w - 1, bottom - 1), background)

        top = bottom - h + pad_h
        offset = left + pad_w
        for p in ps['prs']:
            label = f"#{p['number']}"
            foreground = blend((39, 110, 114), opacity, over=background)
            draw.text((offset, top), label, fill=foreground)
            x, _, ww, hh = font.getbbox(label)
            if p['closed']:
                draw.line([
                    (offset+x, top + hh - hh/3),
                    (offset+x+ww, top + hh - hh/3),
                ], fill=foreground)
            offset += ww
            if not p['attached']:
                # overdraw top border to mark the detachment
                draw.line([(left, bottom-h), (left+w, bottom-h)], fill=ERROR)
            for attribute in filter(None, [
                'error' if p['pr'].error else '',
                '' if p['checked'] else 'unchecked',
                '' if p['reviewed'] else 'unreviewed',
                '' if p['attached'] else 'detached',
                'staged' if p['pr'].staging_id else 'ready' if p['pr']._ready else ''
            ]):
                label = f' {attribute}'
                color = SUCCESS if attribute in ('staged', 'ready') else ERROR
                draw.text((offset, top), label,
                          fill=blend(color, opacity, over=background),
                          font=supfont)
                offset += supfont.getbbox(label)[2]
            offset += math.ceil(supfont.getlength(" "))

    buffer = io.BytesIO()
    im.save(buffer, 'png', optimize=True)
    return werkzeug.wrappers.Response(buffer.getvalue(), headers=headers)

Color = Tuple[int, int, int]
TEXT: Color = (102, 102, 102)
ERROR: Color = (220, 53, 69)
SUCCESS: Color = (40, 167, 69)
BG: Mapping[str | None, Color] = collections.defaultdict(lambda: (255, 255, 255), {
    'info': (217, 237, 247),
    'success': (223, 240, 216),
    'warning': (252, 248, 227),
    'danger': (242, 222, 222),
})
def blend_single(c: int, over: int, opacity: float) -> int:
    return round(over * (1 - opacity) + c * opacity)

def blend(color: Color, opacity: float, *, over: Color = (255, 255, 255)) -> Color:
    assert 0.0 <= opacity <= 1.0
    return (
        blend_single(color[0], over[0], opacity),
        blend_single(color[1], over[1], opacity),
        blend_single(color[2], over[2], opacity),
    )

def lighten(color: Color, *, by: float) -> Color:
    # colorsys uses values in the range [0, 1] rather than pillow/CSS-style [0, 225]
    r, g, b = tuple(c / 255 for c in color)
    hue, lightness, saturation = colorsys.rgb_to_hls(r, g, b)

    # by% of the way between value and 1.0
    if by >= 0: lightness += (1.0 - lightness) * by
    # -by% of the way between 0 and value
    else:lightness *= (1.0 + by)

    return cast(Color, tuple(
        round(c * 255)
        for c in colorsys.hls_to_rgb(hue, lightness, saturation)
    ))
