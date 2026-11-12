const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('data/macros.gs','utf8');
const id=n=>'00000000-0000-4000-8000-'+String(n).padStart(12,'0');
const clone=x=>JSON.parse(JSON.stringify(x));
function environment(answers=[],failSnapshot=false){
  const sheets=new Map(),cleared=[],deleted=[],rules=[];let sequence=99;
  class Sheet{
    constructor(name,rows=[]){this.name=name;this.rows=clone(rows);this.maxRows=100;this.charts=[];sheets.set(name,this);}
    getName(){return this.name;}
    getLastRow(){let n=this.rows.length;while(n&&this.rows[n-1].every(x=>x===''))n--;return n;}
    getMaxColumns(){return 26;}
    getMaxRows(){return Math.max(this.maxRows,this.rows.length);}
    insertRowsAfter(_index,count){this.maxRows+=count;}
    insertColumnsAfter(){}
    setColumnWidth(){return this;}
    getCharts(){return this.charts;}
    updateChart(chart){this.charts=[chart];}
    getDataRange(){return this.getRange(1,1,Math.max(1,this.getLastRow()),Math.max(1,...this.rows.map(x=>x.length)));}
    getRange(r,c,h=1,w=1){const sheet=this;return {
      getSheet(){return sheet;},
      getValues(){return Array.from({length:h},(_,i)=>Array.from({length:w},(_,j)=>sheet.rows[r+i-1]?.[c+j-1]??''));},
      setValues(values){
        if(failSnapshot&&sheet.name.startsWith('_pending'))throw new Error('simulated snapshot failure');
        for(let i=0;i<h;i++){while(sheet.rows.length<r+i)sheet.rows.push([]);for(let j=0;j<w;j++)sheet.rows[r+i-1][c+j-1]=values[i][j];}
        return this;
      },
      clearContent(){cleared.push(sheet.name);for(let i=0;i<h;i++)for(let j=0;j<w;j++)sheet.rows[r+i-1][c+j-1]='';},
      setDataValidations(values){rules.push({sheet:sheet.name,row:r,column:c,values:clone(values)});return this;}
    };}
    clearContents(){this.rows=[];}
    setName(name){sheets.delete(this.name);this.name=name;sheets.set(name,this);return this;}
    copyTo(){const copy=new Sheet('Copy of '+this.name,this.rows);copy.charts=this.charts.slice();return copy;}
  }
  const ss={getId:()=> 'copy-id',getSpreadsheetTimeZone:()=> 'Europe/Moscow',getSheetByName:n=>sheets.get(n),insertSheet:n=>new Sheet(n),deleteSheet:s=>{deleted.push(s.name);sheets.delete(s.name);}};
  const scope={module:{exports:{}},Date,Map,Set,console,
    Utilities:{getUuid:()=>id(sequence++)},
    LockService:{getDocumentLock:()=>({waitLock(){},releaseLock(){},tryLock(){return true;}})},
    SpreadsheetApp:{getActiveSpreadsheet:()=>ss,flush(){},newDataValidation:()=>({values:[],requireValueInList(values){this.values=values;return this;},setAllowInvalid(value){this.allow=value;return this;},build(){return {values:this.values,allowInvalid:this.allow};}}),getUi:()=>({ButtonSet:{YES_NO:'yes-no'},Button:{YES:'yes'},alert(...args){return args.length===1?'ok':answers.shift()||'yes';}})}};
  vm.runInNewContext(source,scope);const api=scope.module.exports;
  new Sheet(api.FB.meta,[['key','value'],['spreadsheet_id','copy-id'],['schema_version',2],['taxonomy_revision',0],['migration_state','ready'],['access_mode','open'],['pending_request_id','']]);
  new Sheet(api.FB.categories,[['category_id','type','name','active','deactivated_period'],[id(1),'Расход','Продукты',true,'']]);
  new Sheet(api.FB.children,[['subcategory_id','current_category_id','name','active'],[id(2),id(1),'Магазин',true]]);
  new Sheet(api.FB.history,[['category_id','subcategory_id']]);
  new Sheet(api.FB.ledger,[api.FB.header,['11.11.2026','Продукты','Магазин',100,'Расход','',id(1),id(2),id(3),'valid',46337]]);
  new Sheet(api.FB.snapshots,[api.FB.snapshotHeader,[id(3),id(1),id(2),'Продукты','Магазин','Расход']]);
  new Sheet(api.FB.columns,[['category_id','column'],[id(1),5]]);
  new Sheet(api.FB.labels,[['sheet','cell','kind','category_id','subcategory_id'],
    ['Аналитика','A9','category',id(1),''],['Аналитика','G1','heading',id(1),''],['Аналитика','G3','subcategory',id(1),id(2)]]);
  new Sheet(api.FB.detail,[['category_id','subcategory_id','category','subcategory','amount'],[id(1),id(2),'Продукты','Магазин',100]]);
  new Sheet(api.FB.analytics,[['ТЕКУЩАЯ АНАЛИТИКА','','Месяц:',11,'Состояние','ready'],['','','Год:',2026],['Расходы за месяц',100],['Доход за месяц',0],['Остаток на текущий момент',-100],[],['Траты по категориям (за месяц)'],['Категория','Сумма','Процент от трат','category_id'],['Продукты',100,1,id(1)]]);
  const display=sheets.get(api.FB.analytics);
  display.rows[0][6]='Подкатегории: Продукты';display.rows[0][7]=100;
  display.rows[1][6]='Подкатегория';display.rows[1][7]='Сумма';
  display.rows[2][6]='Магазин';display.rows[2][7]=100;
  return {scope,api,ss,sheets,cleared,deleted,rules,Sheet};
}
{
  const e=environment(['no']);e.scope.saveMonthlyReport();
  assert.equal(e.sheets.get(e.api.FB.ledger).rows[1][3],100);
  assert.equal(e.sheets.get(e.api.FB.archive).rows[1][4],100);
  assert.equal(e.cleared.length,0);
  assert.equal(e.sheets.get(e.api.FB.meta).rows.find(r=>r[0]==='access_mode')[1],'open');
  const snapshot=e.sheets.get('Архив (Ноябрь 2026)');
  assert.deepEqual(snapshot.getRange(1,7,3,2).getValues(),[['Подкатегории: Продукты',100],['Подкатегория','Сумма'],['Магазин',100]]);
  assert.equal(snapshot.rows[0][3],11);assert.equal(snapshot.rows[1][3],2026);
  assert.equal(snapshot.rows[7][3],'');assert.equal(snapshot.rows[8][3],'');
  assert.ok(!snapshot.rows.flat().some(value=>typeof value==='string'&&/^[a-f0-9]{8}-[a-f0-9-]{27}$/.test(value)));
  const labels=e.sheets.get(e.api.FB.labels).rows;
  assert.ok(labels.some(row=>row[0]==='Архив (Ноябрь 2026)'&&row[1]==='G1'&&row[3]===id(1)));
  assert.ok(labels.some(row=>row[0]==='Архив (Ноябрь 2026)'&&row[1]==='G3'&&row[4]===id(2)));
}
{
  const e=environment(['yes']);e.scope.saveMonthlyReport();
  assert.ok(e.cleared.includes(e.api.FB.ledger));
  assert.equal(e.sheets.get(e.api.FB.archive).rows.length,2);
  assert.ok(e.sheets.has('Архив (Ноябрь 2026)'));
  assert.equal(e.sheets.get(e.api.FB.meta).rows.find(r=>r[0]==='access_mode')[1],'open');
}
{
  const e=environment([],true);
  assert.throws(()=>e.scope.saveMonthlyReport(),/snapshot failure/);
  assert.equal(e.cleared.length,0);
  assert.equal(e.sheets.get(e.api.FB.ledger).rows[1][3],100);
  assert.equal(e.sheets.get(e.api.FB.meta).rows.find(r=>r[0]==='access_mode')[1],'maintenance');
}
{
  const e=environment(['no']);new e.Sheet('Архив (Ноябрь 2026)',[['old',900]]);
  e.scope.saveMonthlyReport();
  assert.equal(e.cleared.length,0);assert.equal(e.deleted.length,0);
  assert.equal(e.sheets.get('Архив (Ноябрь 2026)').rows[0][1],900);
}
{
  const e=environment();e.sheets.get(e.api.FB.ledger).rows[1][0]='01.12.2026';
  assert.throws(()=>e.scope.saveMonthlyReport(),/другого месяца/);assert.equal(e.cleared.length,0);
  assert.equal(e.sheets.get(e.api.FB.meta).rows.find(r=>r[0]==='access_mode')[1],'open');
}
{
  const e=environment(['no']);e.sheets.get(e.api.FB.ledger).rows[1][2]='';
  assert.throws(()=>e.scope.saveMonthlyReport(),/Исправьте строки: 2/);
  assert.equal(e.cleared.length,0);assert.equal(e.sheets.has(e.api.FB.archive),false);
  assert.equal(e.sheets.get(e.api.FB.meta).rows.find(r=>r[0]==='access_mode')[1],'open');
  e.sheets.get(e.api.FB.ledger).rows[1][2]='Магазин';
  e.scope.saveMonthlyReport();
  assert.ok(e.sheets.has('Архив (Ноябрь 2026)'));
  assert.equal(e.sheets.get(e.api.FB.ledger).rows[1][3],100);
}
console.log('Apps Script archive: first archive, confirmed clear, cancellation, copy failure, duplicate-period refusal and mixed-period safety passed');
{
  const e=environment(['no']);
  const originalRange={getA1Notation:()=> 'G3:H3'};
  e.sheets.get(e.api.FB.analytics).charts=[{
    getRanges:()=>[originalRange],
    modify(){const ranges=[];return {clearRanges(){return this;},addRange(range){ranges.push(range);return this;},build(){return {ranges};}};}
  }];
  e.scope.saveMonthlyReport();
  const chart=e.sheets.get('Архив (Ноябрь 2026)').charts[0];
  assert.equal(chart.ranges[0].getSheet().getName(),'Архив (Ноябрь 2026)');
}
{
  const e=environment(['no']);
  e.sheets.get(e.api.FB.categories).rows.push([id(4),'Расход','Новая',true,'']);
  e.sheets.get(e.api.FB.children).rows.push([id(5),id(4),'Подкатегория',true]);
  e.sheets.get(e.api.FB.analytics).rows.push(['Новая',0,0,id(4)]);
  e.sheets.get(e.api.FB.detail).rows.push([id(4),id(5),'Новая','Подкатегория',0]);
  e.scope.saveMonthlyReport();
  const summary=e.sheets.get(e.api.FB.archive).rows;
  assert.equal(summary[0][4],'Продукты');assert.equal(summary[1][4],100);
  assert.equal(summary[0][5],'Новая');assert.equal(summary[1][5],0);
}
{
  const e=environment(['yes']);
  e.sheets.get(e.api.FB.categories).rows[1][3]=false;
  e.sheets.get(e.api.FB.categories).rows[1][4]='2026-11';
  e.scope.saveMonthlyReport();
  assert.equal(e.sheets.get(e.api.FB.archive).rows[1][4],100);
  const view=e.sheets.get(e.api.FB.analytics).rows;
  view[0][3]=12;view[2][1]=0;view[4][1]=0;view[8][1]=0;
  e.sheets.get(e.api.FB.detail).rows[1][4]=0;
  e.scope.saveMonthlyReport();
  assert.equal(e.sheets.get(e.api.FB.archive).rows[1][4],100);
  assert.equal(e.sheets.get(e.api.FB.archive).rows[2][4],0);
}
console.log('Apps Script archive: dynamic columns and actual/zero deactivation periods passed');
{
  const e=environment(),ledger=e.sheets.get(e.api.FB.ledger);
  ledger.rows.push(['12.11.2026','Продукты','Магазин',200,'Расход','']);
  while(ledger.rows.length<161)ledger.rows.push(Array(11).fill(''));
  ledger.rows.push(['13.11.2026','Продукты','Магазин',300,'Расход','']);
  e.scope.onEdit({source:e.ss,range:{getSheet:()=>ledger}});
  assert.equal(ledger.rows[2][9],'valid');assert.equal(ledger.rows[161][9],'valid');
  assert.notEqual(ledger.rows[2][8],ledger.rows[161][8]);
  assert.equal(e.rules[0].values.length,161);
  assert.ok(e.rules[0].values[1][0].values.includes('Продукты'));
  assert.ok(e.rules[0].values[1][1].values.includes('Магазин'));
  ledger.rows[2]=Array(11).fill('');e.scope.refreshFinanceBotRows();
  assert.equal(ledger.rows[2][8],'');
}
console.log('Apps Script manual input: dropdowns, range paste, row clearing and expansion beyond 155 passed');
