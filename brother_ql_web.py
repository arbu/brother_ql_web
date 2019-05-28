#!/usr/bin/env python

"""
This is a web service to print labels on Brother QL label printers.
"""

import sys, logging, random, json, argparse
from io import BytesIO

from bottle import run, route, get, post, response, request, jinja2_view as view, static_file, redirect
from PIL import Image, ImageDraw, ImageFont

from brother_ql.devicedependent import models, label_type_specs, label_sizes
from brother_ql.devicedependent import ENDLESS_LABEL, DIE_CUT_LABEL, ROUND_DIE_CUT_LABEL
from brother_ql import BrotherQLRaster, create_label
from brother_ql.backends import backend_factory, guess_backend

import fontconfig

logger = logging.getLogger(__name__)

LABEL_SIZES = [ (name, label_type_specs[name]['name']) for name in label_sizes]

try:
    with open('config.json') as fh:
        CONFIG = json.load(fh)
except FileNotFoundError as e:
    with open('config.example.json') as fh:
        CONFIG = json.load(fh)


@route('/')
def index():
    redirect('/labeldesigner')

@route('/static/<filename:path>')
def serve_static(filename):
    return static_file(filename, root='./static')

@route('/labeldesigner')
@view('labeldesigner.jinja2')
def labeldesigner():
    font_info = [(i, font[1], font[2]) for i, font in enumerate(CONFIG['FONTS'])]
    return {'font_info': json.dumps(font_info),
            'default_font': CONFIG['LABEL']['DEFAULT_FONT'],
            'label_sizes': LABEL_SIZES,
            'website': CONFIG['WEBSITE'],
            'label': CONFIG['LABEL']}

def get_label_context(request):
    """ might raise LookupError() """

    d = request.params.decode() # UTF-8 decoded form data

    context = {
      'text':          d.get('text', None),
      'font_size': int(d.get('font_size', 100)),
      'label_size':    d.get('label_size', "62"),
      'kind':          label_type_specs[d.get('label_size', "62")]['kind'],
      'margin':    int(d.get('margin', 10)),
      'threshold': int(d.get('threshold', 70)),
      'align':         d.get('align', 'center'),
      'orientation':   d.get('orientation', 'standard'),
      'margin_top':    float(d.get('margin_top',    24))/100.,
      'margin_bottom': float(d.get('margin_bottom', 45))/100.,
      'margin_left':   float(d.get('margin_left',   35))/100.,
      'margin_right':  float(d.get('margin_right',  35))/100.,
    }
    context['margin_top']    = int(context['font_size']*context['margin_top'])
    context['margin_bottom'] = int(context['font_size']*context['margin_bottom'])
    context['margin_left']   = int(context['font_size']*context['margin_left'])
    context['margin_right']  = int(context['font_size']*context['margin_right'])

    try:
        font_index = int(d.get('font_index', CONFIG['LABEL']['DEFAULT_FONT']))
        context['font_path'] = CONFIG['FONTS'][font_index][0]
    except KeyError:
        raise LookupError("Couln't find the font")

    try:
        width, height = label_type_specs[context['label_size']]['dots_printable']
    except KeyError:
        raise LookupError("Unknown label_size")

    if height > width: width, height = height, width
    if context['orientation'] == 'rotated': height, width = width, height
    context['width'], context['height'] = width, height

    return context

def create_label_im(text, **kwargs):
    label_type = kwargs['kind']
    im_font = ImageFont.truetype(kwargs['font_path'], kwargs['font_size'])
    im = Image.new('L', (20, 20), 'white')
    draw = ImageDraw.Draw(im)
    # workaround for a bug in multiline_textsize()
    # when there are empty lines in the text:
    lines = []
    for line in text.split('\n'):
        if line == '': line = ' '
        lines.append(line)
    text = '\n'.join(lines)
    linesize = im_font.getsize(text)
    textsize = draw.multiline_textsize(text, font=im_font)
    width, height = kwargs['width'], kwargs['height']
    if kwargs['orientation'] == 'standard':
        if label_type in (ENDLESS_LABEL,):
            height = textsize[1] + kwargs['margin_top'] + kwargs['margin_bottom']
    elif kwargs['orientation'] == 'rotated':
        if label_type in (ENDLESS_LABEL,):
            width = textsize[0] + kwargs['margin_left'] + kwargs['margin_right']
    im = Image.new('L', (width, height), 'white')
    draw = ImageDraw.Draw(im)
    if kwargs['orientation'] == 'standard':
        if label_type in (DIE_CUT_LABEL, ROUND_DIE_CUT_LABEL):
            vertical_offset  = (height - textsize[1])//2
            vertical_offset += (kwargs['margin_top'] - kwargs['margin_bottom'])//2
        else:
            vertical_offset = kwargs['margin_top']
        horizontal_offset = max((width - textsize[0])//2, 0)
    elif kwargs['orientation'] == 'rotated':
        vertical_offset  = (height - textsize[1])//2
        vertical_offset += (kwargs['margin_top'] - kwargs['margin_bottom'])//2
        if label_type in (DIE_CUT_LABEL, ROUND_DIE_CUT_LABEL):
            horizontal_offset = max((width - textsize[0])//2, 0)
        else:
            horizontal_offset = kwargs['margin_left']
    offset = horizontal_offset, vertical_offset
    draw.multiline_text(offset, text, (0), font=im_font, align=kwargs['align'])
    return im

@get('/api/preview/text')
@post('/api/preview/text')
def get_preview_image():
    context = get_label_context(request)
    im = create_label_im(**context)
    return_format = request.query.get('return_format', 'png')
    if return_format == 'base64':
        import base64
        response.set_header('Content-type', 'text/plain')
        return base64.b64encode(image_to_png_bytes(im))
    else:
        response.set_header('Content-type', 'image/png')
        return image_to_png_bytes(im)

def image_to_png_bytes(im):
    image_buffer = BytesIO()
    im.save(image_buffer, format="PNG")
    image_buffer.seek(0)
    return image_buffer.read()

@post('/api/print/text')
@get('/api/print/text')
def print_text():
    """
    API to print a label

    returns: JSON

    Ideas for additional URL parameters:
    - alignment
    """

    return_dict = {'success': False}

    try:
        context = get_label_context(request)
    except LookupError as e:
        return_dict['error'] = e.msg
        return return_dict

    if context['text'] is None:
        return_dict['error'] = 'Please provide the text for the label'
        return return_dict

    im = create_label_im(**context)
    if DEBUG: im.save('sample-out.png')

    if context['kind'] == ENDLESS_LABEL:
        rotate = 0 if context['orientation'] == 'standard' else 90
    elif context['kind'] in (ROUND_DIE_CUT_LABEL, DIE_CUT_LABEL):
        rotate = 'auto'

    qlr = BrotherQLRaster(CONFIG['PRINTER']['MODEL'])
    create_label(qlr, im, context['label_size'], threshold=context['threshold'], cut=True, rotate=rotate)

    if not DEBUG:
        try:
            be = BACKEND_CLASS(CONFIG['PRINTER']['PRINTER'])
            be.write(qlr.data)
            be.dispose()
            del be
        except Exception as e:
            return_dict['message'] = str(e)
            logger.warning('Exception happened: %s', e)
            return return_dict

    return_dict['success'] = True
    if DEBUG: return_dict['data'] = str(qlr.data)
    return return_dict

def main():
    global DEBUG, BACKEND_CLASS, CONFIG
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', default=False)
    parser.add_argument('--loglevel', type=lambda x: getattr(logging, x.upper()), default=False)
    parser.add_argument('--font-folder', action="append", help='Specify additional folders for additional .ttf/.otf fonts.')
    parser.add_argument('--no-system-fonts', action="store_true", help='Do not search system folders for fonts.')
    parser.add_argument('--default-label-size', default=False, help='Label size inserted in your printer. Defaults to 62.')
    parser.add_argument('--default-orientation', default=False, choices=('standard', 'rotated'), help='Label orientation, defaults to "standard". To turn your text by 90°, state "rotated".')
    parser.add_argument('--model', default=False, choices=models, help='The model of your printer (default: QL-500)')
    parser.add_argument('printer',  nargs='?', default=False, help='String descriptor for the printer to use (like tcp://192.168.0.23:9100 or file:///dev/usb/lp0)')
    args = parser.parse_args()

    if args.printer:
        CONFIG['PRINTER']['PRINTER'] = args.printer

    if args.port:
        PORT = args.port
    else:
        PORT = CONFIG['SERVER']['PORT']

    if args.loglevel:
        LOGLEVEL = args.loglevel
    else:
        LOGLEVEL = CONFIG['SERVER']['LOGLEVEL']

    if LOGLEVEL == 'DEBUG':
        DEBUG = True
    else:
        DEBUG = False

    if args.model:
        CONFIG['PRINTER']['MODEL'] = args.model

    if args.default_label_size:
        CONFIG['LABEL']['DEFAULT_SIZE'] = args.default_label_size

    if args.default_orientation:
        CONFIG['LABEL']['DEFAULT_ORIENTATION'] = args.default_orientation

    ADDITIONAL_FONT_FOLDERS = (args.font_folder or []) + (CONFIG['SERVER']['ADDITIONAL_FONT_FOLDERS'] or [])

    NO_SYSTEM_FONTS = args.no_system_fonts or CONFIG['SERVER']['NO_SYSTEM_FONTS']


    logging.basicConfig(level=LOGLEVEL)

    try:
        selected_backend = guess_backend(CONFIG['PRINTER']['PRINTER'])
    except ValueError:
        parser.error("Couln't guess the backend to use from the printer string descriptor")
    BACKEND_CLASS = backend_factory(selected_backend)['backend_class']

    if CONFIG['LABEL']['DEFAULT_SIZE'] not in label_sizes:
        parser.error("Invalid --default-label-size. Please choose on of the following:\n:" + " ".join(label_sizes))

    if NO_SYSTEM_FONTS:
        fc = fontconfig.Config.create()
    else:
        fc = fontconfig.Config.get_current()
    if ADDITIONAL_FONT_FOLDERS:
        for folder in ADDITIONAL_FONT_FOLDERS:
            fc.app_font_add_dir(folder)

    props = fontconfig.PROP.FILE, fontconfig.PROP.FAMILY, fontconfig.PROP.STYLE
    CONFIG['FONTS'] = []
    for font in fc.font_list(fontconfig.Pattern.create(), props):
        CONFIG['FONTS'].append(tuple(font.get(prop, 0)[0] for prop in props))

    CONFIG['FONTS'].sort(key = lambda font: font[1:])

    if not CONFIG['FONTS']:
        sys.stderr.write("Not a single font was found on your system. Please install some or use the \"--font-folder\" argument.\n")
        sys.exit(2)

    for default_font in CONFIG['LABEL'].get('DEFAULT_FONTS', ()):
        for i, font in enumerate(CONFIG['FONTS']):
            if default_font['family'] == font[1] and default_font['style'] == font[2]:
                CONFIG['LABEL']['DEFAULT_FONT'] = i
                logger.debug("Selected the following default font: {}".format(font))
                break
        if 'DEFAULT_FONT' in CONFIG['LABEL']:
            break
    else:
        sys.stderr.write('Could not find any of the default fonts. Choosing a random one.\n')
        CONFIG['LABEL']['DEFAULT_FONT'] = random.randint(0, len(CONFIG['FONTS']))
        sys.stderr.write('The default font is now set to: {1} ({2})\n'.format(*CONFIG['FONTS'][CONFIG['LABEL']['DEFAULT_FONT']]))


    run(host=CONFIG['SERVER']['HOST'], port=PORT, debug=DEBUG)

if __name__ == "__main__":
    main()
