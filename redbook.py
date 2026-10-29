# SPDX-License-Identifier: AGPL-3.0-only
"""Red Data Book of Ukraine 2021 categories (species.redbook2021).

Codes from the ministry order No. 29 of 19.01.2021, most threatened first;
the order is also the sort order wherever a list is sorted by status.
"""
from .domain import _l

REDBOOK_CODES = ('extinct', 'extinct_in_wild', 'endangered', 'vulnerable',
                 'rare', 'not_evaluated', 'data_deficient')
REDBOOK_LABELS = {
    'extinct': _l('зниклий'),
    'extinct_in_wild': _l('зниклий в природі'),
    'endangered': _l('зникаючий'),
    'vulnerable': _l('вразливий'),
    'rare': _l('рідкісний'),
    'not_evaluated': _l('неоцінений'),
    'data_deficient': _l('недостатньо відомий'),
}
REDBOOK_RANK = {code: i for i, code in enumerate(REDBOOK_CODES)}
