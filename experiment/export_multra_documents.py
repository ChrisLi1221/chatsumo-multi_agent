#!/usr/bin/env python3
"""Export optional Word copies and package only anonymous manuscript files."""
from pathlib import Path
import subprocess
import tempfile
import re
import shutil
from zipfile import ZipFile, ZIP_DEFLATED
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
PAPER = ROOT / 'paper/multra_revision'
W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
NS = {'w': W}


def fix_table_widths(path):
    # Pandoc can emit zero-width LaTeX tables, which LibreOffice renders off-page.
    with ZipFile(path) as z:
        items = {n: z.read(n) for n in z.namelist()}
    root = ET.fromstring(items['word/document.xml'])
    for table in root.findall('.//w:tbl', NS):
        # Match the manuscript's fixed 9 pt table text (Word uses half-points).
        for run in table.findall('.//w:r', NS):
            rp = run.find('w:rPr', NS)
            if rp is None:
                rp = ET.Element(f'{{{W}}}rPr'); run.insert(0, rp)
            for tag in ['sz', 'szCs']:
                size = rp.find(f'w:{tag}', NS)
                if size is None: size = ET.SubElement(rp, f'{{{W}}}{tag}')
                size.set(f'{{{W}}}val', '18')
        rows = table.findall('w:tr', NS)
        count = max(len(r.findall('w:tc', NS)) for r in rows)
        proportions = [.23, .77] if count == 2 else [1 / count] * count
        widths = [round(p * 9000) for p in proportions]
        prop = table.find('w:tblPr', NS)
        width = prop.find('w:tblW', NS)
        width.set(f'{{{W}}}type', 'dxa'); width.set(f'{{{W}}}w', '9000')
        grid = table.find('w:tblGrid', NS)
        grid.clear()
        for w in widths:
            ET.SubElement(grid, f'{{{W}}}gridCol', {f'{{{W}}}w': str(w)})
        for row in rows:
            for cell, w in zip(row.findall('w:tc', NS), widths):
                cp = cell.find('w:tcPr', NS)
                if cp is None:
                    cp = ET.Element(f'{{{W}}}tcPr'); cell.insert(0, cp)
                cw = cp.find('w:tcW', NS)
                if cw is None: cw = ET.SubElement(cp, f'{{{W}}}tcW')
                cw.set(f'{{{W}}}type', 'dxa'); cw.set(f'{{{W}}}w', str(w))
    items['word/document.xml'] = ET.tostring(root, encoding='utf-8', xml_declaration=True)
    with ZipFile(path, 'w', ZIP_DEFLATED) as z:
        for name, data in items.items(): z.writestr(name, data)


def main():
    def expand(path):
        return re.sub(r'\\input\{([^}]+)\}',
                      lambda m: expand(PAPER / (m[1] if m[1].endswith('.tex') else m[1]+'.tex')),
                      path.read_text())
    source = expand(PAPER / 'main.tex')
    source = re.sub(r'(\\includegraphics(?:\[[^\]]*\])?\{[^}]+)\.pdf\}', r'\1.png}', source)
    # Use the compiled LaTeX numbering; Pandoc otherwise leaves section labels literal.
    labels = dict(re.findall(r'\\newlabel\{([^}]+)\}\{\{([^}]*)\}', (PAPER/'main.aux').read_text()))
    def reference(match):
        kind, label = match.groups()
        if label not in labels:
            raise ValueError(f'Compile LaTeX before exporting: unresolved reference {label}')
        return '('+labels[label]+')' if kind=='eqref' else labels[label]
    source = re.sub(r'\\(ref|eqref)\{([^}]+)\}', reference, source)
    source = re.sub(r'\\label\{[^}]+\}', '', source)
    # Pandoc drops hyperref's \path macro; retain its literal scientific IDs.
    source = re.sub(r'\\path\{([^}]*)\}',
                    lambda m: r'\texttt{'+re.sub(r'([_#%&$])', r'\\\1', m[1])+'}', source)
    source = re.sub(r'\\resizebox\{\\linewidth\}\{!\}\{(\\begin\{tabular\}[\s\S]*?\\end\{tabular\})\s*\}',
                    lambda m: m[1], source)
    numbers = {'table': 0, 'figure': 0}
    def caption(match):
        kind = match[2]; numbers[kind] += 1
        return match[1] + f'{kind.title()} {numbers[kind]}: '
    source = re.sub(r'(\\begin\{(table|figure)\}[\s\S]*?\\caption\{)', caption, source)
    with tempfile.TemporaryDirectory(prefix='simtio_export_') as folder:
        temp = Path(folder) / 'main.tex'; temp.write_text(source)
        subprocess.run(['pandoc', str(temp), '-f', 'latex', '-t', 'docx', '--number-sections',
                        '--resource-path=.', '--bibliography=references.bib', '--citeproc',
                        '--csl=numeric.csl', '-o', 'manuscript_anonymous.docx'], cwd=PAPER, check=True)
    fix_table_widths(PAPER / 'manuscript_anonymous.docx')
    subprocess.run(['pandoc', 'title_page.tex', '-f', 'latex', '-t', 'docx', '-o', 'title_page.docx'], cwd=PAPER, check=True)
    subprocess.run(['pandoc', 'cover_letter.tex', '-f', 'latex', '-t', 'docx', '-o', 'cover_letter.docx'], cwd=PAPER, check=True)
    for src, dest in [('highlights.txt', 'Highlights.docx'),
                      ('declaration_of_competing_interest.txt', 'Declaration_of_competing_interest.docx')]:
        subprocess.run(['pandoc', src, '-f', 'markdown', '-t', 'docx', '-o', dest], cwd=PAPER, check=True)
    # Explicit allowlist: no title page, author actions, raw local audit, or .env.
    names = ['main.tex', 'main.pdf', 'main.bbl', 'references.bib', 'numeric.csl',
             'manuscript_anonymous.docx', 'highlights.txt', 'Highlights.docx']
    files = [PAPER / n for n in names]
    for directory in ['generated_current', 'supplementary_current']:
        files.extend(p for p in (PAPER / directory).rglob('*') if p.is_file())
    # Only the six figures actually cited by this manuscript enter review packages.
    # Keep historical/alternative artwork locally, without presenting it as current.
    active_figures = sorted(set(re.findall(
        r'\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}', (PAPER/'main.tex').read_text())))
    figure_sources = [PAPER / 'figures_current' / n for n in
                      ['Framework_typography.tex', 'Framework_updated.tex',
                       'Framework_background.png', 'Framework_README.md']]
    for name in active_figures:
        pdf = PAPER / name
        files.extend([pdf, pdf.with_suffix('.png')])
    files.extend(figure_sources)
    with ZipFile(PAPER / 'simtio_anonymous_submission.zip', 'w', ZIP_DEFLATED) as z:
        for p in sorted(files): z.write(p, str(p.relative_to(PAPER)))
    source_files = [PAPER / n for n in ['main.tex', 'main.bbl', 'references.bib']]
    source_files += sorted((PAPER / 'generated_current').glob('*.tex'))
    source_files += [PAPER / name for name in active_figures]
    source_files += figure_sources
    with ZipFile(PAPER / 'manuscript_source.zip', 'w', ZIP_DEFLATED) as z:
        for p in source_files: z.write(p, str(p.relative_to(PAPER)))
    # Preserve the old local link, but the upload guide uses the current filename.
    shutil.copyfile(PAPER/'manuscript_source.zip', PAPER/'manuscript_source_draft.zip')
    with ZipFile(PAPER / 'supplementary_evidence.zip', 'w', ZIP_DEFLATED) as z:
        for p in sorted((PAPER / 'supplementary_current').rglob('*')):
            if p.is_file(): z.write(p, str(p.relative_to(PAPER)))
    print('Created anonymous source/PDF/Word/evidence package; title page remains separate.')


if __name__ == '__main__':
    main()
