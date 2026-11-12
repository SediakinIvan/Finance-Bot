const fs=require('node:fs');const vm=require('node:vm');
const scope={module:{exports:{}},Date,Map,Set};
vm.runInNewContext(fs.readFileSync('data/macros.gs','utf8'),scope);
const api=scope.module.exports;
const cases=JSON.parse(fs.readFileSync(0,'utf8'));
const result=cases.map(x=>api.fbReconcile(x.rows,x.registry,x.snapshots,()=>x.next_id));
process.stdout.write(JSON.stringify(result));
