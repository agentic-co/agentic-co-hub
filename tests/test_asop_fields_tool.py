"""The field-provenance generator and the asop-spec watch, tested offline with fixtures.

`tools/asop_fields.py` installs released versions and asks PyPI; neither happens
here. What is tested is the logic the scheduled job depends on: provenance by
first release, the diff of a newer release against what the hub knows and
stores, the issue it would open, and that it never opens the same one twice.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "asop_fields", Path(__file__).resolve().parent.parent / "tools" / "asop_fields.py")
asop_fields = importlib.util.module_from_spec(spec)
spec.loader.exec_module(asop_fields)

DUMPS = {
    "0.5.0": {"asop": ["asop_id", "steps", "handoffs"], "gate": ["kind", "check"], "chain": ["handoffs"]},
    "0.4.0": {"asop": ["asop_id", "steps"], "gate": ["kind", "check"], "chain": []},
    "0.6.0": {"asop": ["asop_id", "steps", "handoffs", "entry_gates"], "gate": ["kind", "check", "min_cases"],
              "chain": ["handoffs", "entry_gates"]},
}


def test_provenance_is_the_first_release_with_each_field():
    table = asop_fields.provenance(DUMPS)
    assert table["asop"] == {"asop_id": "0.4.0", "entry_gates": "0.6.0", "handoffs": "0.5.0", "steps": "0.4.0"}
    assert table["gate"]["min_cases"] == "0.6.0"


def test_the_rendered_table_is_importable_and_round_trips():
    table = asop_fields.provenance(DUMPS)
    namespace: dict = {}
    exec(asop_fields.render(table, DUMPS), namespace)  # noqa: S102
    assert namespace["FIELDS"] == table and namespace["RELEASES"] == ("0.4.0", "0.5.0", "0.6.0")


def test_a_newer_release_diffs_against_what_the_hub_knows_and_stores():
    known = asop_fields.provenance({v: d for v, d in DUMPS.items() if v != "0.6.0"})
    newest = {"asop": ["asop_id", "steps", "handoffs", "entry_gates", "journal"],
              "gate": ["kind", "check", "min_cases"], "chain": ["handoffs", "entry_gates"]}
    columns = ["asop_id", "steps"]  # the hub stores no `handoffs` column in this fixture
    missing = asop_fields.new_fields(newest, known, columns)
    assert missing == {"asop": ["entry_gates", "handoffs", "journal"], "chain": ["entry_gates"],
                       "gate": ["min_cases"]}


def test_nothing_new_is_nothing_to_report():
    table = asop_fields.provenance(DUMPS)
    assert asop_fields.new_fields(DUMPS["0.6.0"], table, ["asop_id", "steps", "handoffs", "entry_gates"]) == {}


def test_the_issue_title_and_checklist():
    title, body = asop_fields.issue("0.7.0", {"asop": ["journal"], "gate": ["retries"]}, "0.6.0")
    assert title == "asop-spec 0.7.0 adds fields the hub does not forward: asop.journal, gate.retries"
    for item in ("lock", "allowlists", "columns", "provenance table", "regenerate 0.7.0"):
        assert item in body


def test_the_same_issue_is_never_opened_twice():
    title, _ = asop_fields.issue("0.7.0", {"asop": ["journal"]}, "0.6.0")
    assert asop_fields.should_open(title, ["something else"])
    assert not asop_fields.should_open(title, ["something else", title])


def test_the_locked_version_is_read_from_uv_lock():
    lock = '[[package]]\nname = "asop-spec"\nversion = "0.6.0"\nsource = { registry = "https://pypi.org/simple" }\n'
    assert asop_fields.locked_version(lock) == "0.6.0"
    assert asop_fields.locked_version("nothing here") is None
    real = (Path(__file__).resolve().parent.parent / "uv.lock").read_text()
    assert asop_fields.locked_version(real) is not None


def test_versions_compare_numerically():
    assert asop_fields.version_key("0.10.0") > asop_fields.version_key("0.9.1")
