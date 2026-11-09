"""Supported model vocabulary and deterministic, editable CAD presets."""
import math

CAD_GUIDE='''
CAD tool guide (all coordinates/dimensions in millimetres, Z up):
Use generate_model for a finger-jointed box, electronics enclosure, lidded_enclosure with aligned screw holes and vents, bracket, vase or planter with drainage. Supply explicit outer dimensions and stock. Supply explicit dimensions rather than repeatedly testing guessed syntax.
For arbitrary models, generate_artifact(kind="stl" or "glb", content={"plan":"dimensions, features and connections","tree":NODE}). Both exports and companions are generated together.
Primitive nodes: {"shape":"box","size":[w,d,h]}, rounded_box with radius, sphere(radius), cylinder(radius,height,radius_top optional), tube(radius,inner_radius,height), torus(radius,tube), plate(size,radius), capsule(radius,height), wedge(size), prism(sides,radius,height), gear(teeth,radius,height), text(text,size,height), extrude(points:[[x,y],...],height,holes optional,twist optional), revolve(profile:[[radius,z],...]), path(points:[[x,y,z],...],radius,closed,smooth), spring(radius,wire,turns,pitch).
Boolean nodes: {"op":"union|difference|intersection|hull","children":[...]} (difference: base first, cutters after it). Shapes are centered at origin before position/rotation/scale. Keep cutters 1–2 mm past material, avoid coplanar faces and overlap parts intended as one solid. Use top-level allow_multiple:true only for intentional assemblies, never to hide disconnected broken parts.
Patterns: {"op":"grid","child":NODE,"counts":[nx,ny,nz],"spacing":[dx,dy,dz]}; {"op":"repeat","child":NODE,"count":n,"step":[dx,dy,dz]}; {"op":"radial","child":NODE,"count":n,"radius":r}.
Finishing: {"op":"round","child":NODE,"radius":r}; {"op":"shell","child":NODE,"thickness":t,"open":"top"}. Round is a geometric offset, not an engineering-certified edge fillet.
Organic: loft(sections:[[cx,cy,z,rx,ry],...],segments:64,subdivisions:3); human(height:180,build:1,arm_angle:12,resolution:144); sculpt(blend:1,resolution:112,parts:[{shape:"ellipsoid",center:[x,y,z],radii:[rx,ry,rz]},{shape:"capsule",a:[x,y,z],b:[x,y,z],radius:4}]). Sculpt detail smaller than two grid cells can vanish. Use a few shaped surfaces, not piles of disconnected spheres.
Quality: top-level units="mm|cm|in" and quality="draft|standard|high". For custom models add constraints:{size_mm:[w,d,h],tolerance_mm:0.15,components:1}; unspecified axes may be null. Optional material:{color:"#C87959",metallic:0,roughness:0.6} is stored in GLB; STL has no material/color. Exact requested sizes are checked; models are never silently rescaled. Separate assemblies export a BOM and print-oriented parts.
Quality workflow: first plan the real object's dimensions and critical features; build complete geometry; read the returned validation details and source; correct missing features/dimensions. Do not publish diagnostic cubes alongside the finished deliverable. Geometry validation establishes closed topology and volume, not wall-thickness certification, supports, likeness or real-world load safety. Beam is text-only, so do not claim a visual inspection.
Engineering assemblies: use a top-level union with one fully positioned child per part, allow_multiple:true, and assembly:{parts:["base","lid"],export_individual_parts:true,exploded_view:true,checks:[{a:"base",b:"lid",min_gap_mm:0.2,max_overlap_mm3:0.001}]}. Fit checks run on the named components before union. Add constraints.max_size_mm or min_size_mm when the model must fit an envelope. Generate and check a separate spec for each distinct mechanism pose.
'''


def number(p,name,default,minimum=.01,maximum=1000):
    try:value=float(p.get(name,default))
    except (TypeError,ValueError):raise ValueError(name+' must be a number') from None
    if not math.isfinite(value) or not minimum<=value<=maximum:raise ValueError(f'{name} must be between {minimum} and {maximum} mm')
    return value


def box(size,position):return {'shape':'box','size':size,'position':position}
def union(children):return {'op':'union','children':children}
def difference(body,cutters):return {'op':'difference','children':[body]+cutters}


def _basic_model_spec(preset,p,require_dimensions=False):
    if not isinstance(p,dict):raise ValueError('Model parameters must be an object')
    allowed={'width','depth','height','wall','finger_width','clearance','layout','radius','post_diameter','hole_diameter'}
    unknown=set(p)-allowed
    if unknown:raise ValueError('Unsupported CAD parameters: '+', '.join(sorted(unknown)))
    if require_dimensions:
        required={'height','radius','wall'} if preset=='vase' else {'width','depth','height','wall'}
        missing=required-set(p)
        if missing:raise ValueError('Missing explicit outer dimensions/stock: '+', '.join(sorted(missing))+'. Match the user’s requested dimensions; do not rely on defaults or reinterpret inner versus outer size.')
    if p.get('layout','assembled') not in ('assembled','exploded'):raise ValueError('layout must be assembled or exploded')
    t=number(p,'wall',3,.8,20)
    if preset=='vase':
        h=number(p,'height',120,20,500);r=number(p,'radius',35,8,200)
        if t>=r/3:raise ValueError('Vase wall must be less than one third of radius')
        outer=[(r*.66,0),(r*.72,h*.08),(r,h*.35),(r*.9,h*.63),(r*.52,h*.83),(r*.58,h)]
        inner=[(rr-t,max(t,z)) for rr,z in reversed(outer) if z>0]
        profile=[[0,0]]+[list(v) for v in outer]+[list(v) for v in inner]+[[r*.66-t,t],[0,t]]
        return {'plan':f'Hollow lathed vase, height {h} mm, max diameter {2*r} mm, nominal wall {t} mm, closed base.','parameters':p,'tree':{'shape':'revolve','profile':profile,'segments':144}}
    w=number(p,'width',100,20,500);d=number(p,'depth',70,20,500);h=number(p,'height',50,15,400)
    if t*4>=min(w,d,h):raise ValueError('Wall is too thick for these dimensions')
    if preset=='enclosure':
        radius=number(p,'radius',5,.1,min(w,d,h)/3)
        outer={'shape':'plate','size':[w,d,h],'radius':radius,'position':[0,0,h/2]}
        inner={'shape':'plate','size':[w-2*t,d-2*t,h+2],'radius':max(.1,radius-t),'position':[0,0,h/2+t+1]}
        body=difference(outer,[inner]);post=number(p,'post_diameter',9,4,min(w,d)/3)/2
        hole=number(p,'hole_diameter',3,1,post*1.8)/2
        if post<=hole+t*.35:raise ValueError('Screw post needs more material around its hole')
        posts=[];bores=[]
        for sx in (-1,1):
            for sy in (-1,1):
                x,y=sx*(w/2-t-post+.8),sy*(d/2-t-post+.8)
                posts.append({'shape':'cylinder','radius':post,'height':h-t,'position':[x,y,(h-t)/2]})
                bores.append({'shape':'cylinder','radius':hole,'height':h+4,'position':[x,y,h/2]})
        return {'plan':f'Open enclosure {w} × {d} × {h} mm, nominal wall {t} mm, rounded exterior, four integrated screw posts and through bores.','parameters':p,'tree':difference(union([body]+posts),bores)}
    if preset=='bracket':
        hole=number(p,'hole_diameter',5,1,min(w,d)/4)/2
        base=box([w,d,t],[0,0,t/2]);upright=box([w,t,h],[0,(d-t)/2,h/2])
        gussets=[]
        for sx in (-1,1):
            gussets.append({'shape':'wedge','size':[min(d*.55,h*.6),t*1.3,h*.58],'rotation':[0,0,-90], 'position':[sx*(w/2-t*1.8),d/2-t-d*.2,h*.29]})
        cutters=[{'shape':'cylinder','radius':hole,'height':t+4,'position':[sx*w*.27,-d*.2,t/2]} for sx in (-1,1)]
        cutters += [{'shape':'cylinder','radius':hole,'height':t+4,'rotation':[90,0,0],'position':[sx*w*.27,(d-t)/2,h*.72]} for sx in (-1,1)]
        return {'plan':f'Gusseted L bracket {w} × {d} × {h} mm, plate {t} mm, two base holes and two upright holes diameter {2*hole} mm.','parameters':p,'tree':difference(union([base,upright]+gussets),cutters)}
    if preset=='finger_joint_box':
        clearance=number(p,'clearance',.15,0,1);finger=number(p,'finger_width',8,3,h/2)
        count=max(3,min(31,round(h/finger)));count+=1-count%2
        pitch=h/count
        panels=[]
        for sy in (-1,1):
            body=box([w,t,h],[0,sy*(d-t)/2,h/2]);cuts=[]
            for i in range(count):
                if i%2==0:
                    for sx in (-1,1):cuts.append(box([t+clearance+1,t+4,pitch+clearance],[sx*(w/2-t/2+.5),sy*(d-t)/2,(i+.5)*pitch]))
            panel=difference(body,cuts)
            if p.get('layout')=='exploded':panel={'op':'union','children':[panel],'position':[0,sy*(d*.65),0]}
            panels.append(panel)
        for sx in (-1,1):
            body=box([t,d,h],[sx*(w-t)/2,0,h/2]);cuts=[]
            for i in range(count):
                if i%2==1:
                    for sy in (-1,1):cuts.append(box([t+4,t+clearance+1,pitch+clearance],[sx*(w-t)/2,sy*(d/2-t/2+.5),(i+.5)*pitch]))
            panel=difference(body,cuts)
            if p.get('layout')=='exploded':panel={'op':'union','children':[panel],'position':[sx*(w*.65),0,0]}
            panels.append(panel)
        floor=box([w-2*t-clearance*2,d-2*t-clearance*2,t],[0,0,t/2])
        if p.get('layout')=='exploded':floor['position'][2]=-h*.6
        panels.append(floor)
        return {'plan':f'Five-panel open box {w} × {d} × {h} mm, {count} alternating vertical corner fingers, stock {t} mm, clearance {clearance} mm. Inset floor is a separate panel and requires securing; no lid.','parameters':p,'allow_multiple':True,'assembly':{'export_individual_parts':True,'parts':['front','back','left','right','inset floor'],'layout':p.get('layout','assembled'),'finger_count':count,'clearance_mm':clearance},'tree':union(panels)}
    raise ValueError('Unknown CAD preset')


def model_spec(preset,p,require_dimensions=False):
    if not isinstance(p,dict):raise ValueError('Model parameters must be an object')
    if preset not in ('lidded_enclosure','planter'):
        result=_basic_model_spec(preset,p,require_dimensions)
    elif preset=='lidded_enclosure':
        allowed={'width','depth','height','wall','radius','post_diameter','hole_diameter','clearance','vent_count'}
        if set(p)-allowed:raise ValueError('Unsupported enclosure parameters: '+', '.join(sorted(set(p)-allowed)))
        if require_dimensions and {'width','depth','height','wall'}-set(p):raise ValueError('Supply explicit width/depth/height/wall for the closed enclosure')
        w=number(p,'width',100,30,500);d=number(p,'depth',70,30,500);h=number(p,'height',50,20,400);t=number(p,'wall',3,.8,15);gap=number(p,'clearance',.2,.05,1)
        body_p={k:v for k,v in p.items() if k not in ('clearance','vent_count')};body_p.update(width=w,depth=d,height=h-t-gap,wall=t)
        body=_basic_model_spec('enclosure',body_p)['tree'];r=number(p,'radius',5,.1,min(w,d,h)/3)
        post=number(p,'post_diameter',9,4,min(w,d)/3)/2;hole=number(p,'hole_diameter',3,1,post*1.8)/2
        lid={'shape':'plate','size':[w,d,t],'radius':r,'position':[0,0,h-t/2]};cuts=[]
        for sx in (-1,1):
            for sy in (-1,1):
                x,y=sx*(w/2-t-post+.8),sy*(d/2-t-post+.8)
                cuts.append({'shape':'cylinder','radius':hole,'height':t+2,'position':[x,y,h-t/2]})
                cuts.append({'shape':'cylinder','radius':hole*1.8,'height':t*.5,'position':[x,y,h-t*.1]})
        vents=int(number(p,'vent_count',6,0,20))
        if vents!=p.get('vent_count',vents):raise ValueError('vent_count must be an integer')
        for i in range(vents):
            cuts.append({'shape':'plate','size':[w*.48,max(.8,min(t,d*.45/max(vents,1)/2)),t+2],'radius':.4,'position':[0,(i-(vents-1)/2)*d*.45/max(vents,1),h-t/2]})
        lid=difference(lid,cuts)
        result={'plan':f'Two-part closed enclosure {w} × {d} × {h} mm, nominal wall {t} mm, {gap} mm lid gap, four aligned through screw bores/head recesses and {vents} lid vents. Hardware and thread fit require review.','parameters':p,'allow_multiple':True,'tree':union([body,lid]),'assembly':{'export_individual_parts':True,'parts':['body','lid'],'print_rotations':[[0,0,0],[180,0,0]]}}
    else:
        allowed={'height','radius','wall','drain_diameter','drain_count'}
        if set(p)-allowed:raise ValueError('Unsupported planter parameters: '+', '.join(sorted(set(p)-allowed)))
        if require_dimensions and {'height','radius','wall'}-set(p):raise ValueError('Supply explicit height/radius/wall for the planter')
        h=number(p,'height',100,20,400);r=number(p,'radius',45,10,200);t=number(p,'wall',3,.8,min(h,r)/4)
        diameter=number(p,'drain_diameter',5,1,r/3);count=int(number(p,'drain_count',5,1,13))
        if count!=p.get('drain_count',count):raise ValueError('drain_count must be an integer')
        profile=[[0,0],[r*.72,0],[r,h],[r-t,h],[r*.72-t,t],[0,t]]
        body={'shape':'revolve','profile':profile,'anchor':'bottom'};cuts=[{'shape':'cylinder','radius':diameter/2,'height':t+2,'position':[0,0,t/2]}]
        for i in range(count-1):
            a=2*math.pi*i/max(1,count-1);cuts.append({'shape':'cylinder','radius':diameter/2,'height':t+2,'position':[r*.38*math.cos(a),r*.38*math.sin(a),t/2]})
        result={'plan':f'Tapered hollow planter, maximum diameter {2*r} mm, height {h} mm, nominal wall/base {t} mm, {count} drainage holes diameter {diameter} mm.','parameters':p,'tree':difference(body,cuts)}
    result['quality']='high';result['units']='mm'
    if p.get('layout','assembled')!='exploded':
        if preset in ('vase','planter'):size=[number(p,'radius',35 if preset=='vase' else 45)*2]*2+[number(p,'height',120 if preset=='vase' else 100)]
        else:size=[number(p,'width',100),number(p,'depth',70),number(p,'height',50)]
        result['constraints']={'size_mm':size,'tolerance_mm':.15,'components':5 if preset=='finger_joint_box' else 2 if preset=='lidded_enclosure' else 1}
    return result
