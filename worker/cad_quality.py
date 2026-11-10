"""Explicit model units, tessellation and measured dimension contracts."""
import copy
import math
import numpy as np

UNITS={'mm':1.0,'cm':10.0,'in':25.4,'inch':25.4,'inches':25.4}

def prepare_model(spec):
 if not isinstance(spec,dict):raise ValueError('Model specification must be an object')
 spec=copy.deepcopy(spec);unit=str(spec.get('units','mm')).lower()
 if unit not in UNITS:raise ValueError('Model units must be mm, cm or in')
 quality=spec.get('quality','high')
 if quality not in ('draft','standard','high'):raise ValueError('Model quality must be draft, standard or high')
 factor=UNITS[unit];error={'draft':.2,'standard':.08,'high':.025}[quality];minimum={'draft':24,'standard':48,'high':64}[quality]
 curved={'sphere','cylinder','cone','tube','torus','capsule','revolve','rounded_box','plate','spring','path'}
 def walk(value):
  if isinstance(value,list):
   for item in value:walk(item)
  elif isinstance(value,dict):
   if value.get('shape') in curved and 'segments' not in value:
    radius=max(float(value.get('radius',5)),float(value.get('radius_top',0) or 0),.1)*factor
    if value.get('profile'):radius=max(abs(float(p[0])) for p in value['profile'])*factor
    angle=math.acos(max(-1,min(1,1-error/max(radius,error))))
    segments=max(minimum,min(256,math.ceil(math.pi/max(angle,.001))));value['segments']=int(math.ceil(segments/4)*4)
   for item in list(value.values()):
    if isinstance(item,(list,dict)):walk(item)
 walk(spec)
 # One wrapper applies conversion to all dimensions, translations and part geometry.
 if factor!=1:
  if any(spec.get('assembly',{}).get(key) for key in ('checks','exploded_view')):
   raise ValueError('Assembly checks and exploded views require coordinates in mm')
  if 'tree' in spec:node=spec['tree']
  elif 'shape' in spec or 'op' in spec:node={k:v for k,v in spec.items() if k not in ('units','constraints','quality','material','assembly','plan','parameters')}
  elif spec.get('parts'):node={'op':'union','children':spec['parts']}
  else:raise ValueError('Non-mm models require a tree, shape or parts specification')
  spec['tree']={'op':'union','children':[node],'scale':[factor]*3}
  if spec.get('assembly',{}).get('export_individual_parts'):raise ValueError('Assembly part exports currently use mm; convert the part coordinates to mm explicitly')
 return spec,{'input_units':unit,'scale_to_mm':factor,'quality':quality,'target_chord_error_mm':error}

def check_constraints(spec,mesh,info):
 constraints=spec.get('constraints',{})
 if not isinstance(constraints,dict):raise ValueError('Model constraints must be an object')
 checks=[];expected=constraints.get('size_mm')
 tolerance=float(constraints.get('tolerance_mm',.15))
 if not math.isfinite(tolerance) or not .001<=tolerance<=10:raise ValueError('Dimension tolerance must be between 0.001 and 10 mm')
 if expected is not None:
  if not isinstance(expected,(list,tuple)) or len(expected)!=3:raise ValueError('constraints.size_mm must be [width,depth,height], with null for unspecified axes')
  for axis,wanted,actual in zip(('width','depth','height'),expected,info['size_mm']):
   if wanted is None:continue
   if not isinstance(wanted,(int,float)) or not math.isfinite(wanted) or wanted<=0:raise ValueError('Requested model dimensions must be finite positive numbers')
   delta=abs(actual-wanted)
   if delta>tolerance:raise ValueError(f'Model {axis} is {actual:.3f} mm; requested {wanted:.3f} mm (tolerance {tolerance:g} mm). Correct the geometry; no automatic rescaling was performed.')
   checks.append({'axis':axis,'requested_mm':wanted,'actual_mm':actual,'difference_mm':delta,'tolerance_mm':tolerance})
 if 'components' in constraints and info['components']!=int(constraints['components']):raise ValueError(f"Expected {constraints['components']} connected component(s), found {info['components']}")
 for key,limit in (('max_size_mm',max),('min_size_mm',min)):
  wanted=constraints.get(key)
  if wanted is None:continue
  if not isinstance(wanted,list) or len(wanted)!=3:
   raise ValueError(f'constraints.{key} needs [width, depth, height] with null for unspecified axes')
  for axis,bound,actual in zip(('width','depth','height'),wanted,info['size_mm']):
   if bound is None:continue
   if not isinstance(bound,(int,float)) or not math.isfinite(bound) or bound<=0:
    raise ValueError('Envelope limits must be finite positive millimetres')
   if abs(limit(bound,actual)-bound)>tolerance:
    raise ValueError(f'Model {axis} is {actual:.3f} mm and fails {key} limit {bound:.3f} mm')
   checks.append({'axis':axis,'limit':key,'limit_mm':bound,'actual_mm':actual,'tolerance_mm':tolerance})
 a,b,c=(mesh.vertices[mesh.faces[:,i]] for i in range(3));info['surface_area_mm2']=float(np.linalg.norm(np.cross(b-a,c-a),axis=1).sum()/2)
 info['dimension_checks']=checks;info['bounds_mm']={'min':mesh.vertices.min(axis=0).tolist(),'max':mesh.vertices.max(axis=0).tolist()}
 return info

def dimension_sheet(path,mesh,info):
 """Orthographic rendered views with measured extents, not a fabrication drawing."""
 import base64,io
 from PIL import Image,ImageDraw
 from html import escape
 import cad_engine
 panels=[];w,d,h=info['size_mm']
 sheet=Image.new('RGB',(1160,530),'#f8f7f3');draw=ImageDraw.Draw(sheet);font=cad_engine._label_font(15);small=cad_engine._label_font(13)
 draw.text((28,20),path.stem+' · measured views',fill='#27322e',font=cad_engine._label_font(22))
 draw.text((28,52),f'{w:.2f} × {d:.2f} × {h:.2f} mm · {info["volume_mm3"]/1000:.2f} cm³ · {info["components"]} component(s)',fill='#27322e',font=font)
 for i,(label,yaw,pitch,extent) in enumerate((('FRONT',0,0,(w,h)),('RIGHT',-90,0,(d,h)),('TOP',0,90,(w,d)))):
  picture=cad_engine._render_view(mesh.vertices,mesh.faces,yaw,pitch,340,280,ss=1,ground=False,fit=.68,color=info.get('render_color'))
  data=io.BytesIO();picture.save(data,format='PNG');encoded=base64.b64encode(data.getvalue()).decode();x=28+i*370
  sheet.paste(picture,(x,95));draw.text((x+140,75),label,fill='#27322e',font=font);draw.line((x+40,395,x+300,395),fill='#687970');draw.text((x+120,407),f'{extent[0]:.2f} mm',fill='#27322e',font=small);draw.text((x+120,429),f'Height {extent[1]:.2f} mm',fill='#27322e',font=small)
  panels.append(f'<g transform="translate({x},95)"><text x="170" y="-15" text-anchor="middle" class="label">{label}</text><image href="data:image/png;base64,{encoded}" width="340" height="280"/><path d="M40 300 H300 M40 290 V307 M300 290 V307" class="dimension"/><text x="170" y="325" text-anchor="middle">{extent[0]:.2f} mm</text><path d="M350 30 V250 M343 30 H357 M343 250 H357" class="dimension"/><text transform="translate(365 140) rotate(-90)" text-anchor="middle">{extent[1]:.2f} mm</text></g>')
 title=escape(path.stem)
 text=f'<svg xmlns="http://www.w3.org/2000/svg" width="1160" height="530" viewBox="0 0 1160 530"><style>text{{font-family:Arial,sans-serif;font-size:13px;fill:#27322e}}.label{{font-size:14px;font-weight:bold}}.dimension{{fill:none;stroke:#687970;stroke-width:1}}</style><rect width="1160" height="530" fill="#f8f7f3"/><text x="28" y="32" style="font-size:21px;font-weight:bold">{title} · measured views</text><text x="28" y="59">{w:.2f} × {d:.2f} × {h:.2f} mm · {info["volume_mm3"]/1000:.2f} cm³ · {info["components"]} component(s)</text>{"".join(panels)}<text x="28" y="470">Dimensions describe the exported bounds. Views are rendered previews, not a fabrication drawing.</text><text x="28" y="495">Review fits, local wall thickness, supports and loads before manufacturing.</text></svg>'
 path.write_text(text,'utf-8')
 draw.text((28,470),'Dimensions describe the exported bounds. Views are rendered previews, not a fabrication drawing.',fill='#27322e',font=small)
 draw.text((28,495),'Review fits, local wall thickness, supports and loads before manufacturing.',fill='#27322e',font=small);sheet.save(path.with_suffix('.png'))
