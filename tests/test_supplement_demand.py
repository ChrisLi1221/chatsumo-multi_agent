from collections import Counter
import xml.etree.ElementTree as ET

import pytest

from experiment.supplement_demand import scaled_demand


def test_intensity_preserves_od_and_window_and_paired_subset(tmp_path):
    root=ET.Element('routes');ET.SubElement(root,'vType',id='passenger',vClass='passenger')
    for i in range(2400):
        ET.SubElement(root,'trip',id=str(i),depart=str(i*1.5),attrib={'from':f'a{i%17}','to':f'b{i%23}','type':'passenger'})
    source=tmp_path/'source.xml';ET.ElementTree(root).write(source)
    before=source.read_bytes();small=tmp_path/'small.xml';large=tmp_path/'large.xml'
    sm=scaled_demand(source,small,1200,42);lg=scaled_demand(source,large,3600,42)
    assert sm['selected_source_ids']==lg['selected_source_ids']
    low=ET.parse(small).findall('trip');high=ET.parse(large).findall('trip')
    assert len(low)==1200 and len(high)==3600
    assert len({t.get('id') for t in high})==3600
    assert Counter(int(t.get('id'))//2 for t in low)==Counter(range(1200))
    original={t.get('id'):t.attrib for t in root.findall('trip')}
    for t in high:
        if '__' not in t.get('id'):assert t.attrib==original[t.get('id')]
        else:
            parent=original[t.get('id').split('__')[0]]
            assert t.get('from')==parent['from'] and t.get('to')==parent['to']
            assert float(t.get('depart'))==float(parent['depart'])+.75
    assert all(0<=float(t.get('depart'))<3600 for t in high)
    assert source.read_bytes()==before
    again=tmp_path/'again.xml';scaled_demand(source,again,1200,42)
    assert again.read_bytes()==small.read_bytes()
    changed=tmp_path/'changed.xml';scaled_demand(source,changed,1200,43)
    assert changed.read_bytes()!=small.read_bytes()


def test_intensity_rejects_incompatible_source(tmp_path):
    source=tmp_path/'bad.xml';source.write_text('<routes><trip id="x" depart="0" from="a" to="b"/></routes>')
    with pytest.raises(AssertionError):scaled_demand(source,tmp_path/'out.xml',1200,42)
