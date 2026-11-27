/*
 * Configuration page of TA-unifi-cloud, driven in a browser on a real Splunk.
 * Each save is read back from splunkd's raw configs/conf-* endpoint and the
 * API key from storage/passwords, neither of which the page or the add-on's
 * REST handlers read.
 *
 * Accounts point at the compose network's mock (https://mock:8443) with a
 * dummy key, so nothing here reaches api.ui.com or a real console.
 */
const path = require('path');
const { test, expect } = require('@playwright/test');
const ucc = require('./ucc/ucc-helpers');

const GC = require(path.join(__dirname, '..', 'globalConfig.json'));
const APP = GC.meta.name;
const ROOT = GC.meta.restRoot.toLowerCase();
const SETTINGS_CONF = `${ROOT}_settings`;
const ACCOUNT_CONF = `${ROOT}_account`;
const PAGE_TITLE = GC.pages.configuration.title;
const accountTab = GC.pages.configuration.tabs.find((t) => t.name === 'account');
const entity = (field) => accountTab.entity.find((e) => e.field === field);

// Visible label -> account conf key, written out rather than looked up by field
// in globalConfig.json, so a remapped field cannot move the lookup with it.
const ACCOUNT = {
    name: 'Name', connection_type: 'Connection type', api_key: 'API key', url: 'Console URL',
    verify_ssl: 'Verify TLS certificate',
};
const LOCAL_OPTION = 'Local console (UniFi Network API key)';

let rest;
let savedLogging;

test.beforeAll(async () => {
    rest = await ucc.restClient();
    savedLogging = await rest.confStanza(APP, SETTINGS_CONF, 'logging');
});

test.afterAll(async () => {
    await rest.setConf(APP, SETTINGS_CONF, 'logging', { loglevel: (savedLogging && savedLogging.loglevel) || 'INFO' });
    await rest.dispose();
});

test.beforeEach(async ({ page }) => {
    await ucc.login(page);
});

async function openAccounts(page) {
    await ucc.gotoUccPage(page, APP, 'configuration', PAGE_TITLE);
    await ucc.openTab(page, accountTab.title);
}

test('Configuration page loads every tab without app errors', async ({ page }) => {
    const errs = ucc.watchAppErrors(page, APP);
    await ucc.gotoUccPage(page, APP, 'configuration', PAGE_TITLE);
    for (const t of GC.pages.configuration.tabs) {
        const title = t.title || (t.type === 'loggingTab' ? 'Logging' : t.name);
        await ucc.openTab(page, title);
        if (t.table) {
            await expect(page.getByRole('button', { name: 'Add', exact: true })).toBeVisible();
        } else if (t.type === 'loggingTab') {
            await expect(page.getByText('Log level').first()).toBeVisible();
        }
    }
    errs.assertNone();
});

test('Logging tab saves the chosen level and it survives a reload', async ({ page }) => {
    await ucc.gotoUccPage(page, APP, 'configuration', PAGE_TITLE);
    await ucc.openTab(page, 'Logging');
    const target = savedLogging && savedLogging.loglevel === 'DEBUG' ? 'WARNING' : 'DEBUG';
    await ucc.selectOption(page, page, 'Log level', target);
    expect(await ucc.saveConfigTab(page)).toBe(200);
    await ucc.expectConf(() => rest.confStanza(APP, SETTINGS_CONF, 'logging'),
        (c) => c && c.loglevel === target, `[logging] loglevel=${target} after Save`);
    await page.reload();
    await ucc.openTab(page, 'Logging');
    await expect(ucc.controlGroup(page, 'Log level')).toContainText(target);
});

test('account: create, edit and delete; the API key never comes back and lives in storage/passwords', async ({ page }) => {
    const errs = ucc.watchAppErrors(page, APP);
    const name = `e2e_acct_${Date.now().toString(36)}`;
    // Dummy, unique per run so a hit in the DOM cannot be anything else.
    const secret = `e2e-dummy-key-${Math.random().toString(36).slice(2)}`;
    const secretCheck = { app: APP, conf: ACCOUNT_CONF, stanza: name, field: 'api_key', secret,
        handlerPath: `/servicesNS/nobody/${APP}/${ROOT}_account/${name}` };
    const stanza = () => rest.confStanza(APP, ACCOUNT_CONF, name);
    try {
        await openAccounts(page);

        // Create: Local console, mock URL, TLS verification off.
        await page.getByRole('button', { name: 'Add', exact: true }).click();
        const d = ucc.dialog(page);
        await expect(d).toBeVisible();
        await ucc.fillText(d, ACCOUNT.name, name);
        await ucc.selectOption(page, d, ACCOUNT.connection_type, LOCAL_OPTION);
        await ucc.fillText(d, ACCOUNT.api_key, secret);
        await ucc.fillText(d, ACCOUNT.url, 'https://mock:8443');
        await ucc.setCheckbox(d, ACCOUNT.verify_ssl, false);
        await ucc.submitDialog(page, 'Add');
        await expect(ucc.inputRow(page, name)).toBeVisible();

        const made = await ucc.expectConf(stanza, (c) => c !== null, `[${name}] created`);
        expect(made.connection_type, 'connection_type').toBe('local');
        expect(made.url, 'url').toBe('https://mock:8443');
        expect(ucc.isTrue(made.verify_ssl), 'verify_ssl unticked').toBe(false);
        await ucc.expectSecretNotEchoed(page, rest, secretCheck);

        // After a reload, on the table and in the edit dialog.
        await page.reload();
        await ucc.openTab(page, accountTab.title);
        await expect(ucc.inputRow(page, name)).toBeVisible();
        expect.soft(await ucc.domHolds(page, secret), 'API key in the DOM after reload').toBe(false);

        // Edit: the dialog shows what was saved (never the key), the update lands
        // and the key is kept, not replaced by the mask the dialog shows.
        await ucc.rowAction(page, name, 'Edit');
        const e = ucc.dialog(page);
        await expect(e).toBeVisible();
        expect(await ucc.readText(e, ACCOUNT.url), 'edit shows url').toBe('https://mock:8443');
        expect.soft(await ucc.domHolds(page, secret), 'API key in the edit dialog').toBe(false);
        await ucc.fillText(e, ACCOUNT.url, 'https://mock:8443/e2e');
        await ucc.submitDialog(page, 'Update');
        const after = await ucc.expectConf(stanza, (c) => c && c.url === 'https://mock:8443/e2e', 'edited url saved');
        // Whole stanza: the fields not edited survive the update.
        expect(after.connection_type, 'after edit connection_type').toBe('local');
        expect(ucc.isTrue(after.verify_ssl), 'after edit verify_ssl').toBe(false);
        await ucc.expectSecretNotEchoed(page, rest, secretCheck);

        // Delete: stanza and credential both gone.
        await ucc.rowAction(page, name, 'Delete');
        await ucc.submitDialog(page, 'Delete');
        await expect(ucc.inputRow(page, name)).toHaveCount(0);
        await ucc.expectConf(stanza, (c) => c === null, `[${name}] removed`);
        await ucc.expectSecretGone(rest, { app: APP, conf: ACCOUNT_CONF, stanza: name });
        errs.assertNone();
    } finally {
        await rest.del(`/servicesNS/nobody/${APP}/${ROOT}_account/${name}`);
    }
});

test('account: a non-https Console URL and an invalid name are refused and nothing is written', async ({ page }) => {
    const name = `e2e_bad_${Date.now().toString(36)}`;
    await openAccounts(page);
    await page.getByRole('button', { name: 'Add', exact: true }).click();
    const d = ucc.dialog(page);
    await ucc.fillText(d, ACCOUNT.name, name);
    await ucc.fillText(d, ACCOUNT.api_key, 'e2e-dummy-refused');
    await ucc.fillText(d, ACCOUNT.url, 'http://mock:8443');
    await d.getByRole('button', { name: 'Add', exact: true }).click();
    await expect(d.getByText(entity('url').validators[0].errorMsg).first()).toBeVisible();

    await ucc.fillText(d, ACCOUNT.url, 'https://mock:8443');
    await ucc.fillText(d, ACCOUNT.name, '9_starts_with_a_digit');
    await d.getByRole('button', { name: 'Add', exact: true }).click();
    await expect(d.getByText(entity('name').validators[0].errorMsg).first()).toBeVisible();
    // Give a wrongly accepted save time to land before checking it did not.
    await page.waitForTimeout(3000);
    expect(await rest.confStanza(APP, ACCOUNT_CONF, name)).toBeNull();
    expect(await rest.confStanza(APP, ACCOUNT_CONF, '9_starts_with_a_digit')).toBeNull();
    await d.getByRole('button', { name: 'Cancel', exact: true }).click();
});
