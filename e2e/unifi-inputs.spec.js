/*
 * Inputs page of TA-unifi-cloud, driven in a browser on a real Splunk:
 * create, disable, enable, edit and delete one input of every service in
 * globalConfig.json. After each step the input's stanza is read from
 * splunkd's raw configs/conf-inputs endpoint, which the page and the add-on's
 * REST handler never read, so a form that maps a label to the wrong key, or a
 * handler that drops a write, fails here.
 *
 * The inputs use a Cloud account aimed at the compose network's mock with a
 * dummy key: an enabled input polls the mock (and is refused), never
 * api.ui.com.
 */
const path = require('path');
const { test, expect } = require('@playwright/test');
const ucc = require('./ucc/ucc-helpers');

const GC = require(path.join(__dirname, '..', 'globalConfig.json'));
const APP = GC.meta.name;
const ROOT = GC.meta.restRoot.toLowerCase();
const PAGE_TITLE = GC.pages.inputs.title;
const ACCOUNT_NAME = `e2e_inputs_acct_${Date.now().toString(36)}`;

// Visible label -> inputs.conf key per service, written out here, not looked up
// from globalConfig.json by field name: a lookup by field would follow a
// remapped field and agree with the bug.
const SITE_MANAGER = { name: 'Name', interval: 'Interval', account: 'Account to use', index: 'Index' };
const FIELDS = {
    unifi_collect_hosts: SITE_MANAGER,
    unifi_collect_sites: SITE_MANAGER,
    unifi_collect_devices: SITE_MANAGER,
    unifi_network: SITE_MANAGER,
};
// Text values typed into the form; the account is picked from its select.
const VALUES = {
    unifi_collect_hosts: { create: { interval: '3600', index: 'main' }, edit: { interval: '7200' } },
    unifi_collect_sites: { create: { interval: '3600', index: 'main' }, edit: { interval: '7200' } },
    unifi_collect_devices: { create: { interval: '3600', index: 'main' }, edit: { interval: '7200' } },
    unifi_network: { create: { interval: '600', index: 'main' }, edit: { interval: '900' } },
};

let rest;
test.beforeAll(async () => {
    rest = await ucc.restClient();
    // Setup only, through the add-on's handler so the key is stored the UCC way.
    const r = await rest.post(`/servicesNS/nobody/${APP}/${ROOT}_account`, {
        name: ACCOUNT_NAME, connection_type: 'cloud', api_key: 'e2e-dummy-key', url: 'https://mock:8443', verify_ssl: '0',
    });
    expect([200, 201], `create account: ${r.status} ${r.body.slice(0, 300)}`).toContain(r.status);
});
test.afterAll(async () => {
    await rest.del(`/servicesNS/nobody/${APP}/${ROOT}_account/${ACCOUNT_NAME}`);
    await rest.dispose();
});
test.beforeEach(async ({ page }) => ucc.login(page));

test('every input service has test values and labels', () => {
    const services = GC.pages.inputs.services.map((s) => s.name).sort();
    expect(services).toEqual(Object.keys(VALUES).sort());
    expect(services).toEqual(Object.keys(FIELDS).sort());
});

/** Fill the create form: text fields by label, then the account select. The
 *  Index control is a combo box; Tab commits it and closes its menu. */
async function fillInput(page, d, label, values) {
    for (const [f, v] of Object.entries(values)) {
        await ucc.fillText(d, label(f), v);
        await ucc.control(d, label(f)).press('Tab');
    }
}

for (const service of GC.pages.inputs.services) {
    const label = (key) => FIELDS[service.name][key];
    const stanza = (name) => rest.inputStanza(APP, service.name, name);

    test(`${service.name}: create, disable, enable, edit and delete through the page`, async ({ page }) => {
        const errs = ucc.watchAppErrors(page, APP);
        const name = `e2e_${service.name}_${Date.now().toString(36)}`;
        const { create, edit } = VALUES[service.name];
        try {
            await ucc.gotoUccPage(page, APP, 'inputs', PAGE_TITLE);

            // Create
            const d = await ucc.openCreateInput(page, service.title);
            await fillInput(page, d, label, { name, ...create });
            await ucc.selectOption(page, d, label('account'), ACCOUNT_NAME);
            await ucc.submitDialog(page, 'Add');
            await expect(ucc.inputRow(page, name)).toBeVisible();
            const made = await ucc.expectConf(() => stanza(name), (c) => c !== null, `[${service.name}://${name}] created`);
            const want = { ...create, account: ACCOUNT_NAME };
            for (const [f, v] of Object.entries(want)) expect(made[f], `created ${f}`).toBe(v);
            expect(ucc.isTrue(made.disabled), 'a new input starts enabled').toBe(false);

            // Disable, then enable
            await ucc.toggleRow(page, name);
            await ucc.expectConf(() => stanza(name), (c) => c && ucc.isTrue(c.disabled), 'disabled after the switch');
            await ucc.toggleRow(page, name);
            await ucc.expectConf(() => stanza(name), (c) => c && !ucc.isTrue(c.disabled), 'enabled after the switch');

            // Edit: the dialog opens with what was saved, and the update lands
            await ucc.rowAction(page, name, 'Edit');
            const e = ucc.dialog(page);
            await expect(e).toBeVisible();
            for (const [f, v] of Object.entries(create)) expect(await ucc.readText(e, label(f)), `edit shows ${f}`).toBe(v);
            await expect(ucc.controlGroup(e, label('account'))).toContainText(ACCOUNT_NAME);
            await fillInput(page, e, label, edit);
            await ucc.submitDialog(page, 'Update');
            const after = await ucc.expectConf(() => stanza(name),
                (c) => c && Object.entries(edit).every(([f, v]) => c[f] === v), 'edited values saved');
            // Whole stanza: the fields not edited must survive the update.
            for (const [f, v] of Object.entries({ ...want, ...edit })) expect(after[f], `after edit ${f}`).toBe(v);

            // Delete
            await ucc.rowAction(page, name, 'Delete');
            await ucc.submitDialog(page, 'Delete');
            await expect(ucc.inputRow(page, name)).toHaveCount(0);
            await ucc.expectConf(() => stanza(name), (c) => c === null, `[${service.name}://${name}] removed`);
            errs.assertNone();
        } finally {
            await rest.deleteInput(APP, service.name, name);
        }
    });

    test(`${service.name}: an invalid name is refused and nothing is written`, async ({ page }) => {
        const bad = '9_starts_with_a_digit';
        await ucc.gotoUccPage(page, APP, 'inputs', PAGE_TITLE);
        const d = await ucc.openCreateInput(page, service.title);
        await fillInput(page, d, label, { name: bad, ...VALUES[service.name].create });
        await ucc.selectOption(page, d, label('account'), ACCOUNT_NAME);
        await d.getByRole('button', { name: 'Add', exact: true }).click();
        const msg = service.entity.find((x) => x.field === 'name').validators[0].errorMsg;
        await expect(d.getByText(msg).first()).toBeVisible();
        await page.waitForTimeout(3000);
        expect(await stanza(bad)).toBeNull();
        await d.getByRole('button', { name: 'Cancel', exact: true }).click();
    });
}
