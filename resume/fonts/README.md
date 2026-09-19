# Fonts

The resume is set in Helvetica Neue. It is not committed here: it is a licensed
Apple/Linotype font and this is not ours to redistribute.

To make the rendered PDF identical to the original, copy your own Helvetica Neue
into this directory as:

    HelveticaNeue.ttf          (Regular)
    HelveticaNeue-Medium.ttf
    HelveticaNeue-Bold.ttf

On macOS the family lives at `/System/Library/Fonts/HelveticaNeue.ttc`. Split the
collection into the three faces with fontTools:

    pip install fonttools
    python -c "
    from fontTools.ttLib import TTCollection
    c = TTCollection('/System/Library/Fonts/HelveticaNeue.ttc')
    for f in c.fonts:
        name = f['name'].getDebugName(6)
        if name in ('HelveticaNeue','HelveticaNeue-Medium','HelveticaNeue-Bold'):
            f.save(f'{name}.ttf')
    "

Keep this repository **private** if you add them — both for the font licence and
because the resume carries your contact details.

Without these files the layout, spacing and line breaks are unchanged; only the
letterforms differ, falling back to Liberation Sans or whatever the system
provides.
