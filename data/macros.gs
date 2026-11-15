/* FinanceBot v2: replace Apps Script source only after migration on a COPY.
 * Original v1 source is retained in data/macros-v1.gs.
 */
const FB = {
  meta: '_financebot_meta', categories: '_financebot_categories',
  children: '_financebot_subcategories', history: '_financebot_category_history',
  snapshots: '_financebot_row_snapshots', columns: '_financebot_archive_columns',
  labels: '_financebot_archive_labels', detail: '_financebot_detail',
  ledger: 'Траты', analytics: 'Аналитика', archive: 'Архив',
  header: ['Дата','Категория','Подкатегория','Сумма','Доход/Расход','Примечание',
    'category_id','subcategory_id','transaction_id','validation_state','report_date'],
  snapshotHeader: ['transaction_id','category_id','subcategory_id',
    'category_label','subcategory_label','transaction_type']
};
function fbName(x) { return String(x).trim().replace(/\s+/g, ' ').toLocaleLowerCase('ru'); }
function fbUUID(x) { return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(x); }
function fbDate(value) {
  if (typeof value === 'number' && Number.isFinite(value) && value >= 1) return Math.floor(value);
  if (value instanceof Date && Number.isFinite(value.getTime()))
    return Math.round((Date.UTC(value.getFullYear(),value.getMonth(),value.getDate())-Date.UTC(1899,11,30))/86400000);
  if (typeof value !== 'string') throw new Error('invalid_date');
  let m = value.trim().match(/^(\d{2})\.(\d{2})\.(\d{4})$/), y, month, day;
  if (m) { y=+m[3]; month=+m[2]; day=+m[1]; }
  else {
    m=value.trim().match(/^(\d{4})-(\d{2})-(\d{2})$/);
    if (!m) throw new Error('invalid_date');
    y=+m[1]; month=+m[2]; day=+m[3];
  }
  const date=new Date(0); date.setUTCFullYear(y,month-1,day); date.setUTCHours(0,0,0,0);
  if (y<1 || y>9999 || date.getUTCFullYear()!==y || date.getUTCMonth()!==month-1 || date.getUTCDate()!==day)
    throw new Error('invalid_date');
  return Math.round((date.getTime()-Date.UTC(1899,11,30))/86400000);
}
function fbAmount(x) {
  if (typeof x==='boolean' || !/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i.test(String(x).trim()) || !Number.isFinite(Number(x)) || Number(x)<=0)
    throw new Error('invalid_amount');
  return Number(x);
}
function fbRows(ss,name) {
  const sheet=ss.getSheetByName(name);
  if (!sheet) throw new Error('Missing v2 sheet: '+name);
  return sheet.getDataRange().getValues().map(row=>row.map(value=>
    value instanceof Date ? Utilities.formatDate(value,ss.getSpreadsheetTimeZone(),'yyyy-MM-dd') : value));
}
function fbEnsureSize(sheet,rows,columns){
  if(rows>sheet.getMaxRows())sheet.insertRowsAfter(sheet.getMaxRows(),rows-sheet.getMaxRows());
  if(columns>sheet.getMaxColumns())sheet.insertColumnsAfter(sheet.getMaxColumns(),columns-sheet.getMaxColumns());
}
function fbObjects(rows,required) {
  if (!rows.length) throw new Error('Missing header');
  const names=rows[0].map(String);
  if (new Set(names).size!==names.length || required.some(x=>!names.includes(x))) throw new Error('Invalid header');
  return rows.slice(1).filter(r=>r.some(x=>String(x).trim()!=='')).map(r=>Object.fromEntries(names.map((n,i)=>[n,r[i]===undefined?'':r[i]])));
}
function fbMetadata(ss,ready) {
  const rows=fbObjects(fbRows(ss,FB.meta),['key','value']);
  if (new Set(rows.map(x=>x.key)).size!==rows.length) throw new Error('Duplicate metadata');
  const m=Object.fromEntries(rows.map(x=>[x.key,String(x.value)]));
  if (m.schema_version!=='2' || m.spreadsheet_id!==ss.getId() || !/^\d+$/.test(m.taxonomy_revision||''))
    throw new Error('Invalid schema or scope');
  if (ready && (m.migration_state!=='ready' || m.access_mode!=='open' || m.pending_request_id))
    throw new Error('Finish migration or recovery');
  return m;
}
function fbSetMeta(ss,changes) {
  const s=ss.getSheetByName(FB.meta), rows=s.getDataRange().getValues();
  rows.slice(1).forEach(r=>{if(Object.prototype.hasOwnProperty.call(changes,r[0])) r[1]=changes[r[0]];});
  s.getRange(1,1,rows.length,2).setValues(rows); SpreadsheetApp.flush();
}
function fbRegistry(ss) {
  const categories=fbObjects(fbRows(ss,FB.categories),['category_id','type','name','active','deactivated_period']);
  const children=fbObjects(fbRows(ss,FB.children),['subcategory_id','current_category_id','name','active']);
  const history=fbObjects(fbRows(ss,FB.history),['category_id','subcategory_id']);
  const bool=x=>{if(!['true','false'].includes(String(x).toLowerCase())) throw new Error('Invalid activity'); return String(x).toLowerCase()==='true';};
  const ids=new Set(), names=new Set();
  categories.forEach(c=>{
    c.active=bool(c.active); const key=c.type+'\0'+fbName(c.name);
    if(!fbUUID(c.category_id)||ids.has(c.category_id)||names.has(key)||!fbName(c.name)||!['Доход','Расход'].includes(c.type))
      throw new Error('Invalid category');
    if(c.active===Boolean(c.deactivated_period)||(!c.active&&!/^\d{4}-(0[1-9]|1[0-2])$/.test(c.deactivated_period)))
      throw new Error('Invalid deactivation period');
    ids.add(c.category_id); names.add(key);
  });
  names.clear();
  children.forEach(c=>{
    c.active=bool(c.active); const key=c.current_category_id+'\0'+fbName(c.name);
    if(!fbUUID(c.subcategory_id)||ids.has(c.subcategory_id)||names.has(key)||!fbName(c.name)||!categories.some(p=>p.category_id===c.current_category_id))
      throw new Error('Invalid subcategory');
    ids.add(c.subcategory_id); names.add(key);
  });
  history.forEach(pair=>{
    const p=categories.find(x=>x.category_id===pair.category_id), c=children.find(x=>x.subcategory_id===pair.subcategory_id);
    if(!p||!c||p.type!==categories.find(x=>x.category_id===c.current_category_id).type) throw new Error('Invalid historical pair');
  });
  return {categories,children,history};
}
function fbReconcile(rows,registry,snapshots,uuid) {
  if(JSON.stringify(rows[0])!==JSON.stringify(FB.header)) throw new Error('Unsupported ledger layout');
  const counts=new Map(); rows.slice(1).forEach(r=>{if(r[8])counts.set(r[8],(counts.get(r[8])||0)+1);});
  const service=[], saved=[], errors=[];
  rows.slice(1).forEach((row,index)=>{
    const v=Array.from({length:6},(_,i)=>row[i]===undefined?'':row[i]);
    if(v.every(x=>String(x).trim()==='')){service.push(['','','','','']);return;}
    let id=String(row[8]||''), p='', c='', d='', state='valid';
    const old=snapshots.find(x=>x.transaction_id===id);
    try {
      if(id&&(!fbUUID(id)||counts.get(id)>1))throw new Error('duplicate_or_invalid_transaction_id');
      id=id||uuid();
      const unchanged=old&&fbName(v[1])===fbName(old.category_label)&&fbName(v[2])===fbName(old.subcategory_label)&&fbName(v[4])===fbName(old.transaction_type);
      if(unchanged){
        p=old.category_id;c=old.subcategory_id;
        if(row[6]!==p||row[7]!==c)throw new Error('identity_tampered');
        const child=registry.children.find(x=>x.subcategory_id===c);
        if(!registry.categories.some(x=>x.category_id===p)||!child||(child.current_category_id!==p&&!registry.history.some(x=>x.category_id===p&&x.subcategory_id===c)))
          throw new Error('invalid_category_pair');
      } else {
        const matches=registry.children.filter(child=>{
          const parent=registry.categories.find(x=>x.category_id===child.current_category_id);
          return child.active&&parent.active&&fbName(parent.type)===fbName(v[4])&&fbName(parent.name)===fbName(v[1])&&fbName(child.name)===fbName(v[2]);
        });
        if(matches.length!==1)throw new Error('invalid_category_pair');
        c=matches[0].subcategory_id;p=matches[0].current_category_id;
      }
      d=fbDate(v[0]);fbAmount(v[3]);
      if(String(v[5]).length>1000)throw new Error('note_too_long');
    } catch(error){
      state=error.message;errors.push(index+2);
      if(old&&!['duplicate_or_invalid_transaction_id','identity_tampered'].includes(state)){p=old.category_id;c=old.subcategory_id;}
    }
    service.push([p,c,id,state,d]);
    const identity=state==='valid'?[id,p,c,String(v[1]),String(v[2]),String(v[4])]:(old?FB.snapshotHeader.map(k=>old[k]):null);
    if(identity&&!saved.some(x=>x[0]===identity[0]))saved.push(identity);
  });
  return {service,saved,errors};
}
function fbSync(ss,registry) {
  const rows=fbRows(ss,FB.ledger), old=fbObjects(fbRows(ss,FB.snapshots),FB.snapshotHeader);
  const result=fbReconcile(rows,registry,old,()=>Utilities.getUuid());
  if(JSON.stringify(fbRows(ss,FB.ledger))!==JSON.stringify(rows))throw new Error('Concurrent ledger edit');
  if(result.service.length)ss.getSheetByName(FB.ledger).getRange(2,7,result.service.length,5).setValues(result.service);
  const s=ss.getSheetByName(FB.snapshots);s.clearContents();
  fbEnsureSize(s,result.saved.length+1,6);
  s.getRange(1,1,result.saved.length+1,6).setValues([FB.snapshotHeader,...result.saved]);
  return result;
}
function fbDropdowns(ss,registry) {
  const s=ss.getSheetByName(FB.ledger), rows=fbRows(ss,FB.ledger);
  const parents=registry.categories.filter(x=>x.active).map(x=>x.name), rules=[];
  const rule=values=>values.length?SpreadsheetApp.newDataValidation().requireValueInList(values,true).setAllowInvalid(false).build():null;
  for(let i=1;i<s.getMaxRows();i++){
    const row=rows[i]||[];
    const children=registry.children.filter(c=>c.active&&registry.categories.some(p=>p.active&&p.category_id===c.current_category_id&&fbName(p.name)===fbName(row[1]||''))).map(x=>x.name);
    rules.push([rule([...new Set([...parents,...(row[6]?[String(row[1])]:[])])]),
      rule([...new Set([...children,...(row[7]?[String(row[2])]:[])])].filter(Boolean))]);
  }
  if(rules.length)s.getRange(2,2,rules.length,2).setDataValidations(rules);
}
function onEdit(e) {
  if(!e||!e.range||![FB.ledger,FB.analytics].includes(e.range.getSheet().getName()))return;
  const lock=fbLock();if(!lock.tryLock(5000))return;
  try{fbMetadata(e.source,true);const r=fbRegistry(e.source);fbSync(e.source,r);fbDropdowns(e.source,r);}
  finally{lock.releaseLock();}
}
function refreshFinanceBotRows(){
  const ss=SpreadsheetApp.getActiveSpreadsheet(),lock=fbLock();lock.waitLock(30000);
  try{fbMetadata(ss,true);const r=fbRegistry(ss),result=fbSync(ss,r);fbDropdowns(ss,r);
    if(result.errors.length)throw new Error('Исправьте строки: '+result.errors.join(', '));}
  finally{lock.releaseLock();}
}
function verifyFinanceBotMigration(){
  const ss=SpreadsheetApp.getActiveSpreadsheet(),lock=fbLock();lock.waitLock(30000);
  try{
    const meta=fbMetadata(ss,false);
    if(meta.migration_state!=='applying'||meta.access_mode!=='maintenance')throw new Error('Unexpected migration state');
    const registry=fbRegistry(ss),result=fbSync(ss,registry);fbDropdowns(ss,registry);
    if(result.errors.length)throw new Error('Исправьте строки: '+result.errors.join(', '));
    SpreadsheetApp.flush();
    const proof={spreadsheet_id:ss.getId(),schema_version:2,taxonomy_revision:Number(meta.taxonomy_revision),verified:true};
    console.log(JSON.stringify(proof));
    return proof;
  }finally{lock.releaseLock();}
}
function fbPeriod(rows){
  const month=Number(rows[0][3]),year=Number(rows[1][3]);
  if(!Number.isInteger(month)||month<1||month>12||!Number.isInteger(year)||year<1||year>9999)throw new Error('Invalid reporting period');
  return {month,year,key:year+'-'+String(month).padStart(2,'0')};
}
function fbArchiveValues(rows,registry,period){
  let income=0,expenses=0;
  const totals=Object.fromEntries(registry.categories.filter(x=>x.type==='Расход').map(x=>[x.category_id,0]));
  rows.slice(1).forEach(row=>{
    if(!row.slice(0,6).some(x=>String(x).trim()))return;
    if(row[9]!=='valid')throw new Error('Invalid operation before archive');
    const serial=fbDate(row[0]),day=new Date(Date.UTC(1899,11,30)+serial*86400000);
    if(day.getUTCFullYear()!==period.year||day.getUTCMonth()+1!==period.month)throw new Error('Есть операции другого месяца; очистка отменена');
    const parent=registry.categories.find(x=>x.category_id===row[6]);
    if(!parent)throw new Error('Unknown category ID');
    if(parent.deactivated_period&&String(parent.deactivated_period)<period.key)throw new Error('Operation after deactivation');
    const amount=fbAmount(row[3]);
    if(parent.type==='Доход')income+=amount;else{expenses+=amount;totals[parent.category_id]+=amount;}
  });
  return {income,expenses,balance:income-expenses,totals};
}
function onOpen(){
  SpreadsheetApp.getUi().createMenu('Финансы ИФТ')
    .addItem('Сохранить месячный отчёт','saveMonthlyReport').addToUi();
}
function fbLock(){return LockService.getScriptLock?LockService.getScriptLock():LockService.getDocumentLock();}
function fbArchiveContext(ss){
  const before=fbMetadata(ss,false);
  const registry=fbRegistry(ss),synced=fbSync(ss,registry);
  if(synced.errors.length)throw new Error('Исправьте строки: '+synced.errors.join(', '));
  SpreadsheetApp.flush();
  const analytic=ss.getSheetByName(FB.analytics),view=analytic.getDataRange().getValues();
  const period=fbPeriod(view),rows=fbRows(ss,FB.ledger),totals=fbArchiveValues(rows,registry,period);
  const sourceLabels=fbRows(ss,FB.labels),liveLabels=sourceLabels.slice(1).filter(row=>row[0]===FB.analytics);
  if(!liveLabels.length)throw new Error('Dashboard label index is missing; finish report migration first');
  if(view[0][5]!=='ready'||!Number.isFinite(Number(view[2][1]))||!Number.isFinite(Number(view[3][1]))||
    Math.abs(Number(view[2][1])-totals.expenses)>0.005||Math.abs(Number(view[3][1])-totals.income)>0.005||
    !Number.isFinite(Number(view[4][1]))||Math.abs(Number(view[4][1])-totals.balance)>0.005)throw new Error('Итоги аналитики не совпадают с операциями');
  const months=['Январь','Февраль','Март','Апрель','Май','Июнь','Июль','Август','Сентябрь','Октябрь','Ноябрь','Декабрь'];
  const name=months[period.month-1]+' '+period.year,archiveName='Архив ('+name+')';
  let summary=ss.getSheetByName(FB.archive);
  const found=summary&&summary.getLastRow()>1?summary.getRange(2,1,summary.getLastRow()-1,1).getValues().findIndex(r=>r[0]===name):-1;
  const old=ss.getSheetByName(archiveName);
  return {before,registry,view,period,rows,totals,sourceLabels,liveLabels,name,archiveName,summary,found,old};
}
function fbArchiveUnchanged(ss,c){
  return JSON.stringify(fbRows(ss,FB.ledger))===JSON.stringify(c.rows)&&fbMetadata(ss,false).taxonomy_revision===c.before.taxonomy_revision&&fbPeriod(fbRows(ss,FB.analytics)).key===c.period.key;
}
function fbSaveArchive(ss,c,touched=()=>{}){
  const {registry,view,period,rows,totals,sourceLabels,liveLabels,name,archiveName,found,old}=c;
  let {summary}=c;
  const analytic=ss.getSheetByName(FB.analytics);
    const cs=ss.getSheetByName(FB.columns),columns=fbObjects(fbRows(ss,FB.columns),['category_id','column']);
    let last=Math.max(4,...columns.map(x=>Number(x.column)));
    registry.categories.filter(x=>x.type==='Расход').forEach(c=>{if(!columns.some(x=>x.category_id===c.category_id))columns.push({category_id:c.category_id,column:++last});});
    const values=Array(last).fill(0),headers=Array(last).fill('');
    values.splice(0,4,name,totals.income,totals.expenses,totals.balance);headers.splice(0,4,'Месяц','Доход','Расход','Остаток');
    columns.forEach(item=>{
      const parent=registry.categories.find(x=>x.category_id===item.category_id);
      if(!parent)throw new Error('Unknown archived category');
      headers[+item.column-1]=parent.name;
      values[+item.column-1]=parent.deactivated_period&&String(parent.deactivated_period)<period.key?0:totals.totals[parent.category_id];
    });
    touched();
    summary=summary||ss.insertSheet(FB.archive);
    if(last>summary.getMaxColumns())summary.insertColumnsAfter(summary.getMaxColumns(),last-summary.getMaxColumns());
    summary.getRange(1,1,1,last).setValues([headers]);
    const target=found>=0?found+2:Math.max(2,summary.getLastRow()+1);
    fbEnsureSize(summary,target,last);
    summary.getRange(target,1,1,last).setValues([values]);
    cs.clearContents();fbEnsureSize(cs,columns.length+1,2);cs.getRange(1,1,columns.length+1,2).setValues([['category_id','column'],...columns.map(x=>[x.category_id,+x.column])]);
    // IDs belong in the protected label index, not the visible monthly report.
    // Keep D1:D2 (reporting period), but remove summary IDs in D8 and below.
    const snapshotView=view.map((row,i)=>row.map((value,j)=>i>=7&&j===3?'':value));
    const temp=analytic.copyTo(ss);temp.setName('_pending_'+Utilities.getUuid().slice(0,8));
    // Freezing formulas can shrink getDataRange() when trailing formulas
    // evaluate to empty strings. Write and verify the same explicit rectangle.
    const snapshotRange=temp.getRange(1,1,snapshotView.length,snapshotView[0].length);
    snapshotRange.setValues(snapshotView);
    if(JSON.stringify(summary.getRange(target,1,1,last).getValues()[0])!==JSON.stringify(values))throw new Error('Archive readback mismatch');
    if(JSON.stringify(snapshotRange.getValues())!==JSON.stringify(snapshotView))throw new Error('Snapshot readback mismatch');
    // Freeze the readable dashboard as-is. Never overlay the internal detail
    // table on its blocks/charts. Rebind chart sources to the frozen copy.
    temp.getCharts().forEach(chart=>{
      const ranges=chart.getRanges();
      // Native copyTo already redirects charts to the copied sheet. Rebuilding
      // all correctly copied charts is slow and can stall archive completion.
      if(ranges.every(range=>range.getSheet().getSheetId()===temp.getSheetId()))return;
      const builder=chart.modify().clearRanges();
      ranges.forEach(range=>builder.addRange(temp.getRange(range.getA1Notation())));
      temp.updateChart(builder.build());
    });
    if(old)ss.deleteSheet(old);temp.setName(archiveName);
    const labels=sourceLabels.filter((r,i)=>i===0||r[0]!==archiveName);
    liveLabels.forEach(row=>labels.push([archiveName,...row.slice(1)]));
    const ls=ss.getSheetByName(FB.labels);ls.clearContents();fbEnsureSize(ls,labels.length,5);ls.getRange(1,1,labels.length,5).setValues(labels);
  return ss.getSheetByName(archiveName);
}
function fbClearArchive(ss,c,stage=()=>{}){
  const {period}=c,analytic=ss.getSheetByName(FB.analytics);
    stage('clearing');
    const ledger=ss.getSheetByName(FB.ledger);
    if(ledger.getLastRow()>1)ledger.getRange(2,1,ledger.getLastRow()-1,FB.header.length).clearContent();
    const identities=ss.getSheetByName(FB.snapshots);identities.clearContents();identities.getRange(1,1,1,6).setValues([FB.snapshotHeader]);
    stage('advancing');
    if(period.month===12)analytic.getRange(1,4,2,1).setValues([[1],[period.year+1]]);
    else analytic.getRange(1,4).setValues([[period.month+1]]);
    stage('complete');
}
function saveMonthlyReport(){
  const ss=SpreadsheetApp.getActiveSpreadsheet();
  let ui;
  try{ui=SpreadsheetApp.getUi();}
  catch(error){throw new Error('Нет доступа к интерфейсу таблицы. Перезагрузите ИФТ-таблицу и выберите «Финансы ИФТ → Сохранить месячный отчёт». Данные не изменены.');}
  const lock=fbLock();fbMetadata(ss,true);lock.waitLock(30000);
  let cleanExit=false,archiveTouched=false;
  try{
    fbSetMeta(ss,{access_mode:'maintenance'});
    const c=fbArchiveContext(ss);
    if((c.found>=0||c.old)&&ui.alert('Заменить архив?','Архив за '+c.name+' существует. Заменить?',ui.ButtonSet.YES_NO)!==ui.Button.YES){cleanExit=true;return;}
    lock.waitLock(30000);
    if(!fbArchiveUnchanged(ss,c))throw new Error('Данные изменились после подтверждения');
    fbSaveArchive(ss,c,()=>{archiveTouched=true;});
    if(ui.alert('Очистить операции?','Архив проверен. Удалить операции за '+c.name+' из «Траты»?',ui.ButtonSet.YES_NO)!==ui.Button.YES){cleanExit=true;return;}
    lock.waitLock(30000);if(!fbArchiveUnchanged(ss,c))throw new Error('Данные изменились; очистка отменена');
    fbClearArchive(ss,c);cleanExit=true;
    ui.alert('Архив проверен, операции очищены. Аналитика переключена на следующий месяц.');
  }finally{
    if(cleanExit||!archiveTouched)fbSetMeta(ss,{access_mode:'open'});
    lock.releaseLock();
  }
}
// The web entry is enabled only in the separately deployed IFT script project.
const FB_ARCHIVE_IFT='1jaEzmJPqYzxSp-HtDw-zwt_kPqSEs4enBrkG-Lc-UzA';
function fbArchiveHash(value){
  return Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256,JSON.stringify(value),Utilities.Charset.UTF_8)
    .map(x=>('0'+((x+256)%256).toString(16)).slice(-2)).join('');
}
function fbArchiveSource(c){return fbArchiveHash([c.rows,c.view,c.before.taxonomy_revision,c.period.key]);}
function fbArchiveCurrentSource(ss){
  return fbArchiveHash([fbRows(ss,FB.ledger),ss.getSheetByName(FB.analytics).getDataRange().getValues(),fbMetadata(ss,false).taxonomy_revision,fbPeriod(fbRows(ss,FB.analytics)).key]);
}
function fbArchiveStored(ss,name){
  const archive=ss.getSheetByName(name),summary=ss.getSheetByName(FB.archive);
  return fbArchiveHash([archive?archive.getDataRange().getValues():null,summary?summary.getDataRange().getValues():null]);
}
function fbArchiveReply(job){
  return {protocol:1,ok:true,request_id:job.id,status:job.status,period:job.period,name:job.name,
    replace:job.replace||false,url:job.url||'',code:job.code||''};
}
function fbArchivePut(props,job){
  props.setProperty('FB_ARCHIVE_JOB_'+job.id,JSON.stringify(job));
  const all=props.getProperties(),jobs=Object.keys(all).filter(k=>k.startsWith('FB_ARCHIVE_JOB_'))
    .map(k=>({key:k,job:JSON.parse(all[k])})).sort((a,b)=>b.job.created-a.job.created);
  // Keep recent signatures valid for their full acceptance window; never evict active work.
  jobs.slice(20).filter(x=>['complete','kept','cancelled','stale'].includes(x.job.status)&&Date.now()-x.job.created>300000)
    .forEach(x=>props.deleteProperty(x.key));
}
function fbArchiveAuthenticate(e,props){
  const envelope=JSON.parse(e.postData.contents);
  if(typeof envelope.payload!=='string'||envelope.payload.length>8192||!/^[a-f0-9]{64}$/.test(envelope.signature||''))throw new Error('unauthorized');
  const key=props.getProperty('FB_ARCHIVE_KEY');
  if(!key||key.length<32)throw new Error('unauthorized');
  const expected=Utilities.computeHmacSha256Signature(envelope.payload,key,Utilities.Charset.UTF_8)
    .map(x=>('0'+((x+256)%256).toString(16)).slice(-2)).join('');
  let diff=0;for(let i=0;i<64;i++)diff|=expected.charCodeAt(i)^envelope.signature.charCodeAt(i);
  if(diff)throw new Error('unauthorized');
  const p=JSON.parse(envelope.payload),uuid=/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
  if(!Number.isInteger(p.timestamp)||Math.abs(Date.now()-p.timestamp)>300000||!uuid.test(p.request_id||'')||!uuid.test(p.nonce||'')||
    !['preview','archive','clear','status','cancel'].includes(p.action)||String(p.owner_id)!==props.getProperty('FB_ARCHIVE_OWNER_ID')||
    p.spreadsheet_id!==FB_ARCHIVE_IFT||props.getProperty('FB_ARCHIVE_SHEET_ID')!==FB_ARCHIVE_IFT)throw new Error('unauthorized');
  return p;
}
function fbArchiveDispatch(p,props){
  const key='FB_ARCHIVE_JOB_'+p.request_id,stored=props.getProperty(key);
  let job=stored?JSON.parse(stored):null;
  if(job&&(job.owner!==String(p.owner_id)||job.scope!==p.spreadsheet_id))throw new Error('unauthorized');
  if(p.action==='status'&&!job)return {protocol:1,ok:true,status:'not_found',request_id:p.request_id};
  if(job&&['complete','kept','cancelled','stale'].includes(job.status))return fbArchiveReply(job);
  if(job&&job.nonces&&job.nonces.includes(p.nonce))return fbArchiveReply(job);
  if(p.action==='preview'&&!job){
    const active=Object.values(props.getProperties()).filter(v=>v.startsWith('{')).map(v=>{try{return JSON.parse(v);}catch(_){return {};}})
      .find(j=>j.scope===p.spreadsheet_id&&j.id&& !['complete','kept','cancelled','stale'].includes(j.status));
    if(active)return {protocol:1,ok:false,code:'request_in_progress',request_id:active.id};
  }
  if(!job&&p.action!=='preview')return {protocol:1,ok:true,status:'not_found',request_id:p.request_id};
  const ss=SpreadsheetApp.openById(FB_ARCHIVE_IFT);
  const save=()=>fbArchivePut(props,job),stamp=status=>{job.status=status;save();};
  const stale=()=>{job.code='source_changed';stamp('stale');return fbArchiveReply(job);};
  try{
    if(p.action==='preview'&&!job){
      fbMetadata(ss,true);const c=fbArchiveContext(ss);
      job={id:p.request_id,owner:String(p.owner_id),scope:p.spreadsheet_id,created:Date.now(),status:'preview',
        period:c.period.key,month:c.period.month,year:c.period.year,name:c.name,archiveName:c.archiveName,
        revision:c.before.taxonomy_revision,source:fbArchiveSource(c),priorArchive:fbArchiveStored(ss,c.archiveName),
        replace:c.found>=0||Boolean(c.old),nonces:[p.nonce]};save();return fbArchiveReply(job);
    }
    if(p.action==='cancel'&&['preview','archived'].includes(job.status)){
      stamp(job.status==='archived'?'kept':'cancelled');return fbArchiveReply(job);
    }
    if(p.action==='status'||job.status==='complete'){
      if(['clearing','advancing','complete_pending'].includes(job.status)){
        const meta=fbMetadata(ss,false),period=fbPeriod(fbRows(ss,FB.analytics));
        const next=job.month===12?{month:1,year:job.year+1}:{month:job.month+1,year:job.year};
        const empty=fbRows(ss,FB.ledger).slice(1).every(row=>row.slice(0,6).every(x=>x===''||x===null));
        if(job.status==='complete_pending'&&meta.access_mode==='open'&&period.key===String(next.year)+'-'+String(next.month).padStart(2,'0')){stamp('complete');return fbArchiveReply(job);}
        if(!empty||meta.taxonomy_revision!==job.revision||fbArchiveStored(ss,job.archiveName)!==job.savedArchive||
          ![job.period,String(next.year)+'-'+String(next.month).padStart(2,'0')].includes(period.key)){
          job.code='recovery_required';stamp('recovery_required');return fbArchiveReply(job);
        }
        fbClearArchive(ss,{period:{month:job.month,year:job.year}},stage=>stamp(stage==='complete'?'complete_pending':stage));
        fbSetMeta(ss,{access_mode:'open'});stamp('complete');
      }else if(job.status==='archiving'){
        job.code='recovery_required';stamp('recovery_required');
      }
      return fbArchiveReply(job);
    }
    if(p.action==='archive'&&job.status==='preview'){
      fbMetadata(ss,true);if(fbArchiveCurrentSource(ss)!==job.source)return stale();const c=fbArchiveContext(ss);
      if(fbArchiveSource(c)!==job.source||fbArchiveStored(ss,job.archiveName)!==job.priorArchive)return stale();
      job.nonces.push(p.nonce);stamp('archiving');fbSetMeta(ss,{access_mode:'maintenance'});
      const archive=fbSaveArchive(ss,c);
      job.savedArchive=fbArchiveStored(ss,job.archiveName);
      job.url='https://docs.google.com/spreadsheets/d/'+ss.getId()+'/edit#gid='+archive.getSheetId();
      stamp('archived');fbSetMeta(ss,{access_mode:'open'});return fbArchiveReply(job);
    }
    if(p.action==='clear'&&job.status==='archived'){
      fbMetadata(ss,true);if(fbArchiveCurrentSource(ss)!==job.source)return stale();const c=fbArchiveContext(ss);
      if(fbArchiveSource(c)!==job.source||fbArchiveStored(ss,job.archiveName)!==job.savedArchive)return stale();
      job.nonces.push(p.nonce);stamp('clearing');fbSetMeta(ss,{access_mode:'maintenance'});
      fbClearArchive(ss,c,stage=>stamp(stage==='complete'?'complete_pending':stage));fbSetMeta(ss,{access_mode:'open'});stamp('complete');return fbArchiveReply(job);
    }
    return fbArchiveReply(job);
  }catch(error){
    if(job&&['archiving','clearing','advancing','complete_pending'].includes(job.status)){
      // Persist the checkpoint. Status can prove completion of clear/advance;
      // uncertain archive writes require operator recovery and stay blocked.
      job.code='operation_failed';save();return fbArchiveReply(job);
    }
    return {protocol:1,ok:false,code:'invalid_source',request_id:p.request_id};
  }
}
function doPost(e){
  const props=PropertiesService.getScriptProperties();let p;
  try{p=fbArchiveAuthenticate(e,props);}catch(_){return ContentService.createTextOutput(JSON.stringify({protocol:1,ok:false,code:'unauthorized'})).setMimeType(ContentService.MimeType.JSON);}
  const lock=fbLock();let result;
  try{lock.waitLock(30000);result=fbArchiveDispatch(p,props);}
  catch(_){result={protocol:1,ok:false,code:'busy',request_id:p.request_id};}
  finally{lock.releaseLock();}
  return ContentService.createTextOutput(JSON.stringify(result)).setMimeType(ContentService.MimeType.JSON);
}
if(typeof module!=='undefined')module.exports={FB,fbName,fbDate,fbAmount,fbReconcile,fbArchiveValues,fbPeriod};
