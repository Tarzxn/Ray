"""Presentation layouts, Office charts and editorial document styling for Beam."""
import json
import math
from pathlib import Path
import tempfile
import threading
import artifacts as base

_ORIGINAL = {k:getattr(base,'write_'+k) for k in ('docx','pdf','xlsx','pptx')}
_INSTALLED = False
_LOCK = threading.RLock()
INK, PAPER, ACCENT, MUTED = '252722','F8F7F3','C87959','777970'


def object_content(content):
    if isinstance(content,str):
        try: return json.loads(content)
        except ValueError: return content
    return content


def markdown_content(content):
    content=object_content(content)
    if isinstance(content,dict):
        text=str(content.get('markdown',content.get('content','')))
        if content.get('title') and not text.lstrip().startswith('# '):
            text='# '+str(content['title'])+'\n\n'+(str(content.get('subtitle',''))+'\n\n' if content.get('subtitle') else '')+text
        if not text.strip(): raise ValueError('Document needs markdown content')
        return text
    return str(content)


def write_docx(path,content):
    from docx import Document
    from docx.shared import Pt,RGBColor
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    _ORIGINAL['docx'](path,markdown_content(content))
    doc=Document(path)
    data=object_content(content)
    if isinstance(data,dict):
        size=data.get('page_size','Letter')
        if size not in ('A4','Letter'):raise ValueError('DOCX page_size must be A4 or Letter')
        from docx.shared import Mm
        for section in doc.sections:
            section.page_width=Mm(210 if size=='A4' else 215.9);section.page_height=Mm(297 if size=='A4' else 279.4)
        doc.core_properties.subject=str(data.get('subtitle',''))[:255]
    for name in ('Normal','Body Text'):
        doc.styles[name].font.name='Aptos'
        doc.styles[name].font.size=Pt(11)
        doc.styles[name].paragraph_format.line_spacing=1.18
        doc.styles[name].paragraph_format.space_after=Pt(8)
    for name,size,color in [('Heading 1',27,INK),('Heading 2',17,INK),('Heading 3',13,ACCENT),('Heading 4',11,INK)]:
        style=doc.styles[name]
        style.font.name='Aptos Display'
        style.font.size=Pt(size)
        style.font.color.rgb=RGBColor.from_string(color)
        style.paragraph_format.keep_with_next=True
        style.paragraph_format.space_before=Pt(18)
        style.paragraph_format.space_after=Pt(8)
    for paragraph in doc.paragraphs:
        paragraph.paragraph_format.widow_control=True
    for table in doc.tables:
        for row in table.rows:
            props=row._tr.get_or_add_trPr()
            props.append(OxmlElement('w:cantSplit'))
        for cell in table.rows[0].cells:
            shading=cell._tc.get_or_add_tcPr().find(qn('w:shd'))
            if shading is not None: shading.set(qn('w:fill'),INK)
            for run in cell.paragraphs[0].runs: run.font.color.rgb=RGBColor(255,255,255)
    for section in doc.sections:
        section.header.paragraphs[0].text=doc.core_properties.title[:90]
        for run in section.header.paragraphs[0].runs:
            run.font.size=Pt(8)
            run.font.color.rgb=RGBColor.from_string(MUTED)
    doc.core_properties.author=str(data.get('author','Beam'))[:120] if isinstance(data,dict) else 'Beam'
    doc.save(path)


def write_pdf(path,content):
    data=object_content(content)
    _ORIGINAL['pdf'](path,markdown_content(content),page_size=data.get('page_size','Letter') if isinstance(data,dict) else 'Letter')


def write_xlsx(path,content):
    from openpyxl import load_workbook
    from openpyxl.chart import BarChart,LineChart,PieChart,Reference
    from openpyxl.styles import Font,PatternFill,Alignment
    from openpyxl.formatting.rule import ColorScaleRule
    from openpyxl.utils.cell import column_index_from_string
    data=object_content(content)
    _ORIGINAL['xlsx'](path,data)
    descriptors=data.get('sheets',[data] if 'rows' in data else []) if isinstance(data,dict) else []
    if not isinstance(descriptors,list): descriptors=[]
    wb=load_workbook(path)
    for index,ws in enumerate(wb.worksheets):
        ws.sheet_view.showGridLines=False
        ws.row_dimensions[1].height=30
        for cell in ws[1]:
            cell.fill=PatternFill('solid',fgColor=INK)
            cell.font=Font(name='Aptos',bold=True,color='FFFFFF',size=11)
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                cell.font=Font(name='Aptos',size=11,color='2676A5' if cell.data_type=='f' else INK)
        spec=descriptors[index] if index<len(descriptors) and isinstance(descriptors[index],dict) else {}
        for column,fmt in spec.get('formats',{}).items():
            if not isinstance(fmt,str) or len(fmt)>100: raise ValueError('Invalid column number format')
            number=column_index_from_string(column.upper())
            if number>ws.max_column: raise ValueError(f'Format column {column} does not exist')
            for row in ws.iter_rows(min_row=2,min_col=number,max_col=number): row[0].number_format=fmt
        for column in spec.get('color_scales',[]):
            number=column_index_from_string(column.upper())
            if number>ws.max_column: raise ValueError('Color scale column does not exist')
            ws.conditional_formatting.add(f'{column}2:{column}{ws.max_row}',ColorScaleRule(start_type='min',start_color='FAEDE3',end_type='max',end_color=ACCENT))
        chart_specs=list(spec.get('charts',[]))
        if isinstance(data,dict) and 'sheets' in data:
            chart_specs += [c for c in data.get('charts',[]) if (c.get('sheet') == ws.title or (not c.get('sheet') and index==0))]
        if len(chart_specs)>8:raise ValueError('Use at most eight charts per sheet; no charts were silently dropped')
        for chart_index,chart_spec in enumerate(chart_specs):
            if ws.max_row<2:raise ValueError('Chart needs a header and at least one data row')
            chart_type=chart_spec.get('type','bar')
            if chart_type not in {'bar','line','pie'}: raise ValueError('Spreadsheet chart type must be bar, line or pie')
            chart={'bar':BarChart,'line':LineChart,'pie':PieChart}[chart_type]()
            chart.title=str(chart_spec.get('title',''))
            chart.style=10
            values=chart_spec.get('values',['B'])
            if isinstance(values,str): values=[values]
            for column in values:
                number=column_index_from_string(column.upper())
                if number>ws.max_column: raise ValueError('Chart value column does not exist')
                if not any(isinstance(ws.cell(row,number).value,(int,float)) or ws.cell(row,number).data_type=='f' for row in range(2,ws.max_row+1)):raise ValueError('Chart value columns must contain numeric values or formulas')
                chart.add_data(Reference(ws,min_col=number,max_col=number,min_row=1,max_row=ws.max_row),titles_from_data=True)
            label_col=column_index_from_string(chart_spec.get('labels','A').upper())
            if label_col>ws.max_column: raise ValueError('Chart label column does not exist')
            chart.set_categories(Reference(ws,min_col=label_col,min_row=2,max_row=ws.max_row))
            chart.width,chart.height=22,12
            from openpyxl.utils import get_column_letter
            ws.add_chart(chart,f'{get_column_letter(ws.max_column+2)}{2+chart_index*25}')
        ws.print_title_rows='1:1'
    wb.properties.creator='Beam'
    wb.save(path)
    wb.close()


def write_pptx(path,content):
    from pptx import Presentation
    from pptx.util import Inches,Pt
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import MSO_ANCHOR
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    data=object_content(content)
    theme=data.get('theme',{}) if isinstance(data,dict) else {}
    import re
    def color(key,default):
        value=str(theme.get(key,default)).lstrip('#').upper()
        if not re.fullmatch('[0-9A-F]{6}',value):raise ValueError('Theme colors must be six hexadecimal digits')
        return value
    INK,PAPER,ACCENT,MUTED=color('text','252722'),color('background','F8F7F3'),color('accent','C87959'),color('muted','777970')
    brand=str(theme.get('brand',''))[:80]
    slides=data.get('slides') if isinstance(data,dict) else data
    if not isinstance(slides,list) or not slides: raise ValueError('Presentation needs a non-empty slides array')
    if len(slides)>100: raise ValueError('Presentation exceeds 100 input slides')
    prs=Presentation()
    prs.slide_width,prs.slide_height=Inches(13.333),Inches(7.5)
    def shape(slide,x,y,w,h,color,rounded=False):
        box=slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE if rounded else MSO_SHAPE.RECTANGLE,Inches(x),Inches(y),Inches(w),Inches(h))
        box.fill.solid();box.fill.fore_color.rgb=RGBColor.from_string(color);box.line.fill.background()
        return box
    def text(slide,txt,x,y,w,h,size=22,color=INK,bold=False,font='Aptos',fit=True):
        txt=base.strip_control(str(txt))
        box=slide.shapes.add_textbox(Inches(x),Inches(y),Inches(w),Inches(h))
        tf=box.text_frame;tf.word_wrap=True
        tf.margin_left=tf.margin_right=0
        tf.margin_top=tf.margin_bottom=0
        # Estimate wrapping before committing text; reject gross overflow instead of silently clipping.
        def needed(s): return sum(max(1,math.ceil(len(line)/max(8,w*72/(s*.51)))) for line in txt.split('\n'))*s*1.24/72
        while fit and size>14 and needed(size)>h: size-=1
        if needed(size)>h*1.25: raise ValueError('Slide text is too long. Split the content into more slides.')
        for i,line in enumerate(txt.split('\n')):
            p=tf.paragraphs[0] if i==0 else tf.add_paragraph()
            p.text=line;p.font.name=font;p.font.size=Pt(size);p.font.bold=bold;p.font.color.rgb=RGBColor.from_string(color)
            p.space_after=Pt(5)
        return box
    def new(title,notes='',dark=False):
        slide=prs.slides.add_slide(prs.slide_layouts[6])
        slide.background.fill.solid();slide.background.fill.fore_color.rgb=RGBColor.from_string(INK if dark else PAPER)
        if title:
            text(slide,title,.72,.58,11.8,1.0,31,'FFFFFF' if dark else INK,True)
            shape(slide,.72,1.71,.6,.045,ACCENT)
        text(slide,brand,.72,7.05,3,.2,9,'A9AB9E' if dark else MUTED,False)
        text(slide,f'{len(prs.slides):02d}',12.1,7.05,.5,.2,9,'A9AB9E' if dark else MUTED)
        if notes: slide.notes_slide.notes_text_frame.text=str(notes)
        return slide
    for index,item in enumerate(slides):
        if not isinstance(item,dict): raise ValueError('Every slide must be an object')
        title=str(item.get('title',f'Slide {index+1}'))
        layout=item.get('layout','content')
        notes=item.get('notes',item.get('speaker_notes',''))
        if layout in {'title','section'}:
            slide=new('',notes,True)
            text(slide,str(item.get('eyebrow','')),.85,1.3,10,.4,11,'C5B9A8')
            shape(slide,.85,2.03,1.0,.06,ACCENT)
            text(slide,title,.85,2.45,11.6,2.25,48,'FFFFFF',True)
            if item.get('subtitle'):text(slide,item['subtitle'],.85,5.13,11.3,1.0,22,'C5C7BE')
        elif layout=='metrics':
            metrics=item.get('metrics',[])
            if not 1<=len(metrics)<=4: raise ValueError('Metric slides need 1–4 metrics')
            slide=new(title,notes)
            w=(11.85-.25*(len(metrics)-1))/len(metrics)
            for i,metric in enumerate(metrics):
                x=.72+i*(w+.25)
                shape(slide,x,2.45,w,3.35,'EEECE5',True)
                text(slide,metric.get('value',''),x+.24,2.95,w-.48,1.12,46,ACCENT,True)
                text(slide,metric.get('label',''),x+.24,4.35,w-.48,1.0,20,INK)
        elif layout=='process':
            steps=item.get('steps',[])
            if not 2<=len(steps)<=5:raise ValueError('Process slides need 2–5 steps')
            slide=new(title,notes);w=(11.85-.2*(len(steps)-1))/len(steps)
            for i,step in enumerate(steps):
                if not isinstance(step,dict):raise ValueError('Every process step needs title and body')
                x=.72+i*(w+.2);shape(slide,x,2.2,w,4.25,'EEECE5',True)
                text(slide,str(i+1).zfill(2),x+.2,2.5,w-.4,.6,25,ACCENT,True)
                text(slide,step.get('title',''),x+.2,3.35,w-.4,.9,22,INK,True)
                text(slide,step.get('body',''),x+.2,4.5,w-.4,1.6,18)
        elif layout=='two-column':
            slide=new(title,notes)
            for index,column in enumerate(('left','right')):
                spec=item.get(column,{})
                x=.72+index*6.1
                shape(slide,x,2.1,5.75,4.5,'EEECE5' if index==0 else 'EAEDE7',True)
                text(slide,spec.get('title',''),x+.28,2.4,5.19,.7,23,INK,True)
                bullets=spec.get('bullets',[])
                text(slide,'\n'.join('• '+str(b) for b in bullets),x+.28,3.34,5.19,2.85,21)
        elif layout=='chart' or item.get('chart'):
            spec=item.get('chart',{})
            labels=spec.get('labels',[])
            if not labels or len(labels)>40: raise ValueError('Slide charts need 1–40 labels')
            chart_data=CategoryChartData();chart_data.categories=labels
            for series in spec.get('series',[]):
                values=series.get('values',[])
                if len(values)!=len(labels) or not all(isinstance(v,(int,float)) and math.isfinite(v) for v in values):
                    raise ValueError('Chart values must be finite numbers, one per label')
                chart_data.add_series(str(series.get('name','Series')),values)
            if not spec.get('series'): raise ValueError('Chart needs a data series')
            kind={'bar':XL_CHART_TYPE.COLUMN_CLUSTERED,'line':XL_CHART_TYPE.LINE,'pie':XL_CHART_TYPE.PIE}.get(spec.get('type','bar'))
            if kind is None: raise ValueError('Chart type must be bar, line or pie')
            slide=new(title,notes)
            chart=slide.shapes.add_chart(kind,Inches(.85),Inches(2.13),Inches(11.6),Inches(4.45),chart_data).chart
            chart.has_legend=len(spec['series'])>1 or spec.get('type')=='pie'
            chart.chart_style=10
            if item.get('source'):text(slide,item['source'],.85,6.65,11.5,.3,10,MUTED)
        elif item.get('table'):
            rows=base._rows_from(item['table'])
            cols=max(len(r) for r in rows)
            if cols>8: raise ValueError('Slide tables support at most 8 columns; split the table')
            for offset in range(1,max(2,len(rows)),7):
                group=[rows[0]]+rows[offset:offset+7]
                slide=new(title+(' (continued)' if offset>1 else ''),notes if offset==1 else '')
                table=slide.shapes.add_table(len(group),cols,Inches(.85),Inches(2.12),Inches(11.65),Inches(.53*len(group))).table
                for ri,row in enumerate(group):
                    for ci in range(cols):
                        cell=table.cell(ri,ci)
                        value=str(row[ci]) if ci<len(row) and row[ci] is not None else ''
                        if len(value)>90: raise ValueError('Slide table cell too long; shorten or split the table')
                        cell.text=value
                        cell.fill.solid();cell.fill.fore_color.rgb=RGBColor.from_string(INK if ri==0 else ('EAE8E0' if ri%2 else PAPER))
                        cell.margin_left=Inches(.12);cell.margin_top=Inches(.08)
                        for p in cell.text_frame.paragraphs:
                            p.font.name='Aptos';p.font.size=Pt(14 if cols<6 else 12);p.font.bold=ri==0;p.font.color.rgb=RGBColor.from_string('FFFFFF' if ri==0 else INK)
        else:
            items=base._slide_lines(item.get('bullets',item.get('body',[])))
            if not items:
                slide=new(title,notes)
                if item.get('subtitle'):text(slide,item['subtitle'],.85,2.5,11.5,2.5,28)
            for chunk_index,chunk in enumerate(base._chunk_items(items)):
                slide=new(title+(' (continued)' if chunk_index else ''),notes if chunk_index==0 else '')
                text(slide,'\n'.join(('    '*depth)+'• '+line for line,depth in chunk),.95,2.22,11.3,4.32,25)
    prs.core_properties.title=str(slides[0].get('title','Presentation'))[:120]
    prs.core_properties.author='Beam'
    prs.save(path)


def install():
    global _INSTALLED
    with _LOCK:
        if not _INSTALLED:
            for name in ('docx','pdf','xlsx','pptx'): setattr(base,'write_'+name,globals()['write_'+name])
            _INSTALLED=True


def produce_artifact(root,item,cancelled=None,progress=None):
    from beam_core import project_path
    install()
    # Validate nested archive paths too. Output staging remains atomic in the original engine.
    def check(value,depth=0):
        if depth>4: raise ValueError('Archive nesting exceeds four levels')
        project_path(root,value['path'])
        if value.get('kind')=='zip':
            entries=object_content(value['content'])
            if isinstance(entries,dict): entries=entries.get('files',[])
            if isinstance(entries,list):
                for entry in entries: check(entry,depth+1)
    check(item)
    result=base.produce_artifact(root,item,cancelled=cancelled,progress=progress)
    if result['kind'] in ('stl','glb'):
        report=Path(root)/Path(result['path']).with_name(Path(result['path']).stem+'-validation.json')
        if report.exists():
            info=json.loads(report.read_text('utf-8'))
            result['details']={k:info[k] for k in ('size_mm','volume_mm3','triangles','components','watertight','winding_consistent','assembly_parts','assembly_checks','exploded_view','parts_archive','bill_of_materials','dimension_sheet','dimension_checks','surface_area_mm2','quality') if k in info}
            if info.get('parts_archive'):result['details']['parts_archive']=(Path(result['path']).parent/info['parts_archive']).as_posix()
            result['details']['source']=str(Path(result['path']).with_suffix('.model.json')).replace('\\','/')
            result['details']['validation']=str(report.relative_to(root)).replace('\\','/')
    if result['kind']=='xlsx':
        from openpyxl import load_workbook
        wb=load_workbook(Path(root)/result['path'])
        formulas=sum(cell.data_type=='f' for ws in wb for row in ws for cell in row)
        result['details']={'sheets':wb.sheetnames,'charts':sum(len(ws._charts) for ws in wb),'formulas':formulas}
        wb.close()
        if not formulas: result['warnings']=[w for w in result['warnings'] if 'recalculation' not in w]
    return result


def self_test():
    with tempfile.TemporaryDirectory(prefix='beam-selftest-') as temp:
        cases=[('report.docx','docx',{'title':'Beam validation','markdown':'## Results\n\nA real document with **formatted text**.\n\n| Check | Result |\n|---|---|\n| Export | Passed |'}),
               ('report.pdf','pdf','# Beam validation\n\nCafé — a complete PDF.'),
               ('budget.xlsx','xlsx',{'sheets':[{'name':'Budget','rows':[['Month','Value'],['Jan',20],['Feb',30]],'formats':{'B':'£#,##0.00'},'charts':[{'type':'bar','title':'Budget','labels':'A','values':['B']}]}]}),
               ('deck.pptx','pptx',{'slides':[{'layout':'title','title':'Build something good','subtitle':'Beam desktop'},
                 {'layout':'metrics','title':'By the numbers','metrics':[{'value':'4','label':'Verified formats'},{'value':'1','label':'Desktop workspace'}]},
                 {'layout':'two-column','title':'Compare options','left':{'title':'Today','bullets':['Local files','Cloud reasoning']},'right':{'title':'Tomorrow','bullets':['Better workflows','Your next project']}},
                 {'layout':'chart','title':'Progress','chart':{'labels':['A','B'],'series':[{'name':'Tasks','values':[2,4]}]}}]})]
        results=[produce_artifact(temp,{'path':name,'kind':kind,'content':content}) for name,kind,content in cases]
        from cad_templates import model_spec
        for preset in ('enclosure','bracket','vase','finger_joint_box','lidded_enclosure','planter'):
            results.append(produce_artifact(temp,{'path':preset+'.stl','kind':'stl','content':model_spec(preset,{})}))
        from agent_runtime import runtime_info,run_command
        runtime=runtime_info()
        command_check=run_command('python -c "print(123)"',temp,15) if runtime['python'] else {'status':'Python not installed on host; external coding commands unavailable'}
        return {'ok':True,'files':results,'external_python_check':command_check}
