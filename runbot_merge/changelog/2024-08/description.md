IMP: PR descriptions are now markdown-rendered in the dashboard

Previously the raw text was displayed. The main advantage of rendering, aside
from not splatting huge links in the middle of the thing, is that we can
autolink *odoo tasks* if they're of a pattern we recognize. Some support has
also been added for github's references to mirror GFM rendering.

This would be a lot less useful (and in fact pretty much useless) if we could
use github's built-in [references to external resources](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/managing-repository-settings/configuring-autolinks-to-reference-external-resources)
sadly that seems to not be available on our plan.
