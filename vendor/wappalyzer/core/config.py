import json
import pathlib

config = {
    'threads': 3,
    'scan_type': 'full',
}

root_dir = str(pathlib.Path(__file__).parent.resolve()).removesuffix('core')
data_dir = root_dir + '/data'

tech_db = {}
with open(data_dir + '/technologies.json', 'r', encoding='utf-8') as f:
    tech_db = json.load(f)

cat_db = {}
with open(data_dir + '/categories.json', 'r', encoding='utf-8') as f:
    cat_db = json.load(f)

groups_db = {}
with open(data_dir + '/groups.json', 'r', encoding='utf-8') as f:
    groups_db = json.load(f)

extension_path = data_dir + '/wappalyzer-extension.zip'
