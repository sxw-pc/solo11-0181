from .common import rows,latest,proof,placement_hours
from pathlib import Path
COMMAND='bloom';KINDS=['orchard','bloom_observation','spray_notice']
NORMAL=str(Path(__file__).resolve().parent.parent/'fixtures/bloom_ok.json')
FAILURE='fixtures/bloom_spray_late.json'
def decide(ev):
    bloom=latest(ev,'bloom_observation');sprays=rows(ev,'spray_notice')
    total,zones,ids=placement_hours(ev)
    return [('花期版本选定',bloom['data'].get('window','待观察') if bloom else '待观察',proof([bloom]) or ids),('喷药禁入发布','禁入' if any(s['data'].get('delivered') for s in sprays) else '未生效',proof(sprays) or ids),('果园分区覆盖认定',total,ids)]
