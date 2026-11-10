"""Inspect real output contents and report verifiable quality facts."""
from pathlib import Path
import hashlib
import json
import math
import re
import zipfile

GUIDES={
 'documents':'DOCX/PDF: Markdown or {title,subtitle,markdown,page_size:"A4|Letter",author}. Use informative headings, complete paragraphs and real tables. Include source links where needed. Avoid placeholders, irrelevant Beam branding and generic filler. PDFs are fixed layout; DOCX remains editable. inspect_artifact returns actual text/table/page counts, not a visual review.',
 'xlsx':'XLSX: {sheets:[{name,rows:[[headers],[values]],formats:{B:"£#,##0.00"},charts:[{type:"bar|line|pie",title,labels:"A",values:["B"]}]}]}. Use formulas for derived values; numeric inputs must be numbers. Keep IDs as text. Charts require numeric data and a header row. Name every sheet meaningfully. Formula results recalculate in Excel/LibreOffice; Beam does not calculate them. Do not claim cached totals have been calculated.',
 'pptx':'PPTX: {theme:{accent:"C87959",background:"F8F7F3",text:"252722",brand:""},slides:[{layout:"title",title,subtitle},{layout:"metrics",title,metrics:[{value,label}]},{layout:"two-column",title,left:{title,bullets:[]},right:{title,bullets:[]}},{layout:"process",title,steps:[{title,body}]},{layout:"chart",title,chart:{type:"bar",labels:[],series:[{name,values:[]}]}},{title,table:[[headers],[values]]},{title,bullets:[],notes:"Speaker notes"}]}. Use one idea per slide and a mix of layouts. Limit bullets, use notes for detail. Content must be complete. Do not invent numbers or citations.',
 '3d':'3D: first call cad_help. Use an explicit dimensional design and supported geometry. Top-level units: mm (default), cm, or in. quality: draft|standard|high (default high). constraints:{size_mm:[width,depth,height],tolerance_mm:0.1,components:1} verifies final geometry; never silently stretches/rescales it. The first grounded model is centered in X/Y with Z=0 at the bottom. Presets: enclosure, lidded_enclosure, bracket, vase, planter, finger_joint_box. Deliverables include STL, GLB, editable JSON, a topology/dimension report, preview and dimensioned view sheet. Assemblies include separate print-oriented part exports and a BOM. Validation does not prove appearance, tolerances, supports or load safety.',
 'other':'Charts: {type:"bar|line|pie",title,labels:[],series:[{name,values:[]}]}; values must match labels. SVG/code/text must be complete valid files. ZIP: [{path,kind,content}]; include a README and all local assets, and use relative links. Files are reopened and staged before publication; do not report success if validation fails.'}

def guide(kind='all'):
 key={'docx':'documents','pdf':'documents','stl':'3d','glb':'3d','chart':'other','zip':'other','text':'other'}.get(str(kind).lower(),str(kind).lower())
 return GUIDES.get(key,'\n\n'.join(GUIDES.values()))

def _text(value,limit=18000):return str(value or '')[:limit]

def inspect_file(path):
 path=Path(path);suffix=path.suffix.lower();result={'name':path.name,'bytes':path.stat().st_size,'format':suffix.lstrip('.'),'checks':[],'warnings':[],'preview_note':'Structured content preview; visual layout has not been reviewed.'}
 def check(label,detail):result['checks'].append({'check':label,'detail':detail})
 if suffix=='.docx':
  from docx import Document
  doc=Document(path);paragraphs=[{'text':p.text,'style':p.style.name} for p in doc.paragraphs if p.text.strip()];tables=[[[c.text for c in row.cells] for row in t.rows] for t in doc.tables]
  result.update(paragraphs=paragraphs[:400],tables=[t[:80] for t in tables[:12]],metrics={'paragraphs':len(paragraphs),'tables':len(tables),'words':sum(len(p['text'].split()) for p in paragraphs)})
  if not paragraphs and not tables:raise ValueError('Document has no readable contents')
  check('Reopened DOCX',f"{len(paragraphs)} paragraphs and {len(tables)} tables")
 elif suffix=='.pdf':
  from pypdf import PdfReader
  doc=PdfReader(path);pages=[p.extract_text() or '' for p in doc.pages];result.update(pages=[_text(t,15000) for t in pages[:30]],metrics={'pages':len(pages),'words':sum(len(t.split()) for t in pages)})
  if not pages:raise ValueError('PDF has no pages')
  check('Decoded PDF streams',f'{len(pages)} pages')
  if not any(t.strip() for t in pages):result['warnings'].append('PDF contains no extractable text; it may contain only images.')
 elif suffix=='.xlsx':
  from openpyxl import load_workbook
  from openpyxl.formula.tokenizer import Tokenizer,TokenizerError
  wb=load_workbook(path,data_only=False)
  try:
   sheets=[];formulas=charts=0
   for ws in wb:
    rows=[]
    for row in ws.iter_rows():
     for cell in row:
      if cell.data_type=='e':raise ValueError(f'Spreadsheet error {cell.value} at {ws.title}!{cell.coordinate}')
      if cell.data_type=='f':
       formulas+=1
       if '#REF!' in cell.value.upper():raise ValueError(f'Broken formula reference at {ws.title}!{cell.coordinate}')
       try:Tokenizer(cell.value)
       except TokenizerError as error:raise ValueError(f'Invalid formula at {ws.title}!{cell.coordinate}: {error}') from None
     if len(rows)<80:rows.append([v if isinstance(v,(str,int,float,bool,type(None))) else str(v) for v in [c.value for c in row[:20]]])
    charts+=len(ws._charts);sheets.append({'name':ws.title,'rows':rows,'row_count':ws.max_row,'column_count':ws.max_column,'charts':len(ws._charts)})
   result.update(sheets=sheets,metrics={'sheets':len(sheets),'formulas':formulas,'charts':charts},formula_note='Formulas are preserved and checked for broken references. Values recalculate in Excel/LibreOffice; no calculation was performed by Beam.')
   check('Reopened workbook','Formula tokens and cell errors checked')
  finally:wb.close()
 elif suffix=='.pptx':
  from pptx import Presentation
  prs=Presentation(path);slides=[]
  for i,slide in enumerate(prs.slides):
   text=[];tables=[];chart_count=0
   for shape in slide.shapes:
    if shape.left<-.01*prs.slide_width or shape.top<-.01*prs.slide_height or shape.left+shape.width>prs.slide_width*1.01 or shape.top+shape.height>prs.slide_height*1.01:raise ValueError(f'Slide {i+1} has an object outside the slide canvas')
    if shape.has_text_frame and shape.text.strip():text.append(_text(shape.text,6000))
    if shape.has_table:tables.append([[c.text for c in row.cells] for row in shape.table.rows])
    chart_count+=int(shape.has_chart)
   notes=slide.notes_slide.notes_text_frame.text if slide.has_notes_slide else ''
   slides.append({'number':i+1,'text':text,'tables':tables,'charts':chart_count,'notes':_text(notes,6000)})
  result.update(slides=slides,metrics={'slides':len(slides),'charts':sum(s['charts'] for s in slides),'tables':sum(len(s['tables']) for s in slides)})
  check('Reopened presentation','Slide canvas bounds checked; text fit uses an estimate')
 elif suffix in ('.stl','.glb'):
  import mesh_engine as me
  mesh=me.read_stl(path) if suffix=='.stl' else me.read_glb(path);info=mesh.validate(connected=False);result.update(metrics=info)
  report=path.with_name(path.stem+'-validation.json')
  if report.is_file():result['model']=json.loads(report.read_text('utf-8'))
  check('Reopened mesh','Closed edges, winding, volume and dimensions checked')
 elif suffix=='.zip':
  with zipfile.ZipFile(path) as archive:
   if archive.testzip():raise ValueError('ZIP has a corrupt member')
   result.update(files=[{'path':v.filename,'bytes':v.file_size} for v in archive.infolist()[:300]],metrics={'files':len(archive.infolist())})
  check('Reopened archive','ZIP CRCs checked')
 elif suffix in ('.png','.jpg','.jpeg','.webp','.gif'):
  from PIL import Image
  with Image.open(path) as picture:result['metrics']={'width':picture.width,'height':picture.height,'mode':picture.mode};picture.verify()
  check('Decoded image','Image container checked; contents not visually interpreted')
 else:
  if path.stat().st_size>2000000:raise ValueError('Text preview exceeds the 2 MB inspection limit')
  text=path.read_text('utf-8-sig');result.update(text=_text(text,50000),metrics={'lines':len(text.splitlines()),'characters':len(text)})
  check('Decoded text','UTF-8 contents available')
 serialized=json.dumps(result,ensure_ascii=False,default=str)
 if re.search(r'\b(?:LOREM IPSUM|INSERT (?:TEXT|TITLE) HERE|REPLACE[_ ]ME)\b',serialized,re.I):result['warnings'].append('Possible placeholder content found; review the requested content.')
 return result

def audit_for_publish(path,kind):
 result=inspect_file(path);result['sha256']=hashlib.sha256(Path(path).read_bytes()).hexdigest()
 return result
