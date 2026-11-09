// Pure binary-parser checks; no browser or UI automation.
const fs=require('fs'), vm=require('vm'), assert=require('assert');
const path=require('path'), {execFileSync}=require('child_process');
const fixtures=JSON.parse(execFileSync(process.env.VELARIS_TEST_PYTHON || 'python',['-c',`
import base64, json, tempfile
from pathlib import Path
import mesh_engine as me
fixtures = {}
with tempfile.TemporaryDirectory(prefix='.viewer-test-', dir='.') as temp:
    for name, mesh in [('loft', me.loft({'sections':[[0,0,0,5,5],[0,0,15,3,3]]})),
                       ('sculpt', me.sculpt({'parts':[{'center':[0,0,0],'radii':[5,7,10]}], 'resolution':32}))]:
        mesh = mesh.grounded()
        fixtures[name] = {}
        for ext, writer in [('stl',me.write_stl),('glb',me.write_glb)]:
            target = Path(temp)/(name+'.'+ext)
            writer(target,mesh)
            fixtures[name][ext] = base64.b64encode(target.read_bytes()).decode('ascii')
print(json.dumps(fixtures))
`],{cwd:path.resolve(__dirname,'..','worker'),encoding:'utf8',maxBuffer:16*1024*1024}));
const src=fs.readFileSync(require('path').join(__dirname,'../static/app.js'),'utf8');
const start=src.indexOf('function parseGlb(buf)'), code=src.slice(start,src.indexOf('const $=s=>document.querySelector',start));   // the two pure parsers, before any DOM code
const ctx={TextDecoder,DataView,Float32Array,Uint32Array,Uint8Array,ArrayBuffer,Math,Map,JSON,Infinity,Error};
vm.createContext(ctx);vm.runInContext(code,ctx);
for(const [name,fixture] of Object.entries(fixtures)) {
  function read(ext){const b=Buffer.from(fixture[ext],'base64');return b.buffer.slice(b.byteOffset,b.byteOffset+b.byteLength);}
  const stl=ctx.parseStl(read('stl')), glb=ctx.parseGlb(read('glb'));
  assert.strictEqual(stl.count,glb.count);
  assert(stl.count>100);
  for(let k=0;k<3;k++) {assert(Math.abs(stl.min[k]-glb.min[k])<.001);assert(Math.abs(stl.max[k]-glb.max[k])<.001);}
  assert([...glb.pos].every(Number.isFinite));assert([...glb.nrm].every(Number.isFinite));
  console.log(name,stl.count,'triangles; STL/GLB parser bounds agree');
}

