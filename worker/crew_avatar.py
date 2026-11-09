"""Small, metadata-free member avatars; no remote URLs or active SVG uploads."""
import base64, io, re

SHAPES=('circle','square','diamond','triangle','clover','flower','star','heart','house','sparkle')

def avatar(value=None,color='#bd9773'):
    if not value:return {'kind':'character','shape':'clover','color':color,'eyes':'line'}
    if not isinstance(value,dict):raise ValueError('Avatar must be an object')
    if value.get('kind','character')=='image':
        raw=value.get('image','')
        if not isinstance(raw,str) or len(raw)>3000000 or not re.match(r'^data:image/(png|jpeg|webp);base64,',raw):raise ValueError('Choose a PNG, JPEG or WebP avatar')
        try:
            from PIL import Image,ImageOps
            data=base64.b64decode(raw.split(',',1)[1],validate=True)
            with Image.open(io.BytesIO(data)) as source:
                if source.width*source.height>20000000:raise ValueError('Avatar image is too large')
                image=ImageOps.fit(ImageOps.exif_transpose(source).convert('RGBA'),(256,256))
                output=io.BytesIO();image.save(output,format='PNG',optimize=True)
            return {'kind':'image','image':'data:image/png;base64,'+base64.b64encode(output.getvalue()).decode()}
        except (OSError,ValueError) as error:raise ValueError('Could not read this avatar image') from None
    shape=value.get('shape','clover');shade=value.get('color',color);eyes=value.get('eyes','line')
    if shape not in SHAPES or eyes not in ('line','round','sleepy') or not isinstance(shade,str) or not re.fullmatch('#[0-9A-Fa-f]{6}',shade):raise ValueError('Choose a valid character shape, colour and face')
    return {'kind':'character','shape':shape,'color':shade,'eyes':eyes}
