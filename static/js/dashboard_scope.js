/* SPDX-License-Identifier: AGPL-3.0-only */
window.pamDashboardScope = (() => {
    const regionPrefix = 'ecoregion:';

    function appendParams(params, selection) {
        const institutionIds = [];
        for (const value of selection || []) {
            if (value.startsWith(regionPrefix)) {
                params.append('ecoregion', value.slice(regionPrefix.length));
            } else if (value) {
                institutionIds.push(value);
            }
        }
        if (institutionIds.length) params.set('institution_id', institutionIds.join(','));
    }

    function readParams(params) {
        const institutionIds = (params.get('institution_id') || '').split(',').filter(Boolean);
        return institutionIds.concat(params.getAll('ecoregion').map(key => regionPrefix + key));
    }

    function refresh(select, institutions, regions = [], selection) {
        const element = select[0];
        const selected = selection || [];
        element.replaceChildren(new Option('', ''));
        for (const institution of institutions) {
            element.append(new Option(institution.text, institution.id));
        }

        const regionGroup = document.createElement('optgroup');
        regionGroup.label = element.dataset.ecoregionLabel;
        const available = new Set();
        for (const region of regions) {
            const value = regionPrefix + region.id;
            available.add(value);
            regionGroup.append(new Option(region.text, value));
        }
        // Keep a linked region selected even when it has no visible recordings.
        for (const value of selected) {
            if (value.startsWith(regionPrefix) && !available.has(value)) {
                regionGroup.append(new Option(value.slice(regionPrefix.length), value));
            }
        }
        if (regionGroup.children.length) element.append(regionGroup);
        select.val(selected).trigger('change.select2');
    }

    return { appendParams, readParams, refresh };
})();
