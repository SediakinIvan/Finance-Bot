// Run against the exact unchanged Apps Script and the existing archive fixture.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const fixture={require,console};
vm.runInNewContext(fs.readFileSync('tests/test_archive_script.cjs','utf8')+'\nglobalThis.archiveFixture=environment;',fixture);
const e=fixture.archiveFixture(['no']);
const parent=e.sheets.get(e.api.FB.categories).rows[1];
const child=e.sheets.get(e.api.FB.children).rows[1];
parent[3]=false;parent[4]='2026-10';child[3]=false;
assert.throws(()=>e.scope.saveMonthlyReport(),/after deactivation/);
// These are the exact persisted registry fields produced by reactivate.
parent[3]=true;parent[4]='';child[3]=true;
e.scope.refreshFinanceBotRows();
e.scope.saveMonthlyReport();
assert.equal(e.sheets.get(e.api.FB.archive).rows[1][4],100);
assert.equal(e.sheets.get(e.api.FB.archive).rows[0].length,5);
assert.equal(e.sheets.get(e.api.FB.ledger).rows[1][3],100);
assert.equal(e.sheets.get(e.api.FB.ledger).rows[1][9],'valid');
console.log('Apps Script reactivation: existing script accepts restored IDs and archive reuses the original category column');
