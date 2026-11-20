const assert=require('node:assert/strict'),crypto=require('node:crypto');
const {environment}=require('./test_archive_script.cjs');
const BOOK='1jaEzmJPqYzxSp-HtDw-zwt_kPqSEs4enBrkG-Lc-UzA',KEY='offline-test-key-'.repeat(4);
const PRODUCTION='1I6_HFCMVmxOD5buP6RdhtfJHld66tj7oMGArpW4AEC4';
let seq=10;const uuid=()=>`10000000-0000-4000-8000-${String(seq++).padStart(12,'0')}`;
function remote(book=BOOK){
  const e=environment();let opens=0;
  e.ss.getId=()=>book;e.sheets.get(e.api.FB.meta).rows.find(r=>r[0]==='spreadsheet_id')[1]=book;
  const values={FB_ARCHIVE_KEY:KEY,FB_ARCHIVE_OWNER_ID:'1',FB_ARCHIVE_SHEET_ID:book};
  const props={getProperty:k=>values[k]??null,setProperty:(k,v)=>{values[k]=v;},getProperties:()=>({...values}),deleteProperty:k=>{delete values[k];}};
  e.scope.PropertiesService={getScriptProperties:()=>props};
  e.scope.Utilities.DigestAlgorithm={SHA_256:'sha256'};e.scope.Utilities.Charset={UTF_8:'utf8'};
  e.scope.Utilities.computeDigest=(_alg,s)=>[...crypto.createHash('sha256').update(s).digest()];
  e.scope.Utilities.computeHmacSha256Signature=(s,k)=>[...crypto.createHmac('sha256',k).update(s).digest()];
  e.scope.SpreadsheetApp.openById=id=>{assert.equal(id,book);opens++;return e.ss;};
  e.scope.SpreadsheetApp.getUi=()=>{throw Error('No UI in web app');};
  e.scope.SpreadsheetApp.getActiveSpreadsheet=()=>{throw Error('No active document');};
  e.scope.LockService.getScriptLock=()=>({waitLock(){},releaseLock(){},tryLock(){return true;}});
  e.scope.LockService.getDocumentLock=()=>{throw Error('No document lock');};
  e.scope.ContentService={MimeType:{JSON:'json'},createTextOutput:text=>({text,setMimeType(){return this;}})};
  function send(action,id,changes={},signature){
    const payload=JSON.stringify({action,request_id:id,owner_id:1,spreadsheet_id:book,timestamp:Date.now(),nonce:uuid(),...changes});
    const signed=signature??crypto.createHmac('sha256',KEY).update(payload).digest('hex');
    return JSON.parse(e.scope.doPost({postData:{contents:JSON.stringify({payload,signature:signed})}}).text);
  }
  return {...e,send,values,opens:()=>opens};
}
for(const [book,otherBook] of [[BOOK,PRODUCTION],[PRODUCTION,BOOK]]){
  const e=remote(book),id=uuid();
  assert.equal(e.send('status',uuid(),{spreadsheet_id:otherBook}).code,'unauthorized');
  assert.equal(e.send('status',uuid()).status,'not_found');
  assert.equal(e.opens(),0);
  assert.equal(e.send('preview',id).status,'preview');
  assert.equal(e.send('archive',id).status,'archived');
  assert.equal(e.send('cancel',id).status,'kept');
  assert.equal(e.cleared.length,0);
}
{
  const e=remote(),id=uuid();const preview=e.send('preview',id);assert.equal(preview.status,'preview');
  assert.equal(e.cleared.length,0);assert.equal(e.send('archive',id).status,'archived');
  assert.equal(e.cleared.length,0);assert.equal(e.send('archive',id).status,'archived');
  assert.equal(e.send('clear',id).status,'complete');assert.equal(e.sheets.get(e.api.FB.analytics).rows[0][3],12);
  // New month data must survive every old callback/status request.
  e.sheets.get(e.api.FB.ledger).rows.push(['01.12.2026','Продукты','Магазин',500,'Расход']);
  assert.equal(e.send('clear',id).status,'complete');assert.equal(e.send('status',id).status,'complete');
  assert.equal(e.sheets.get(e.api.FB.ledger).rows.at(-1)[3],500);assert.equal(e.sheets.get(e.api.FB.analytics).rows[0][3],12);
}
{
  const e=remote(),id=uuid();e.send('preview',id);e.send('archive',id);
  assert.equal(e.send('cancel',id).status,'kept');assert.equal(e.sheets.get(e.api.FB.ledger).rows[1][3],100);
  assert.equal(e.sheets.get(e.api.FB.analytics).rows[0][3],11);
}
{
  const e=remote(),id=uuid();e.send('preview',id);e.send('archive',id);
  e.sheets.get(e.api.FB.ledger).rows[1][3]=200;
  assert.equal(e.send('clear',id).status,'stale');assert.equal(e.sheets.get(e.api.FB.ledger).rows[1][3],200);
}
{
  const e=remote();for(const change of [{timestamp:0},{owner_id:2},{spreadsheet_id:'production'},{action:'eval'}])assert.equal(e.send('preview',uuid(),change).code,'unauthorized');
  assert.equal(e.send('preview',uuid(),{},'0'.repeat(64)).code,'unauthorized');assert.equal(e.opens(),0);
}
{
  const e=remote(),id=uuid();e.send('preview',id);e.send('archive',id);
  const original=e.scope.fbSetMeta;let fail=true;
  e.scope.fbSetMeta=(ss,changes)=>{if(changes.access_mode==='open'&&fail){fail=false;throw Error('lost response');}return original(ss,changes);};
  const result=e.send('clear',id);assert.equal(result.status,'complete_pending');
  assert.equal(e.send('status',id).status,'complete');assert.equal(e.sheets.get(e.api.FB.analytics).rows[0][3],12);
}
{
 const e=remote(),id=uuid();e.send('preview',id);e.send('archive',id);
 const original=e.scope.fbClearArchive;
 e.scope.fbClearArchive=(ss,c,stage)=>{const sheet=e.sheets.get(e.api.FB.ledger);sheet.rows=sheet.rows.slice(0,1);throw Error('stopped after clearing ledger');};
 assert.equal(e.send('clear',id).status,'clearing');e.scope.fbClearArchive=original;
 assert.equal(e.send('status',id).status,'complete');assert.equal(e.sheets.get(e.api.FB.analytics).rows[0][3],12);
}
console.log('Apps Script web archive: UI-less execution, signatures/scope, confirmations, stale data, repeats and clear recovery passed');
