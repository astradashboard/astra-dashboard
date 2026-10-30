import json, importlib, sys
sys.path.insert(0, '.')
D = {}
for m in ['src1', 'src2', 'src3', 'src4']:
    for k, v in importlib.import_module(m).T.items():
        assert len(v) == 3, k
        D[k] = {'en': v[0], 'de': v[1], 'es': v[2]}
json.dump(D, open('../static/i18n.json', 'w'), ensure_ascii=False, separators=(',', ':'))
print(len(D))
