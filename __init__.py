# SPDX-License-Identifier: AGPL-3.0-only
from flask import Blueprint

pam_bp = Blueprint('pam', __name__, template_folder='templates')

from .domain import pam_domain


@pam_bp.context_processor
def _inject_pam_translations():
    from flask_babel import gettext as _msg, ngettext as _nmsg

    def _gettext(string, **kw):
        raw = pam_domain.get_translations().gettext(string)
        if raw != string:
            return (raw % kw) if kw else raw
        return _msg(string, **kw)

    def _ngettext(string, plural, n):
        t = pam_domain.ngettext(string, plural, n)
        fallback = string if n == 1 else plural
        return t if t != fallback else _nmsg(string, plural, n)

    return {'_': _gettext, 'gettext': _gettext, 'ngettext': _ngettext}


from . import routes