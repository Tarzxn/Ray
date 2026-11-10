/* Beam model picker: model, self-review and Crew parallelism. Loaded after app.js. */
(()=>{
  const q=(s,r=document)=>r.querySelector(s);
  const html=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const defaultModel={id:'nemotron-3-super',label:'Nemotron 3 Super',usage:2,pick:'default'};
  let data=null,loading=null,choice={model:defaultModel.id,reflection:'auto',parallel:1};
  const groups=[['default','Default'],['free','Strong and free-friendly'],['heavy','Heavier · may use up a free plan quickly'],['other','Also on your account']];
  const usage=n=>'●'.repeat(n)+'○'.repeat(4-n);
  const label=id=>(data?.models||[defaultModel]).find(m=>m.id===id)?.label||id;

  async function load(force){
    if(data&&!force)return data;
    if(loading)return loading;
    loading=(async()=>{try{
      data=await api('models');
      choice.model=data.selected||choice.model;choice.reflection=data.reflection||'auto';choice.parallel=data.crew_parallel||1;
    }catch{data=data||null}finally{loading=null;paintLabel()}return data})();
    return loading;
  }
  function paintLabel(){
    const el=q('.model-label');if(!el)return;
    el.classList.add('model-picker');el.setAttribute('role','button');el.tabIndex=0;el.title='Choose the model and reasoning';
    el.innerHTML='<span class="model-dot"></span>'+html(label(choice.model))+' <span data-icon="chevron"></span>';
    if(window.hydrateIcons)window.hydrateIcons(el);
    const usageLabel=q('#usage');if(usageLabel)usageLabel.textContent='Ollama Cloud · '+label(choice.model);
    const settingsLabel=q('#settings-model-label');if(settingsLabel)settingsLabel.textContent=label(choice.model);
    const settingsId=q('#settings-model-id');if(settingsId)settingsId.textContent=choice.model;
  }
  function dialog(){
    let d=q('#model-dialog');if(d)return d;
    d=document.createElement('dialog');d.id='model-dialog';
    d.innerHTML='<div class="dialog-content model-dialog"><div class="dialog-heading"><h2>Model and reasoning</h2><button type="button" class="icon-button" id="model-close" aria-label="Close">×</button></div><div id="model-body"></div><div class="model-actions"><span id="model-error" class="form-error"></span><button type="button" class="primary" id="model-save">Save</button></div></div>';
    document.body.appendChild(d);
    q('#model-close',d).onclick=()=>d.close();
    q('#model-save',d).onclick=async()=>{
      const b=q('#model-save',d);b.disabled=true;q('#model-error',d).textContent='';
      try{
        const selected=q('input[name=beam-model]:checked',d);
        const model=selected?.id==='model-custom'
          ? q('#model-custom-id',d).value.trim()
          : selected?.value||choice.model;
        if(!model)throw new Error('Enter an Ollama Cloud model ID.');
        const reflection=q('#model-reflection',d).value,crew_parallel=Number(q('#model-parallel',d).value);
        await api('settings',{model,reflection,crew_parallel});
        choice={model,reflection,parallel:crew_parallel};data=null;await load(true);d.close();
        if(window.toast)window.toast('Using '+label(choice.model));
      }catch(e){q('#model-error',d).textContent=e.message||'Could not save'}finally{b.disabled=false}
    };
    return d;
  }
  function body(){
    const d=dialog(),models=data?.models||[defaultModel];
    const customSelected=!models.some(m=>m.id===choice.model);
    const sections=groups.map(([key,title])=>{
      const items=models.filter(m=>(m.pick||'other')===key);if(!items.length)return'';
      return '<div class="model-group"><h3>'+title+'</h3>'+items.map(m=>'<label class="model-option"><input type="radio" name="beam-model" value="'+html(m.id)+'"'+(m.id===choice.model?' checked':'')+'><span class="model-option-main"><b>'+html(m.label)+'</b><small>'+html(m.note||m.id)+'</small></span><span class="model-usage" title="Relative Ollama Cloud usage">'+usage(m.usage||2)+'</span></label>').join('')+'</div>';
    }).join('');
    const live=data?.live?'':'<p class="model-note">Could not reach Ollama to list your models; showing the usual ones.</p>';
    q('#model-body',d).innerHTML=live+sections+
      '<div class="model-group"><label class="model-option"><input type="radio" name="beam-model" id="model-custom"'+(customSelected?' checked':'')+'>Custom model</label><input type="text" id="model-custom-id" aria-label="Custom Ollama Cloud model ID" placeholder="Ollama Cloud model ID" autocomplete="off" spellcheck="false"><p class="model-note">Enter a model ID available to your Ollama Cloud account.</p></div>'+
      '<div class="model-group"><h3>Self-review</h3><select id="model-reflection"><option value="auto">Automatic (High and Max effort review their work)</option><option value="always">Always review finished work</option><option value="off">Off (fastest, lowest usage)</option></select><p class="model-note">After finishing, Ray checks its answer against your request and the tool results, then fixes problems it finds. Each review is one extra model call.</p></div>'+
      '<div class="model-group"><h3>Crew parallel research</h3><select id="model-parallel"><option value="1">1 at a time</option><option value="2">2 read-only tasks together</option><option value="3">3 read-only tasks together</option></select><p class="model-note">Only Plan-mode tasks run side by side; anything that edits runs alone. Free Ollama plans run one cloud model at a time, so use 2–3 only on Pro or Max.</p></div>';
    q('#model-reflection',d).value=choice.reflection;q('#model-parallel',d).value=String(choice.parallel);
    const custom=q('#model-custom-id',d);
    custom.value=customSelected?choice.model:'';
    custom.oninput=()=>{q('#model-custom',d).checked=true};
  }
  async function open(){const d=dialog(),save=q('#model-save',d);save.disabled=true;q('#model-error',d).textContent='';q('#model-body',d).innerHTML='<p class="model-note">Loading your models…</p>';d.showModal();await load(true);body();save.disabled=false}

  // Crew member editor: one model per specialist
  function botField(){
    let f=q('#crew-bot-model-field');if(f)return f;
    const anchor=q('#crew-bot-role');if(!anchor)return null;
    f=document.createElement('label');f.id='crew-bot-model-field';f.className='model-bot-field';
    f.innerHTML='<span>Ollama Cloud model ID</span><input id="crew-bot-model" list="crew-bot-models" autocomplete="off" spellcheck="false"><datalist id="crew-bot-models"></datalist><small>Choose a suggestion or enter a model ID. Leave blank to use the Studio model.</small>';
    (anchor.closest('label')||anchor).after(f);return f;
  }
  async function fillBot(bot){
    const f=botField();if(!f)return;
    const input=q('#crew-bot-model',f);
    input.value=bot?.model||'';
    await load();
    input.placeholder=label(choice.model);
    q('#crew-bot-models',f).innerHTML=(data?.models||[defaultModel]).map(m=>'<option value="'+html(m.id)+'">'+html(m.label)+'</option>').join('');
  }
  window.beamModels={open,load,fillBot,botModel:()=>q('#crew-bot-model')?.value.trim()||''};
  document.addEventListener('click',e=>{if(e.target.closest('.model-picker'))open()});
  document.addEventListener('keydown',e=>{if((e.key==='Enter'||e.key===' ')&&e.target.classList?.contains('model-picker')){e.preventDefault();open()}});
  const start=()=>{paintLabel();setTimeout(()=>load(),600)};
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start);else start();
  document.addEventListener('beam:connected',()=>load(true));
})();
