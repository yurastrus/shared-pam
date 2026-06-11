import os
from flask_babel import Domain

_here = os.path.dirname(os.path.abspath(__file__))
pam_domain = Domain(
    translation_directories=[os.path.join(_here, 'translations')],
    domain='pam',
)

_ = pam_domain.gettext
_l = pam_domain.lazy_gettext
