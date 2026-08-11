"""Accumulates Actual Result / Status / Defect ID / Remarks per TC ID as the
professional QA suite is executed. Loaded/saved as JSON so execution can
span many separate script runs without losing progress.
"""
import json
import os

PATH = os.path.join(os.path.dirname(__file__), "results.json")


def load():
    if os.path.exists(PATH):
        with open(PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save(results):
    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)


def record(tc_id, actual, status, defect_id="", remarks=""):
    results = load()
    results[tc_id] = {
        "actual": actual, "status": status, "defect_id": defect_id, "remarks": remarks,
    }
    save(results)
    print(f"{tc_id}: {status} -- {actual[:100]}")
