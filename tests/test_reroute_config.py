import xml.etree.ElementTree as ET

from providers.Modification.reroute import write_rerouted_sumocfg
from experiment.revision_matched import candidate_seed


def test_rerouting_preserves_seed_time_and_existing_signal_program(tmp_path):
    source=tmp_path/'source';source.mkdir()
    (source/'signals.add.xml').write_text('<additional><tlLogic id="J"/></additional>')
    (source/'edges.add.xml').write_text('<additional><edgeData id="E" file="old.xml" period="60"/></additional>')
    cfg=source/'sim.sumocfg'
    cfg.write_text('''<configuration><input><net-file value="old.net.xml"/>
      <route-files value="old.rou.xml"/><additional-files value="signals.add.xml,edges.add.xml"/></input>
      <time><begin value="5"/><end value="8000"/><step-length value="0.5"/></time>
      <random_number><seed value="44"/></random_number></configuration>''')
    original=cfg.read_bytes();out=tmp_path/'modified'
    path=write_rerouted_sumocfg(str(cfg),str(out),str(tmp_path/'new.net.xml'),str(tmp_path/'new.rou.xml'))
    root=ET.parse(path).getroot()
    assert root.find('.//seed').get('value')=='44'
    assert root.find('.//step-length').get('value')=='0.5'
    assert root.find('.//end').get('value')=='8000'
    assert root.find('.//route-files').get('value')==str(tmp_path/'new.rou.xml')
    adds=root.find('.//additional-files').get('value').split(',')
    assert str(source/'signals.add.xml') in adds
    edges=[ET.parse(p).find('.//edgeData') for p in adds]
    edge=next(e for e in edges if e is not None)
    assert edge.get('file')==str(out/'edgedata.xml')
    assert cfg.read_bytes()==original


def test_rerouting_adds_measurements_if_source_has_none(tmp_path):
    cfg=tmp_path/'source.sumocfg';cfg.write_text('<configuration><random_number><seed value="43"/></random_number></configuration>')
    out=tmp_path/'new';path=write_rerouted_sumocfg(str(cfg),str(out),'n.xml','r.xml')
    root=ET.parse(path).getroot();add=root.find('.//additional-files').get('value')
    assert ET.parse(out/add).find('.//edgeData').get('file')==str(out/'edgedata.xml')
    assert root.find('.//seed').get('value')=='43'


def test_executed_seed_comes_from_tripinfo_not_run_label(tmp_path):
    trip=tmp_path/'tripinfo.xml'
    c={'sumo_seed':44,'optimized':{'artifacts':{'tripinfo':str(trip)}}}
    trip.write_text('<!-- <sumoConfiguration><random_number><seed value="43"/></random_number></sumoConfiguration> --><tripinfos/>')
    assert candidate_seed(c)==43
    trip.write_text('<!-- <sumoConfiguration><input/></sumoConfiguration> --><tripinfos/>')
    assert candidate_seed(c)==23423
